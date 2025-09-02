#!/usr/bin/env python3
import requests
from sllurp.llrp import LLRPClientFactory
from twisted.internet import reactor

# Change to your Flask server
FLASK_URL = "http://localhost:5000/api/tag"

def tag_report_cb(llrp_msg):
    """Handle incoming tag reports from the reader."""
    try:
        tag_reports = llrp_msg.msgdict['RO_ACCESS_REPORT']['TagReportData']
    except KeyError:
        return

    for tag in tag_reports:
        # Different readers may give EPC-96 or EPCData
        epc = None
        if 'EPC-96' in tag:
            epc = tag['EPC-96']
        elif 'EPCData' in tag:
            epc = tag['EPCData']
        if not epc:
            continue

        # Get RSSI if available
        rssi = tag.get('PeakRSSI')

        print(f"[SLLURP] Tag {epc} RSSI={rssi}")

        # Send to Flask backend
        try:
            resp = requests.post(
                FLASK_URL,
                json={"chipid": str(epc), "rssi": rssi},
                timeout=2
            )
            print(f"[POST] {resp.status_code} -> {resp.text}")
        except Exception as e:
            print(f"[ERROR] Failed to POST: {e}")

def connected(client):
    print("[SLLURP] Connected to RFID reader")

def finished(client, reason):
    print(f"[SLLURP] Disconnected: {reason}")
    reactor.stop()

def main():
    # Replace with your reader’s IP address
    READER_IP = "192.168.1.100"
    READER_PORT = 5084

    factory = LLRPClientFactory(
        antennas=[1],
        start_inventory=True,
        report_every_n=1,
        tag_report_callback=tag_report_cb,
    )
    factory.addClientConnectedCallback(connected)
    factory.addClientDisconnectedCallback(finished)

    reactor.connectTCP(READER_IP, READER_PORT, factory)
    reactor.run()

if __name__ == "__main__":
    main()

