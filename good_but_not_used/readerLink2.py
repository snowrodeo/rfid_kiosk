#!/usr/bin/env python3
import os
import time
import threading
import logging
import requests
from argparse import ArgumentParser

from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import Application, RequestHandler
from tornado.websocket import WebSocketHandler, WebSocketClosedError

from sllurp.llrp import LLRP_DEFAULT_PORT, LLRPReaderConfig, LLRPReaderClient

# ----------------------
# Config / Globals
# ----------------------
TAG_SUPPRESS_SECONDS = 30      # Minimum seconds between sending the same tag to WS
TAG_EXPIRE_SECONDS   = 30      # Auto-remove from live table after X seconds
ANTENNAS_DEFAULT     = [1]
API_ENDPOINT         = "http://localhost:5000/api/tag"  # Legacy API for per-tag POST

log = logging.getLogger("sllurp.app")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s: %(levelname)s: %(message)s")

IOL = IOLoop.current()
WS_CLIENTS = set()
READER = None
READER_LOCK = threading.Lock()
READER_HOST = None
READER_PORT = LLRP_DEFAULT_PORT
CURRENT_POWER = 30
DESIRED_POWER = 30
ANTENNAS = ANTENNAS_DEFAULT[:]

# Tag caches
tag_by_chipid = {}   # chipid -> full record + last_seen_epoch
last_sent_ws = {}    # chipid -> last time sent to WS
last_sent_api = {}   # chipid -> last time sent to API endpoint
TAG_API_TIMEOUT = 30 # seconds per chipid

# Debounce power changes
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
    """Send JSON payload to all connected WS clients from IOLoop."""
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
    if isinstance(epc_val, bytes):
        try:
            return epc_val.decode("utf-8")
        except Exception:
            return epc_val.hex()
    return str(epc_val)

# ----------------------
# Sllurp Callbacks
# ----------------------
def tag_report_callback(_reader, tags):
    """Worker thread callback from sllurp."""
    if not tags:
        return
    tags = convert_to_unicode(tags)
    IOL.add_callback(process_tags_on_ioloop, tags)

def process_tags_on_ioloop(tags):
    """Process tags on IOLoop thread: update live table, broadcast, POST to API."""
    global tag_by_chipid
    updated_ws = False
    out_list = []

    tnow = now_s()
    for t in tags:
        epc = t.get("EPC-96") or t.get("EPCData") or t.get("EPC")
        if not epc:
            continue
        epc_str = epc_to_str(epc)
        chipid = epc_str[-5:]

        rec = {
            "chipid": chipid,
            "EPC-96": epc_str,
            "AntennaID": t.get("AntennaID"),
            "PeakRSSI": t.get("PeakRSSI"),
            "FirstSeenTimestamp": t.get("FirstSeenTimestampUTC") or t.get("FirstSeenTimestamp"),
            "LastSeenTimestamp": t.get("LastSeenTimestampUTC") or t.get("LastSeenTimestamp"),
            "TagSeenCount": t.get("TagSeenCount"),
        }

        old = tag_by_chipid.get(chipid)
        tag_by_chipid[chipid] = {**(old or {}), **rec, "last_seen_epoch": tnow}
        out_list.append(tag_by_chipid[chipid])

        # WS throttling
        last_ws = last_sent_ws.get(chipid, 0)
        if tnow - last_ws >= TAG_SUPPRESS_SECONDS:
            last_sent_ws[chipid] = tnow
            updated_ws = True

        # Legacy API throttling
        last_api = last_sent_api.get(chipid, 0)
        if tnow - last_api >= TAG_API_TIMEOUT:
            last_sent_api[chipid] = tnow
            threading.Thread(target=post_tag_to_api, args=(chipid,), daemon=True).start()

    if updated_ws:
        broadcast({"tags": list(tag_by_chipid.values()), "antenna_power": CURRENT_POWER})

def post_tag_to_api(chipid):
    """Send single tag to legacy API endpoint."""
    try:
        payload = {"chipid": chipid}
        r = requests.post(API_ENDPOINT, json=payload, timeout=1)
        if r.ok:
            log.info("Sent tag %s -> %s", chipid, r.json())
        else:
            log.warning("Failed to send tag %s: %s", chipid, r.status_code)
    except Exception as e:
        log.error("Error posting tag %s: %s", chipid, e)

def expire_stale_tags():
    """Remove tags older than TAG_EXPIRE_SECONDS."""
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
        },
    )
    return LLRPReaderConfig(factory_args)

def _connect_reader_blocking(host: str, port: int, tx_power: int):
    global READER, CURRENT_POWER
    with READER_LOCK:
        if READER and READER.is_alive():
            try: READER.llrp.stopPolitely()
            except: pass
            try: READER.disconnect()
            except: pass
            READER = None

        cfg = _build_config(tx_power)
        reader = LLRPReaderClient(host, port, cfg)
        reader.add_tag_report_callback(tag_report_callback)
        reader.connect()
        READER = reader
        CURRENT_POWER = tx_power
        log.info("Reader connected @ %s:%s with tx_power=%s", host, port, tx_power)

def schedule_reconnect_with_power(tx_power: int):
    global DESIRED_POWER, _power_change_handle, _is_reconfiguring
    DESIRED_POWER = int(tx_power)

    def _apply():
        global _is_reconfiguring, _power_change_handle
        _power_change_handle = None
        if _is_reconfiguring:
            return
        _is_reconfiguring = True
        wanted = DESIRED_POWER

        def worker():
            global _is_reconfiguring
            try:
                log.info("Reconfiguring reader to tx_power=%s", wanted)
                _connect_reader_blocking(READER_HOST, READER_PORT, wanted)
                IOL.add_callback(lambda: broadcast({
                    "antenna_power": CURRENT_POWER,
                    "tags": list(tag_by_chipid.values())
                }))
            except Exception as e:
                log.error("Reconfigure failed: %s", e)
            finally:
                _is_reconfiguring = False
                if DESIRED_POWER != wanted:
                    IOL.add_callback(schedule_reconnect_with_power, DESIRED_POWER)

        threading.Thread(target=worker, daemon=True).start()

    if _power_change_handle is not None:
        IOL.remove_timeout(_power_change_handle)
    _power_change_handle = IOL.call_later(POWER_DEBOUNCE_MS / 1000.0, _apply)

# ----------------------
# Tornado Handlers
# ----------------------
class RootHandler(RequestHandler):
    def get(self):
        here = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(here, "templates", "readerLink.html")
        with open(html_path, "r", encoding="utf-8") as f:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(f.read())

class WSHandler(WebSocketHandler):
    def check_origin(self, origin):
        return True

    def open(self):
        WS_CLIENTS.add(self)
        # Push initial state
        self.write_message({
            "antenna_power": CURRENT_POWER,
            "tags": list(tag_by_chipid.values())
        })
        log.info("WebSocket client connected (total: %d)", len(WS_CLIENTS))

    def on_message(self, message):
        try:
            import json
            data = json.loads(message)
        except Exception:
            log.warning("Invalid WS message (not JSON)")
            return

        if "antenna_power" in data:
            try:
                val = int(data["antenna_power"])
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
    p = ArgumentParser(description="RFID live dashboard with power control + legacy API")
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

    threading.Thread(
        target=_connect_reader_blocking,
        args=(READER_HOST, READER_PORT, CURRENT_POWER),
        daemon=True
    ).start()

    PeriodicCallback(expire_stale_tags, 1000).start()
    IOL.start()

if __name__ == "__main__":
    main()

