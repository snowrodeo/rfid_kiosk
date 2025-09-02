
import asyncio
from sllurp import llrp

async def run():
    # Replace with your reader IP
    reader_ip = '192.168.1.47'

    # Connect to the reader
    reader = llrp.ConnectedReader(reader_ip)
    await reader.connect()

    # Define a callback for tag reports
    def tag_report_callback(tag_report):
        for tag in tag_report.tags:
            print(f"Tag EPC: {tag.epc}, Antenna: {tag.antenna_id}")

    reader.on('tag_report', tag_report_callback)

    # Start inventory
    await reader.start_inventory()

    # Keep running
    while True:
        await asyncio.sleep(1)

# Run the asyncio loop
asyncio.run(run())

