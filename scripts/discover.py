#!/usr/bin/env python3
"""Standalone mDNS / DNS-SD browser for the sunnyconf service — "do we see the comma over broadcast?".

Stdlib only; runs with any python3 (no openpilot deps), on Windows, macOS or Linux:

    python3 sunnyconf/scripts/discover.py [timeout_seconds]

Sends a multicast PTR query for `_sunnyconf._tcp.local.` and prints the instances that answer
(host / ip / port / TXT). mDNS is link-local multicast, so this host must be on the SAME subnet as the
comma. It binds UDP :5353 and joins the group on every local interface — important on Windows, which has
many virtual adapters (WSL/Hyper-V/VPN) and would otherwise query the wrong one.

Note: WSL2 does not receive LAN multicast reliably — run this from Windows/macOS/Linux natively, or use
`discover.sh device` (the comma's own avahi) as a cross-check.
"""
from __future__ import annotations

import socket
import struct
import sys
import time

MADDR, MPORT = "224.0.0.251", 5353
SERVICE = "_sunnyconf._tcp.local."
A, PTR, TXT, SRV = 1, 12, 16, 33


def enc_name(name: str) -> bytes:
  out = b""
  for label in name.rstrip(".").split("."):
    b = label.encode()
    out += bytes([len(b)]) + b
  return out + b"\x00"


def dec_name(data: bytes, off: int) -> tuple[str, int]:
  labels, jumped, end = [], False, off
  while True:
    if off >= len(data):
      break
    n = data[off]
    if n & 0xC0 == 0xC0:
      ptr = ((n & 0x3F) << 8) | data[off + 1]
      if not jumped:
        end = off + 2
      off, jumped = ptr, True
      continue
    off += 1
    if n == 0:
      break
    labels.append(data[off:off + n].decode("utf-8", "replace"))
    off += n
  if not jumped:
    end = off
  return ".".join(labels) + ".", end


def _query() -> bytes:
  return struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + enc_name(SERVICE) + struct.pack("!HH", PTR, 1)


def _parse(data: bytes, inst: dict, ips: dict) -> None:
  if len(data) < 12:
    return
  qd, an, ns, ar = struct.unpack("!HHHH", data[4:12])
  off = 12
  for _ in range(qd):
    _, off = dec_name(data, off)
    off += 4
  for _ in range(an + ns + ar):
    name, off = dec_name(data, off)
    if off + 10 > len(data):
      return
    rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
    off += 10
    rdata, rend = data[off:off + rdlen], off + rdlen
    if rtype == PTR:
      target, _ = dec_name(data, off)
      inst.setdefault(target, {})
    elif rtype == SRV and rdlen >= 6:
      _pri, _wt, port = struct.unpack("!HHH", rdata[:6])
      host, _ = dec_name(data, off + 6)
      inst.setdefault(name, {}).update(port=port, host=host)
    elif rtype == TXT:
      kv, i = {}, 0
      while i < len(rdata):
        length = rdata[i]
        i += 1
        item = rdata[i:i + length].decode("utf-8", "replace")
        i += length
        if "=" in item:
          k, v = item.split("=", 1)
          kv[k] = v
      inst.setdefault(name, {}).setdefault("txt", {}).update(kv)
    elif rtype == A and rdlen == 4:
      ips[name] = socket.inet_ntoa(rdata)
    off = rend


def _local_ipv4s() -> set[str]:
  ips: set[str] = set()
  try:
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
      ips.add(info[4][0])
  except OSError:
    pass
  # also the default-route source ip (works even if the hostname doesn't resolve to the LAN ip)
  try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 53))
    ips.add(s.getsockname()[0])
    s.close()
  except OSError:
    pass
  return {ip for ip in ips if not ip.startswith("127.")}


def main() -> int:
  timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
  ifaces = _local_ipv4s()

  sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # Windows: allows sharing :5353
  try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
  except (AttributeError, OSError):
    pass
  try:
    sock.bind(("", MPORT))                                      # must listen on 5353 for multicast answers
  except OSError as e:
    print(f"could not bind udp/{MPORT} ({e}). Close other mDNS tools (Bonjour/avahi) or run as admin.")
    return 2

  # join the group on every local interface (+ default), so multi-homed Windows joins the LAN adapter
  joined = list(ifaces) + ["0.0.0.0"]
  for ip in joined:
    try:
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                      socket.inet_aton(MADDR) + socket.inet_aton(ip))
    except OSError:
      pass
  sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

  def send_query():
    for ip in ifaces or {"0.0.0.0"}:
      try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
      except OSError:
        pass
      try:
        sock.sendto(_query(), (MADDR, MPORT))
      except OSError:
        pass

  print(f"browsing {SERVICE} for {timeout:g}s  (interfaces: {', '.join(sorted(ifaces)) or 'default'}) ...")
  inst: dict = {}
  ips: dict = {}
  send_query()
  sock.settimeout(0.5)
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      data, _addr = sock.recvfrom(9000)
      _parse(data, inst, ips)
    except TimeoutError:
      send_query()

  # only OUR service — the socket sees all mDNS traffic on :5353 (Windows _dosvc, comma _ssh, ...)
  found = {k: v for k, v in inst.items() if v.get("port") and k.endswith(SERVICE)}
  if not found:
    print("no _sunnyconf service found (daemon up? same subnet? mDNS blocked by the AP? WSL can't see LAN multicast)")
    return 1
  for name, info in sorted(found.items()):
    host = info.get("host", "")
    print(f"FOUND  {name}")
    print(f"       host={host}  ip={ips.get(host, '?')}  port={info.get('port', '?')}")
    if info.get("txt"):
      print(f"       txt={info['txt']}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
