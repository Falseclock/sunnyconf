#!/usr/bin/env python3
"""Читает JPEG-поток minicap с проброшенного порта и складывает кадры на диск.

Останавливается по Ctrl+C: дописывает stamps.txt и выходит с кодом 0,
чтобы вызывающий скрипт мог собрать mp4.
"""
import os
import socket
import struct
import sys
import time

# Виндовый python по умолчанию пишет в cp1252 и падает на кириллице.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

port = int(sys.argv[1]) if len(sys.argv) > 1 else 1313
outdir = sys.argv[2] if len(sys.argv) > 2 else "frames"
os.makedirs(outdir, exist_ok=True)


def readn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("minicap закрыл поток")
        buf += chunk
    return buf


def connect():
    """adb forward принимает соединение даже когда minicap ещё не слушает сокет,
    и тут же его закрывает — поэтому ретраим до успешного чтения баннера."""
    deadline = time.time() + 15
    while time.time() < deadline:
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.settimeout(5)
            head = readn(sock, 2)
            rest = readn(sock, head[1] - 2)
            sock.settimeout(None)
            return sock, rest
        except (OSError, EOFError):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            time.sleep(0.25)
    return None, None


sock, rest = connect()
if sock is None:
    print("не удалось получить поток minicap на порту %d" % port)
    sys.exit(1)

pid, rw, rh, vw, vh = struct.unpack("<Iiiii", rest[:20])
print("minicap: экран %dx%d, пишем %dx%d" % (rw, rh, vw, vh), flush=True)
print("идёт запись — Ctrl+C чтобы остановить", flush=True)

stamps = []
t0 = time.time()
last_print = 0.0
try:
    while True:
        size = struct.unpack("<I", readn(sock, 4))[0]
        data = readn(sock, size)
        now = time.time() - t0
        with open(os.path.join(outdir, "f%06d.jpg" % len(stamps)), "wb") as fh:
            fh.write(data)
        stamps.append(now)
        if now - last_print > 1.0:
            last_print = now
            sys.stdout.write("\r  кадров: %d   время: %.0f с   " % (len(stamps), now))
            sys.stdout.flush()
except KeyboardInterrupt:
    pass
except (EOFError, OSError) as exc:
    print("\nпоток оборвался: %s" % exc)

with open(os.path.join(outdir, "stamps.txt"), "w") as fh:
    fh.write("\n".join("%.4f" % t for t in stamps))
span = stamps[-1] if stamps else 0.0
print("\nснято кадров: %d за %.1f с (%.1f fps)" % (len(stamps), span, len(stamps) / span if span else 0))
