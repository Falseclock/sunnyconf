"""
sunnyconf.discovery — announce the service over mDNS / DNS-SD so the Android app finds the comma
without knowing its IP.

Chosen stack (see INVESTIGATION.md): a self-contained, stdlib-only multicast-DNS responder. We do NOT
depend on `zeroconf` (a pip dep would not reach the AGNOS image via the git-only updater) nor on system
avahi (not guaranteed present, and wiring it would require a launch-script hook we avoid). If `zeroconf`
happens to be importable (e.g. a dev box) we use it for convenience. Discovery is best-effort: any
failure is logged and the HTTP service still runs — the Android client also has a subnet-scan fallback.

Advertises: type `_sunnyconf._tcp.local.`, instance `comma4._sunnyconf._tcp.local.`, port 8765,
TXT {schema_version, sunnypilot_commit, device_id}.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

from openpilot.common.swaglog import cloudlog
from . import DEFAULT_PORT, SERVICE_TYPE, SERVICE_INSTANCE

_MCAST_ADDR = "224.0.0.251"
_MCAST_PORT = 5353
_TTL = 120

# DNS record types
_A, _PTR, _TXT, _SRV, _ANY = 1, 12, 16, 33, 255


# ---- interface IP -----------------------------------------------------------

def _iface_ip(ifname: str) -> str | None:
  try:
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    packed = struct.pack("256s", ifname[:15].encode())
    return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])  # SIOCGIFADDR
  except Exception:
    return None


def local_ip() -> str:
  for ifname in ("wlan0", "wlan1"):
    ip = _iface_ip(ifname)
    if ip and not ip.startswith("127."):
      return ip
  try:                       # default-route fallback
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 53))
    ip = s.getsockname()[0]
    s.close()
    return ip
  except Exception:
    return "0.0.0.0"


# ---- wire helpers -----------------------------------------------------------

def _encode_name(name: str) -> bytes:
  out = b""
  for label in name.rstrip(".").split("."):
    b = label.encode("utf-8")
    out += bytes([len(b)]) + b
  return out + b"\x00"


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
  labels = []
  jumped = False
  end = offset
  while True:
    if offset >= len(data):
      break
    length = data[offset]
    if length & 0xC0 == 0xC0:                 # compression pointer
      ptr = ((length & 0x3F) << 8) | data[offset + 1]
      if not jumped:
        end = offset + 2
      offset = ptr
      jumped = True
      continue
    offset += 1
    if length == 0:
      break
    labels.append(data[offset:offset + length].decode("utf-8", "replace"))
    offset += length
  if not jumped:
    end = offset
  return ".".join(labels) + ".", end


def _rr(name: str, rtype: int, rdata: bytes, cache_flush: bool, ttl: int = _TTL) -> bytes:
  rclass = 0x8001 if cache_flush else 0x0001  # IN, with cache-flush bit for unique records
  return _encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def _txt_rdata(txt: dict) -> bytes:
  out = b""
  for k, v in txt.items():
    item = f"{k}={v}".encode()[:255]
    out += bytes([len(item)]) + item
  return out or b"\x00"


# ---- stdlib responder -------------------------------------------------------

class _StdlibResponder:
  def __init__(self, port: int, txt: dict, instance: str):
    self.port = port
    self.txt = txt
    self.service = SERVICE_TYPE                       # _sunnyconf._tcp.local.
    self.instance = f"{instance}.{SERVICE_TYPE}"      # comma4._sunnyconf._tcp.local.
    self.host = f"{instance}.local."
    self._sock: socket.socket | None = None
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None

  def _answer_packet(self) -> bytes:
    ip = local_ip()
    answers = [
      _rr(self.service, _PTR, _encode_name(self.instance), cache_flush=False),
      _rr(self.instance, _SRV, struct.pack("!HHH", 0, 0, self.port) + _encode_name(self.host), cache_flush=True),
      _rr(self.instance, _TXT, _txt_rdata(self.txt), cache_flush=True),
      _rr(self.host, _A, socket.inet_aton(ip), cache_flush=True),
    ]
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, len(answers), 0, 0)  # QR=1, AA=1
    return header + b"".join(answers)

  def _services_enum_packet(self) -> bytes:
    rr = _rr("_services._dns-sd._udp.local.", _PTR, _encode_name(self.service), cache_flush=False)
    return struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + rr

  def _matches(self, qname: str) -> bool:
    q = qname.lower()
    return q in (self.service.lower(), self.instance.lower(), self.host.lower())

  def _loop(self):
    sock = self._sock
    last_announce = 0.0
    while not self._stop.is_set():
      # periodic unsolicited announcement
      now = time.monotonic()
      if now - last_announce > 30:
        self._send(self._answer_packet())
        last_announce = now
      try:
        sock.settimeout(1.0)
        data, addr = sock.recvfrom(9000)
      except TimeoutError:
        continue
      except OSError:
        return                 # socket died (e.g. interface dropped) -> let _run re-setup
      try:
        self._handle_query(data)
      except Exception:
        pass

  def _handle_query(self, data: bytes):
    if len(data) < 12:
      return
    qdcount = struct.unpack("!H", data[4:6])[0]
    flags = struct.unpack("!H", data[2:4])[0]
    if flags & 0x8000:        # ignore responses
      return
    offset = 12
    wants_service = wants_enum = False
    for _ in range(qdcount):
      name, offset = _decode_name(data, offset)
      if offset + 4 > len(data):
        return
      offset += 4            # qtype + qclass
      n = name.lower()
      if self._matches(n):
        wants_service = True
      if n == "_services._dns-sd._udp.local.":
        wants_enum = True
    if wants_service:
      self._send(self._answer_packet())
    if wants_enum:
      self._send(self._services_enum_packet())

  def _send(self, packet: bytes):
    try:
      self._sock.sendto(packet, (_MCAST_ADDR, _MCAST_PORT))
    except OSError:
      pass

  def _try_setup(self) -> socket.socket | None:
    try:
      sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
      except (AttributeError, OSError):
        pass
      sock.bind(("", _MCAST_PORT))
      mreq = struct.pack("4s4s", socket.inet_aton(_MCAST_ADDR), socket.inet_aton("0.0.0.0"))
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
      sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
      return sock
    except OSError as e:
      cloudlog.debug(f"sunnyconf: mDNS setup not ready ({e}); will retry")
      return None

  def _run(self):
    # retry setup so we survive wlan0 coming up late at boot, or a mid-run interface drop
    announced = False
    while not self._stop.is_set():
      sock = self._try_setup()
      if sock is None:
        if self._stop.wait(10):
          return
        continue
      self._sock = sock
      if not announced:
        cloudlog.info(f"sunnyconf: mDNS responder advertising {self.instance} (stdlib)")
        announced = True
      self._loop()             # returns on socket error or stop
      try:
        sock.close()
      except OSError:
        pass

  def start(self):
    self._thread = threading.Thread(target=self._run, name="sunnyconf_mdns", daemon=True)
    self._thread.start()

  def stop(self):
    self._stop.set()
    if self._sock:
      try:
        self._sock.close()
      except OSError:
        pass


class _ZeroconfBackend:
  """Optional: used only if python-zeroconf is importable (dev convenience)."""
  def __init__(self, port: int, txt: dict, instance: str):
    self.port, self.txt, self.instance = port, txt, instance
    self._zc = None
    self._info = None

  def start(self):
    from zeroconf import Zeroconf, ServiceInfo
    ip = local_ip()
    self._zc = Zeroconf()
    self._info = ServiceInfo(
      SERVICE_TYPE,
      f"{self.instance}.{SERVICE_TYPE}",
      addresses=[socket.inet_aton(ip)],
      port=self.port,
      properties={k: str(v) for k, v in self.txt.items()},
      server=f"{self.instance}.local.",
    )
    self._zc.register_service(self._info)
    cloudlog.info(f"sunnyconf: mDNS responder advertising {self.instance} (zeroconf)")

  def stop(self):
    try:
      if self._zc and self._info:
        self._zc.unregister_service(self._info)
      if self._zc:
        self._zc.close()
    except Exception:
      pass


def start_discovery(port: int = DEFAULT_PORT, txt: dict | None = None, instance: str = SERVICE_INSTANCE):
  txt = txt or {}
  try:
    import zeroconf  # noqa: F401
    backend = _ZeroconfBackend(port, txt, instance)
  except Exception:
    backend = _StdlibResponder(port, txt, instance)
  try:
    backend.start()
    return backend
  except Exception:
    cloudlog.exception("sunnyconf: mDNS announcement failed (continuing without discovery)")
    return None
