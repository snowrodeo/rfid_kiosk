#!/usr/bin/env python3
import os
import time
import threading
import logging
from argparse import ArgumentParser

from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import Application, RequestHandler
from tornado.websocket import WebSocketHandler, WebSocketClosedError

# Sllurp (older API on your system)
from sllurp.llrp import LLRP_DEFAULT_PORT, LLRPReaderConfig, LLRPReaderClient

# ----------------------
# Config / Globals
# ----------------------
TAG_SUPPRESS_SECONDS = 30      # Per-chip “send once per X seconds”
TAG_EXPIRE_SECONDS   = 30      # Auto-remove from live table after X seconds
ANTENNAS_DEFAULT     = [1]

log = logging.getLogger("sllurp.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(levelname)s: %(message)s")

# Live state
IOL = IOLoop.current()
WS_CLIENTS = set()                     # Connected websocket clients
READER = None                          # Current LLRPReaderClient
READER_LOCK = threading.Lock()         # Prevent overlapping reconfigs
READER_HOST = None                     # Set from CLI
READER_PORT = LLRP_DEFAULT_PORT
CURRENT_POWER = 30                     # What we’re actually running at
DESIRED_POWER = 30                     # What the last slider change requested
ANTENNAS = ANTENNAS_DEFAULT[:]

# Tag caches
# tag_by_chipid: chipid -> dict(tag fields + last_seen_epoch)
tag_by_chipid = {}
# last_sent: chipid -> last time we “counted” it (for suppressing spam)
last_sent = {}

# Debounce power changes from the slider
POWER_DEBOUNCE_MS = 400
_power_change_handle = None
_is_reconfiguring = False


# ----------------------
# Helpers
# ----------------------
def convert_to_unicode(obj):
    if isinstance(obj, dict):
        return {convert_to_unicode(k): convert_to_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_unicode(e) for e in obj]
    elif isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    return obj


def broadcast(payload: dict):
    """Send JSONable payload to all connected WS clients from the IOLoop."""
    dead = []
    for ws in list(WS_CLIENTS):
        try:
            ws.write_message(payload)
        except WebSocketClosedError:
            dead.append(ws)
        except Exception as e:
            log.warning("WebSocket send error: %s", e)
    for d in dead:
        WS_CLIENTS.discard(d)


def now_s():
    return time.time()


def epc_to_str(epc_val):
    """Try to normalise EPC into string."""
    if isinstance(epc_val, bytes):
        try:
            return epc_val.decode("utf-8")
        except Exception:
            # Fallback to hex
            return epc_val.hex()
    return str(epc_val)


# ----------------------
# Sllurp callbacks
# ----------------------
def tag_report_callback(_reader, tags):
    """Called by sllurp in a worker thread. We must bounce to Tornado loop."""
    if not tags:
        return
    # normalize
    tags = convert_to_unicode(tags)
    IOL.add_callback(process_tags_on_ioloop, tags)


def process_tags_on_ioloop(tags):
    """Runs on Tornado IOLoop thread."""
    updated = False
    out_list = []

    tnow = now_s()
    for t in tags:
        # EPC field names vary a bit
        epc = t.get("EPC-96") or t.get("EPCData") or t.get("EPC")
        if not epc:
            continue
        epc_str = epc_to_str(epc)
        chipid = epc_str[-5:]  # last 5 chars

        # Build a compact record we’ll keep/display
        rec = {
            "chipid": chipid,
            "EPC-96": epc_str,
            "AntennaID": t.get("AntennaID"),
            "PeakRSSI": t.get("PeakRSSI"),
            "FirstSeenTimestamp": t.get("FirstSeenTimestampUTC") or t.get("FirstSeenTimestamp"),
            "LastSeenTimestamp": t.get("LastSeenTimestampUTC") or t.get("LastSeenTimestamp"),
            "TagSeenCount": t.get("TagSeenCount"),
        }

        # Update/insert in live table and mark last seen
        old = tag_by_chipid.get(chipid)
        tag_by_chipid[chipid] = {**(old or {}), **rec, "last_seen_epoch": tnow}
        out_list.append(tag_by_chipid[chipid])
        updated = True

        # Suppress “send repeatedly” within TAG_SUPPRESS_SECONDS (handled here for WS)
        ls = last_sent.get(chipid, 0)
        if tnow - ls >= TAG_SUPPRESS_SECONDS:
            last_sent[chipid] = tnow
            # (If you also need to POST chipid to another service, do it here)

    if updated:
        broadcast({"tags": list(tag_by_chipid.values()), "antenna_power": CURRENT_POWER})


def expire_stale_tags():
    """Remove tags that haven’t been seen in TAG_EXPIRE_SECONDS; run periodically on IOLoop."""
    tcut = now_s() - TAG_EXPIRE_SECONDS
    removed = []
    for chipid, rec in list(tag_by_chipid.items()):
        if rec.get("last_seen_epoch", 0) < tcut:
            removed.append(chipid)
            tag_by_chipid.pop(chipid, None)
    if removed:
        broadcast({"tags": list(tag_by_chipid.values()), "antenna_power": CURRENT_POWER})


# ----------------------
# Reader lifecycle
# ----------------------
def _build_config(tx_power: int) -> LLRPReaderConfig:
    """
    tx_power: 0..30 where 0 means max power for this reader (per sllurp convention).
    """
    factory_args = dict(
        antennas=ANTENNAS,
        tx_power=tx_power,
        start_inventory=True,
        report_every_n_tags=1,
        tag_content_selector={
            "EnableAntennaID": True,
            "EnablePeakRSSI": True,
            "EnableFirstSeenTimestamp": True,
            "EnableLastSeenTimestamp": True,
            "EnableTagSeenCount": True,
            # EPC bits defaults are fine for this use
        },
    )
    return LLRPReaderConfig(factory_args)


def _connect_reader_blocking(host: str, port: int, tx_power: int):
    """Run in a worker thread. Creates & connects a reader with given power."""
    global READER, CURRENT_POWER
    with READER_LOCK:
        # Clean up any existing reader
        if READER and READER.is_alive():
            try:
                READER.llrp.stopPolitely()
            except Exception:
                pass
            try:
                READER.disconnect()
            except Exception:
                pass
            READER = None

        cfg = _build_config(tx_power)
        reader = LLRPReaderClient(host, port, cfg)
        reader.add_tag_report_callback(tag_report_callback)
        reader.connect()
        READER = reader
        CURRENT_POWER = tx_power
        log.info("Reader connected @ %s:%s with tx_power=%s", host, port, tx_power)


def schedule_reconnect_with_power(tx_power: int):
    """Debounced reconfigure: called on the IOLoop."""
    global DESIRED_POWER, _power_change_handle
    DESIRED_POWER = int(tx_power)

    def _apply():
        global _is_reconfiguring, _power_change_handle
        _power_change_handle = None
        if _is_reconfiguring:
            # Will be handled when current cycle ends
            return
        _is_reconfiguring = True

        wanted = DESIRED_POWER

        def worker():
            global _is_reconfiguring
            try:
                log.info("Reconfiguring reader to tx_power=%s", wanted)
                _connect_reader_blocking(READER_HOST, READER_PORT, wanted)
                # After successful reconnect, notify clients of the new power
                IOL.add_callback(lambda: broadcast({"antenna_power": CURRENT_POWER,
                                                    "tags": list(tag_by_chipid.values())}))
            except Exception as e:
                log.error("Reconfigure failed: %s", e)
            finally:
                _is_reconfiguring = False
                # If the desired changed while we were working, schedule again
                if DESIRED_POWER != wanted:
                    IOL.add_callback(schedule_reconnect_with_power, DESIRED_POWER)

        threading.Thread(target=worker, daemon=True).start()

    # Debounce
    if _power_change_handle is not None:
        IOL.remove_timeout(_power_change_handle)
    _power_change_handle = IOL.call_later(POWER_DEBOUNCE_MS / 1000.0, _apply)


# ----------------------
# Tornado Handlers
# ----------------------
class RootHandler(RequestHandler):
    def get(self):
        # Serve templates/readerLink.html
        here = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(here, "templates", "readerLink.html")
        with open(html_path, "r", encoding="utf-8") as f:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(f.read())


class WSHandler(WebSocketHandler):
    def check_origin(self, origin):
        return True  # allow from kiosk device/LAN

    def open(self):
        WS_CLIENTS.add(self)
        # Push initial state
        self.write_message({
            "antenna_power": CURRENT_POWER,
            "tags": list(tag_by_chipid.values())
        })
        log.info("WebSocket client connected (total: %d)", len(WS_CLIENTS))

    def on_message(self, message):
        # Expect JSON like {"antenna_power": 27}
        try:
            import json
            data = json.loads(message)
        except Exception:
            log.warning("Invalid WS message (not JSON)")
            return

        if "antenna_power" in data:
            try:
                val = int(data["antenna_power"])
                # clamp 0..30
                if val < 0: val = 0
                if val > 30: val = 30
                log.info("Power change requested: %s", val)
                schedule_reconnect_with_power(val)
            except Exception:
                log.warning("Bad antenna_power value: %r", data.get("antenna_power"))

    def on_close(self):
        WS_CLIENTS.discard(self)
        log.info("WebSocket client disconnected (total: %d)", len(WS_CLIENTS))


# ----------------------
# App bootstrap
# ----------------------
def parse_args():
    p = ArgumentParser(description="RFID live dashboard with power control (sllurp legacy API)")
    p.add_argument("host", help="RFID reader IP/hostname")
    p.add_argument("-p", "--port", type=int, default=LLRP_DEFAULT_PORT)
    p.add_argument("-a", "--antennas", default="1", help="comma list, e.g. 1 or 1,2")
    p.add_argument("-X", "--tx-power", type=int, default=30, help="0=max, else 1..30")
    p.add_argument("--listen", default="0.0.0.0", help="Web server bind address")
    p.add_argument("--web-port", type=int, default=4000, help="Web server port")
    return p.parse_args()


def main():
    global READER_HOST, READER_PORT, CURRENT_POWER, DESIRED_POWER, ANTENNAS

    args = parse_args()
    READER_HOST = args.host
    READER_PORT = args.port

    # Antennas
    ANTENNAS = [int(x.strip()) for x in args.antennas.split(",") if x.strip()]
    if not ANTENNAS:
        ANTENNAS = ANTENNAS_DEFAULT[:]

    CURRENT_POWER = int(args.tx_power)
    DESIRED_POWER = CURRENT_POWER

    app = Application([
        (r"/", RootHandler),
        (r"/ws", WSHandler),
    ])
    app.listen(args.web_port, address=args.listen)
    log.info("Server running at http://%s:%d/", args.listen, args.web_port)

    # Connect reader in a worker thread at startup
    threading.Thread(
        target=_connect_reader_blocking,
        args=(READER_HOST, READER_PORT, CURRENT_POWER),
        daemon=True
    ).start()

    # Periodic cleanup of stale tags
    PeriodicCallback(expire_stale_tags, 1000).start()

    IOL.start()


if __name__ == "__main__":
    main()


