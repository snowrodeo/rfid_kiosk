#!/usr/bin/env python3
import socket
import ipaddress
import concurrent.futures

PORT = 5084
TIMEOUT = 1.0
SUBNET = "192.168.40.0/24"  # <-- change this for your LAN

# Minimal GET_READER_CAPABILITIES request (LLRP)
GET_CAPS = b"\x01\x00\x00\x0a\x00\x00\x00\x00\x00\x00"

def probe_reader(ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((str(ip), PORT))
            s.sendall(GET_CAPS)
            data = s.recv(4096)
            if b"Impinj" in data or len(data) > 0:
                return str(ip), data
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
                ip, data = result
                print(f"[FOUND] Impinj candidate at {ip}")
                # Print first bytes of reply for debugging
                print(f" Reply (hex): {data[:64].hex()}...")
                found.append(ip)
    return found

if __name__ == "__main__":
    readers = discover_r420(SUBNET)
    if not readers:
        print("No Impinj readers discovered.")
    else:
        print("Confirmed Impinj readers:", readers)

