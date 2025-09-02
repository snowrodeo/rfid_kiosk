#!/usr/bin/env python3
import os
import time
import threading
import logging
import socket
import ipaddress
import concurrent.futures
import subprocess
import re
import requests
from argparse import ArgumentParser

from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import Application, RequestHandler
from tornado.websocket import WebSocketHandler, WebSocketClosedError

from sllurp.llrp import LLRP_DEFAULT_PORT, LLRPReaderConfig, LLRPReaderClient

# ----------------------
# Config / Globals
# ----------------------
TAG_SUPPRESS_SECONDS = 30
TAG_EXPIRE_SECONDS = 30
ANTENNAS_DEFAULT = [1]

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

API_ENDPOINT = "http://localhost:5000/api/tag"

# Tag caches
tag_by_chipid = {}
last_sent = {}
api_sent_tags = {}

# Debounce power changes
POWER_DEBOUNCE_MS = 400
_power_change_handle = None
_is_reconfiguring = False

# ----------------------
# Helpers
# ----------------------
def get_lan_subnet():
    try:
        output = subprocess.check_output(["ip", "-4", "addr"], text=True)
    except Exception:
        log.error("Cannot run `ip addr` to detect LAN IP.")
        exit(1)

    matches = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output)
    for ip, prefix in matches:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip.startswith("10.") or ip.startswith("192.168.") or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31):
            network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
            log.info(f"Detected LAN subnet: {network} (interface IP: {ip})")
            return network
    log.error("Could not detect LAN subnet automatically.")
    exit(1)

GET_CAPS = b"\x01\x00\x00\x0a\x00\x00\x00\x00\x00\x00"
PORT = 5084
TIMEOUT = 1.0

def probe_reader(ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((str(ip), PORT))
            s.sendall(GET_CAPS)
            data = s.recv(4096)
            if data:
                return str(ip)
    except Exception:
        return None
    return None

def discover_r420(subnet):
    net = ipaddress.ip_network(subnet, strict=False)
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(probe_reader, ip): ip for ip in net.hosts()}
        for f in concurrent.futures.as_completed(futures):
            result = f.result()
            if result:
                ip = result
                log.info(f"[FOUND] Impinj candidate at {ip}")
                found.append(ip)
    return found

def convert_to_unicode(obj):
    if isinstance(obj, dict):
        return {convert_to_unicode(k): convert_to_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_unicode(e) for e in obj]
    elif isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    return obj

def now_s():
    return time.time()

def epc_to_str(epc_val):
    if isinstance(epc_val, bytes):
        try:
            return epc_val.decode("utf-8")
        except Exception:
            return epc_val.hex()
    return str(epc_val)

def broadcast(payload: dict):
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

# ----------------------
# Tag handling
# ----------------------
def tag_report_callback(_reader, tags):
    if not tags:
        return
    tags = convert_to_unicode(tags)
    IOL.add_callback(process_tags_on_ioloop, tags)

def process_tags_on_ioloop(tags):
    global tag_by_chipid, last_sent
    tnow = now_s()
    out_list = []
    updated = False
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
        updated = True

        ls = last_sent.get(chipid, 0)
        if tnow - ls >= TAG_SUPPRESS_SECONDS:
            last_sent[chipid] = tnow
            threading.Thread(target=post_tag_to_api, args=(chipid,), daemon=True).start()

    if updated:
        broadcast({"tags": list(tag_by_chipid.values()), "antenna_power": CURRENT_POWER})

def expire_stale_tags():
    tcut = now_s() - TAG_EXPIRE_SECONDS
    removed = []
    for chipid, rec in list(tag_by_chipid.items()):
        if rec.get("last_seen_epoch", 0) < tcut:
            removed.append(chipid)
            tag_by_chipid.pop(chipid, None)
    if removed:
        broadcast({"tags": list(tag_by_chipid.values()), "antenna_power": CURRENT_POWER})

def post_tag_to_api(chipid):
    try:
        payload = {"chipid": chipid}
        r = requests.post(API_ENDPOINT, json=payload, timeout=1)
        if r.ok:
            log.info(f"Sent tag {chipid} -> {r.json()}")
            IOL.add_callback(lambda: broadcast({
                "api_sent": {"chipid": chipid, "timestamp": int(now_s())}
            }))
        else:
            log.warning(f"Failed to send tag {chipid}: {r.status_code}")
    except Exception as e:
        log.error(f"Error posting tag {chipid}: {e}")

# ----------------------
# Reader handling
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
        log.info(f"Reader connected @ {host}:{port} with tx_power={tx_power}")

def schedule_reconnect_with_power(tx_power: int):
    global DESIRED_POWER, _power_change_handle
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
                log.info(f"Reconfiguring reader to tx_power={wanted}")
                _connect_reader_blocking(READER_HOST, READER_PORT, wanted)
                IOL.add_callback(lambda: broadcast({"antenna_power": CURRENT_POWER,
                                                    "tags": list(tag_by_chipid.values())}))
            except Exception as e:
                log.error(f"Reconfigure failed: {e}")
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
    def check_origin(self, origin): return True
    def open(self):
        WS_CLIENTS.add(self)
        self.write_message({
            "antenna_power": CURRENT_POWER,
            "tags": list(tag_by_chipid.values())
        })
        log.info(f"WebSocket client connected (total: {len(WS_CLIENTS)})")
    def on_message(self, message):
        try:
            import json
            data = json.loads(message)
        except Exception:
            log.warning("Invalid WS message")
            return
        if "antenna_power" in data:
            try:
                val = int(data["antenna_power"])
                val = max(0, min(30, val))
                log.info(f"Power change requested: {val}")
                schedule_reconnect_with_power(val)
            except Exception:
                log.warning(f"Bad antenna_power value: {data.get('antenna_power')}")
    def on_close(self):
        WS_CLIENTS.discard(self)
        log.info(f"WebSocket client disconnected (total: {len(WS_CLIENTS)})")

# ----------------------
# App bootstrap
# ----------------------
def parse_args():
    p = ArgumentParser(description="RFID live dashboard with power control (sllurp legacy API)")
    p.add_argument("-p", "--port", type=int, default=LLRP_DEFAULT_PORT)
    p.add_argument("-a", "--antennas", default="1")
    p.add_argument("-X", "--tx-power", type=int, default=30)
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--web-port", type=int, default=4000)
    return p.parse_args()

def main():
    global READER_HOST, READER_PORT, CURRENT_POWER, DESIRED_POWER, ANTENNAS
    args = parse_args()

    # Detect reader automatically
    while True:
        subnet = get_lan_subnet()
        found = discover_r420(subnet)
        if found:
            READER_HOST = found[0]
            log.info(f"Using reader {READER_HOST}")
            break
        else:
            log.warning("No R420 detected, sleeping 30s...")
            time.sleep(30)

    ANTENNAS = [int(x.strip()) for x in args.antennas.split(",") if x.strip()]
    if not ANTENNAS: ANTENNAS = ANTENNAS_DEFAULT[:]
    CURRENT_POWER = DESIRED_POWER = args.tx_power

    app = Application([
        (r"/", RootHandler),
        (r"/ws", WSHandler),
    ])
    #app.listen(args.web_port, address=args.listen)
    app.listen(args.web_port, address="0.0.0.0")
    log.info(f"Server running at http://{args.listen}:{args.web_port}/")

    # Connect reader in a worker thread
    threading.Thread(target=_connect_reader_blocking, args=(READER_HOST, READER_PORT, CURRENT_POWER), daemon=True).start()
    PeriodicCallback(expire_stale_tags, 1000).start()

    IOL.start()

if __name__ == "__main__":
    main()


