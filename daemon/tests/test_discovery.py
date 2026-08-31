"""mDNS wire-format tests for the stdlib responder (no sockets touched)."""
import struct

from openpilot.sunnyconf.daemon import discovery


def test_name_roundtrip():
  for n in ("comma4._sunnyconf._tcp.local.", "_services._dns-sd._udp.local.", "comma4.local."):
    decoded, _ = discovery._decode_name(discovery._encode_name(n), 0)
    assert decoded == n


def test_answer_packet():
  r = discovery._StdlibResponder(8765, {"schema_version": "1", "device_id": "abc"}, "comma4")
  pkt = r._answer_packet()
  assert struct.unpack("!H", pkt[2:4])[0] == 0x8400          # QR=1, AA=1 response
  assert struct.unpack("!H", pkt[6:8])[0] == 4               # PTR + SRV + TXT + A
  name, _ = discovery._decode_name(pkt, 12)
  assert name == "_sunnyconf._tcp.local."


def test_services_enum_packet():
  r = discovery._StdlibResponder(8765, {}, "comma4")
  pkt = r._services_enum_packet()
  name, _ = discovery._decode_name(pkt, 12)
  assert name == "_services._dns-sd._udp.local."


def test_query_parsing_triggers_match():
  r = discovery._StdlibResponder(8765, {}, "comma4")
  # a PTR query for our service type
  q = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + discovery._encode_name("_sunnyconf._tcp.local.") + struct.pack("!HH", 12, 1)
  sent = []
  r._send = lambda pkt: sent.append(pkt)
  r._handle_query(q)
  assert len(sent) == 1 and struct.unpack("!H", sent[0][6:8])[0] == 4
