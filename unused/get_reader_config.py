#!/usr/bin/env python3
from twisted.internet import reactor
from sllurp.llrp import LLRPClientFactory
from sllurp.llrp_proto import LLRPMessage, LLRPParam

import sys

# Default LLRP port
LLRP_PORT = 5084

# Minimal GET_READER_CAPABILITIES message (LLRP)
GET_CAPS_MSG = LLRPMessage('GET_READER_CAPABILITIES', params=[
    LLRPParam('RequestedData', 'All')
])

class ReaderInfo:
    def __init__(self, ip):
        self.ip = ip
        self.factory = None

    def got_message(self, msg):
        # Called on every LLRP message received
        if msg.message_type == 'READER_CAPABILITIES':
            print(f"\n--- Reader Capabilities for {self.ip} ---")
            # Basic info
            print("Model:", msg.get_param('ModelName', 'Unknown'))
            print("Manufacturer:", msg.get_param('ManufacturerName', 'Unknown'))
            print("Serial Number:", msg.get_param('ReaderID', 'Unknown'))
            print("Num Antennas:", msg.get_param('NumAntennaSupported', 'Unknown'))
            print("Max TX Power:", msg.get_param('MaxTxPower', 'Unknown'))

            antennas = msg.get_param('AntennaProperties', [])
            for ant in antennas:
                ant_id = ant.get_param('AntennaID', 'Unknown')
                tx_power = ant.get_param('TxPower', 'Unknown')
                rx_sens = ant.get_param('RxSensitivity', 'Unknown')
                print(f"Antenna {ant_id}: TX Power={tx_power}, RX Sensitivity={rx_sens}")

            reactor.stop()  # Done

def main(ip):
    info = ReaderInfo(ip)
    factory = LLRPClientFactory(ip, LLRP_PORT)
    factory.on_message = info.got_message
    factory.on_ready = lambda: factory.send_msg(GET_CAPS_MSG)
    reactor.connectTCP(ip, LLRP_PORT, factory)
    reactor.run()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <reader-ip>")
        sys.exit(1)

    main(sys.argv[1])



