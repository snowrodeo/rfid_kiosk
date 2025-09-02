#!/usr/bin/env python3
import socket
import ipaddress
import concurrent.futures
import subprocess
import re

PORT = 5084
TIMEOUT = 1.0

# Minimal GET_READER_CAPABILITIES request (LLRP)
GET_CAPS = b"\x01\x00\x00\x0a\x00\x00\x00\x00\x00\x00"

def get_lan_subnet():
    """
    Automatically detect a LAN subnet for the host.
    Ignores loopback, docker, and link-local addresses.
    """
    try:
        output = subprocess.check_output(["ip", "-4", "addr"], text=True)
    except Exception:
        print("Cannot run `ip addr` to detect LAN IP.")
        exit(1)

    # Parse lines with 'inet', skip loopback and link-local
    matches = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output)
    for ip, prefix in matches:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        # Accept common LAN ranges
        if ip.startswith("10.") or ip.startswith("192.168.") or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31):
            network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
            print(f"Detected LAN subnet: {network} (interface IP: {ip})")
            return network

    print("Could not detect LAN subnet automatically. Please set SUBNET manually.")
    exit(1)

def probe_reader(ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((str(ip), PORT))
            s.sendall(GET_CAPS)
            data = s.recv(4096)
            if data:
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
    # Use detected subnet instead of hardcoded one
    SUBNET = get_lan_subnet()
    readers = discover_r420(SUBNET)
    if not readers:
        print("No Impinj readers discovered.")
    else:
        print("Confirmed Impinj readers:", readers)

