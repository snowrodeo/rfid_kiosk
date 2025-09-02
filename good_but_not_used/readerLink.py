#!/usr/bin/env python3
import os
from argparse import ArgumentParser
from logging import getLogger, INFO, Formatter, StreamHandler, WARN

from tornado.escape import json_decode
from tornado.ioloop import IOLoop
from tornado.template import Loader
from tornado.web import RequestHandler, Application
from tornado.websocket import WebSocketClosedError, WebSocketHandler

from sllurp.llrp import LLRP_DEFAULT_PORT, LLRPReaderConfig, LLRPReaderClient
from sllurp.log import get_logger
import requests
import time

logger = get_logger('sllurp')
tornado_main_ioloop = None


def setup_logging():
    logger.setLevel(INFO)
    logFormat = '%(asctime)s %(name)s: %(levelname)s: %(message)s'
    formatter = Formatter(logFormat)
    handler = StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    stream_handler_warn = StreamHandler()
    stream_handler_warn.setLevel(WARN)
    stream_handler_warn.setFormatter(formatter)

    access_log = getLogger("tornado.access")
    access_log.addHandler(stream_handler_warn)

    app_log = getLogger("tornado.application")
    app_log.addHandler(handler)

    gen_log = getLogger("tornado.general")
    gen_log.addHandler(handler)


class DefaultHandler(RequestHandler):
    def get(self):
        loader = Loader(os.path.dirname(__file__))
        template = loader.load("index.html")
        self.write(template.generate())


class MyWebSocketHandler(WebSocketHandler):
    _listeners = set([])

    @classmethod
    def dispatch_tags(cls, tags):
        payload = {"tags": tags}
        dead = []
        for listener in cls._listeners:
            try:
                listener.write_message(payload)
            except WebSocketClosedError:
                dead.append(listener)
        for d in dead:
            cls._listeners.remove(d)

    def open(self):
        MyWebSocketHandler._listeners.add(self)
        logger.info("WebSocket client connected (total: %d)",
                    len(self._listeners))

    def on_close(self):
        MyWebSocketHandler._listeners.discard(self)
        logger.info("WebSocket client disconnected (total: %d)",
                    len(self._listeners))

    def on_message(self, message):
        try:
            data = json_decode(message)
            logger.info("Received from WS client: %s", data)
        except ValueError:
            logger.warning("Invalid message: %s", message)


def convert_to_unicode(obj):
    """Ensure JSON serializable strings."""
    if isinstance(obj, dict):
        return {convert_to_unicode(k): convert_to_unicode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_unicode(e) for e in obj]
    elif isinstance(obj, bytes):
        return obj.decode("utf-8")
    else:
        return obj

API_ENDPOINT = "http://localhost:5000/api/tag"
seen_tags = {}   # {chipid: last_sent_timestamp}
TAG_TIMEOUT = 30 # seconds

def tag_seen_callback(reader, tags):
    """Run each time the reader reports tags."""
    if not tags:
        return
    tags = convert_to_unicode(tags)

    for tag in tags:
        # Extract EPC (try multiple field names depending on report content)
        epc = tag.get("EPC-96") or tag.get("EPCData")
        if not epc:
            continue

        # Use last 5 characters
        chipid = epc[-5:]

        now = time.time()
        last_sent = seen_tags.get(chipid, 0)

        if now - last_sent < TAG_TIMEOUT:
            # Skip, seen too recently
            continue

        # Record send time
        seen_tags[chipid] = now

        # Send to frontend
        try:
            payload = {"chipid": chipid}
            r = requests.post(API_ENDPOINT, json=payload, timeout=1)
            if r.ok:
                logger.info("Sent tag %s -> %s", chipid, r.json())
            else:
                logger.warning("Failed to send tag %s: %s", chipid, r.status_code)
        except Exception as e:
            logger.error("Error posting tag %s: %s", chipid, e)


def parse_args():
    parser = ArgumentParser(description="Simple RFID Reader Inventory w/ WebSocket")
    parser.add_argument("host", help="hostname or IP address of RFID reader")
    parser.add_argument("-p", "--port", default=LLRP_DEFAULT_PORT, type=int)
    parser.add_argument("-a", "--antennas", default="1")
    return parser.parse_args()


def main(args):
    global tornado_main_ioloop
    setup_logging()
    tornado_main_ioloop = IOLoop.current()

    application = Application([
        (r"/", DefaultHandler),
        (r"/ws", MyWebSocketHandler)
    ])
    application.listen(4000)   # <<< changed from 8888 → 4000

    enabled_antennas = [int(x.strip()) for x in args.antennas.split(",")]
    factory_args = dict(
        antennas=enabled_antennas,
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

    config = LLRPReaderConfig(factory_args)
    reader = LLRPReaderClient(args.host, args.port, config)
    reader.add_tag_report_callback(tag_seen_callback)
    reader.connect()

    logger.info("Server running at http://localhost:4000/")
    tornado_main_ioloop.start()


if __name__ == "__main__":
    cmd_args = parse_args()
    main(cmd_args)

