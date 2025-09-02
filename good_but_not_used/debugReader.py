#!/usr/bin/env python3
import asyncio
import requests
import time
import logging
from sllurp.llrp import LLRPReaderClient, LLRPReaderConfig, LLRP_DEFAULT_PORT

# ---------------- CONFIG -----------------
API_URL = "http://localhost:5000/api/tag"
ANTENNAS = [1]                 # Adjust to your connected antenna(s)
DUPLICATE_IGNORE_SECONDS = 3   # Ignore repeated chip IDs within this time

# Enable debug logging to see all LLRP messages
logging.basicConfig(level=logging.DEBUG)

# Track last seen time for each chipid
last_seen = {}

# --------------- CALLBACK ----------------
def tag_report_cb(reader, tag_reports):
    global last_seen
    if not tag_reports:
        return

    print("\n--- RAW TAG REPORTS ---")
    for tag in tag_reports:
        print(tag)  # show exactly what keys are returned

    # Helper to get RSSI safely
    def get_rssi(tag):
        return tag.get("PeakRssi") or tag.get("Rssi") or 0

    # Pick the tag with the strongest RSSI
    best_tag = max(tag_reports, key=get_rssi)
    rssi = get_rssi(best_tag)

    # Ignore if RSSI is zero
    if rssi == 0:
        return

    epc_str = best_tag["EPC"].hex().upper()
    chipid = epc_str[-5:]

    # Ignore duplicates
    now = time.time()
    if chipid in last_seen and now - last_seen[chipid] < DUPLICATE_IGNORE_SECONDS:
        return
    last_seen[chipid] = now

    print(f"Best tag: EPC={epc_str}, chipid={chipid}, RSSI={rssi}")

    # Send to API
    payload = {"chipid": chipid}
    try:
        r = requests.post(API_URL, json=payload, timeout=1)
        if r.status_code == 200:
            print(f"Sent to API: {payload}")
        else:
            print(f"API error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Error sending to API: {e}")

# --------------- MAIN --------------------
async def main(reader_ip):
    cfg = LLRPReaderConfig()
    cfg.reset_on_connect = True
    cfg.start_inventory = True          # Start ROSpec immediately
    cfg.stop_inventory_on_disconnect = False
    cfg.enable_auto_report = True       # Continuous tag reporting
    cfg.antennas = ANTENNAS

    reader = LLRPReaderClient(reader_ip, LLRP_DEFAULT_PORT, cfg)
    reader.add_tag_report_callback(tag_report_cb)

    print(f"Connecting to reader at {reader_ip}...")
    reader.connect()

    # Keep the script alive indefinitely
    reader.join(None)

# --------------- ENTRY POINT ----------------
if __name__ == "__main__":
    reader_ip = input("Enter reader IP address: ").strip()
    asyncio.run(main(reader_ip))


