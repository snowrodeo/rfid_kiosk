#!/usr/bin/env python3
import socket
import ipaddress
import concurrent.futures
import fcntl
import struct
import os
import sys

PORT = 5084
TIMEOUT = 1.0

# Minimal GET_READER_CAPABILITIES request (LLRP)
GET_CAPS = b"\x01\x00\x00\x0a\x00\x00\x00\x00\x00\x00"

def get_default_lan_subnet():
    """Return the primary LAN subnet automatically."""
    hostname = socket.gethostname()
    ip = None
    for iface_name in os.listdir('/sys/class/net/'):
        if iface_name == 'lo':
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ip_bytes = fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack('256s', iface_name.encode('utf-8')[:15])
            )[20:24]
            ip_addr = socket.inet_ntoa(ip_bytes)
            # Simple check: skip docker, virtual interfaces
            if ip_addr.startswith("127.") or ip_addr.startswith("172.") or ip_addr.startswith("10.") or ip_addr.startswith("192.168."):
                ip = ip_addr
                break
        except Exception:
            continue
    if not ip:
        print("Could not detect LAN IP automatically.")
        sys.exit(1)
    # Assume /24 subnet
    network = ipaddress.ip_network(ip + '/24', strict=False)
    return network

def parse_ascii_strings(data, min_len=4):
    """Extract readable ASCII substrings from binary data."""
    result = []
    current = []
    for b in data:
        if 32 <= b <= 126:  # printable ASCII
            current.append(chr(b))
        else:
            if len(current) >= min_len:
                result.append(''.join(current))
            current = []
    if len(current) >= min_len:
        result.append(''.join(current))
    return result

def probe_reader(ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((str(ip), PORT))
            s.sendall(GET_CAPS)
            data = s.recv(4096)
            if data:
                strings = parse_ascii_strings(data)
                if any("Impinj" in s or "Speedway" in s for s in strings):
                    return str(ip), strings
    except Exception:
        return None
    return None

def discover_r420(network):
    found = []
    print(f"Scanning subnet: {network}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(probe_reader, ip): ip for ip in network.hosts()}
        for f in concurrent.futures.as_completed(futures):
            result = f.result()
            if result:
                ip, strings = result
                print(f"[FOUND] Impinj reader at {ip}")
                print("  Capabilities strings:", strings)
                found.append(ip)
    return found

if __name__ == "__main__":
    subnet = get_default_lan_subnet()
    readers = discover_r420(subnet)
    if not readers:
        print("No Impinj readers discovered.")
    else:
        print("Confirmed Impinj readers:", readers)

