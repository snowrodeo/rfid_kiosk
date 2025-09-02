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
    """Detect the main LAN subnet by checking non-loopback IPv4 addresses."""
    try:
        output = subprocess.check_output(["ip", "-4", "addr"], text=True)
    except Exception:
        print("Cannot run `ip addr` to detect LAN IP.")
        exit(1)

    # Parse lines with 'inet', skip loopback and docker interfaces
    matches = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", output)
    for ip, prefix in matches:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("172.") or ip.startswith("10.") or ip.startswith("192.168."):
            # Accept typical LAN IPs (10.x.x.x, 192.168.x.x, 172.16-31.x.x)
            # Further restrict to exclude loopback & autoconfig addresses
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
                print(f"Detected LAN subnet: {network} (interface IP: {ip})")
                return network
    print("Could not detect LAN subnet automatically. Please set SUBNET manually.")
    exit(1)

def parse_ascii_strings(data, min_len=4):
    """Extract readable ASCII substrings from binary data."""
    result = []
    current = []
    for b in data:
        if isinstance(b, str):
            b = ord(b)  # Python 2 compatibility
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
    """Connect, send GET_READER_CAPABILITIES, return human-readable info."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect((str(ip), PORT))
            s.sendall(GET_CAPS)
            data = s.recv(4096)
            if not data:
                return None
            strings = parse_ascii_strings(data)
            info = {}
            for s in strings:
                if "Impinj" in s:
                    info['Manufacturer'] = s
                if "Speedway" in s:
                    info['Model'] = s
                if re.match(r"^[0-9A-F]{4,}$", s):
                    info['Serial'] = s
            if info:
                return str(ip), info
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
                ip, info = result
                print(f"[FOUND] Impinj reader at {ip}")
                for k,v in info.items():
                    print(f"  {k}: {v}")
                found.append((ip, info))
    return found

if __name__ == "__main__":
    subnet = get_lan_subnet()
    readers = discover_r420(subnet)
    if not readers:
        print("No Impinj readers discovered.")
    else:
        print("Confirmed Impinj readers:")
        for ip, info in readers:
            print(f"{ip} -> {info}")

