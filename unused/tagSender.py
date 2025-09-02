#!/usr/bin/env python3
import asyncio
import requests
import time
from sllurp.llrp import LLRPReaderClient, LLRPReaderConfig, LLRP_DEFAULT_PORT

API_URL = "http://localhost:5000/api/tag"
DUPLICATE_IGNORE_SECONDS = 3  # ignore the same tag for 3 seconds

# Keep track of last seen timestamp for each chipid
last_seen = {}

def tag_report_cb(reader, tag_reports):
    global last_seen
    if not tag_reports:
        return

    # Use 'PeakRssi' if available, else 'Rssi', else 0
    def get_rssi(tag):
        return tag.get("PeakRssi") or tag.get("Rssi") or 0

    # Pick the tag with the strongest RSSI
    best_tag = max(tag_reports, key=get_rssi)
    rssi = get_rssi(best_tag)

    # Ignore if RSSI is 0
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

    payload = {"chipid": chipid}
    try:
        r = requests.post(API_URL, json=payload)
        if r.status_code == 200:
            print(f"Sent to API: {payload}")
        else:
            print(f"API error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Error sending to API: {e}")

async def main(reader_ip):
    cfg = LLRPReaderConfig()
    cfg.reset_on_connect = True
    cfg.start_inventory = True

    reader = LLRPReaderClient(reader_ip, LLRP_DEFAULT_PORT, cfg)
    reader.add_tag_report_callback(tag_report_cb)
    reader.connect()
    reader.join(None)

if __name__ == "__main__":
    reader_ip = input("Enter reader IP address: ").strip()
    asyncio.run(main(reader_ip))

