#!/usr/bin/env bash
# Запись экрана головного устройства (Android 4.4, рабочего video-энкодера на нём нет).
# Захват: minicap отдаёт JPEG-кадры, кодирование в H.264 делает хост.
#
# Работает и из WSL, и из Git Bash:
#   bash sunnyconf/tools/rec_hu.sh [выходной.mp4]
# Остановка: Ctrl+C. Дальше скрипт сам соберёт mp4.
#
# Настройки через переменные окружения:
#   Q=95     качество JPEG на устройстве (1-100, выше = чётче и медленнее)
#   CRF=16   качество x264 на хосте (меньше = лучше)
#   PROJ=1920x720@1920x720/0   проекция minicap (нативная по умолчанию)
#   PORT=1313

set -u
export MSYS_NO_PATHCONV=1
export PYTHONIOENCODING=utf-8

Q="${Q:-95}"
CRF="${CRF:-16}"
PROJ="${PROJ:-1920x720@1920x720/0}"
PORT="${PORT:-1313}"

# --- окружение: WSL/Linux или Git Bash поверх Windows ---
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) WINBASH=1 ;;
  *)                    WINBASH=0 ;;
esac

PY=""
for cand in python3 python; do
  command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
done
if [ -z "$PY" ]; then echo "не найден python3/python"; exit 1; fi

# Путь в форму, понятную хостовому python (в Git Bash он виндовый и /c/... не понимает).
to_py() { if [ "$WINBASH" = 1 ]; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }
# Путь в форму для WSL (из Git Bash ffmpeg вызывается через wsl.exe).
to_wsl() {
  local p="$1"
  [ "$WINBASH" = 1 ] && p="$(cygpath -u "$p")"
  case "$p" in
    /[A-Za-z]/*) printf '/mnt%s\n' "$p" ;;
    *)           printf '%s\n' "$p" ;;
  esac
}
# ffmpeg/ffprobe: в WSL нативные, из Git Bash — через wsl.exe.
run_ff() {
  local dir="$1"; shift
  if [ "$WINBASH" = 1 ]; then
    wsl -e bash -lc "cd '$(to_wsl "$dir")' && $*"
  else
    ( cd "$dir" && eval "$*" )
  fi
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "$HOME/Downloads" ]; then outdir_default="$HOME/Downloads"; else outdir_default="$HOME"; fi
out="${1:-$outdir_default/hu_rec_$(date +%Y%m%d_%H%M%S).mp4}"
frames="${HU_REC_TMP:-$HOME/.hu_rec_frames}"

cleanup() {
  adb shell "ps | grep minicap" 2>/dev/null | grep -v grep | awk '{print $2}' | while read -r p; do
    [ -n "$p" ] && adb shell "kill $p" >/dev/null 2>&1
  done
  adb forward --remove "tcp:$PORT" >/dev/null 2>&1
}

if ! adb get-state >/dev/null 2>&1; then
  echo "устройство не подключено (adb devices пустой)"; exit 1
fi
if ! adb shell "ls /data/local/tmp/minicap" 2>/dev/null | grep -q minicap; then
  echo "на устройстве нет /data/local/tmp/minicap — сначала залей minicap и minicap.so"; exit 1
fi

cleanup
rm -rf "$frames"; mkdir -p "$frames"

adb shell "LD_LIBRARY_PATH=/data/local/tmp /data/local/tmp/minicap -P $PROJ -Q $Q" >/dev/null 2>&1 &
minicap_pid=$!
adb forward "tcp:$PORT" localabstract:minicap >/dev/null 2>&1

trap 'echo' INT
"$PY" "$(to_py "$here/rec_hu_grab.py")" "$PORT" "$(to_py "$frames")"
trap - INT

cleanup
kill "$minicap_pid" 2>/dev/null

count=$(ls "$frames"/*.jpg 2>/dev/null | wc -l)
if [ "$count" -lt 2 ]; then
  echo "кадров почти нет ($count) — экран не менялся, minicap шлёт кадры только на изменение"
  exit 1
fi

# Список для concat с реальными длительностями — так сохраняется настоящий тайминг.
"$PY" - "$(to_py "$frames")" <<'PY'
import os, sys
d = sys.argv[1]
st = [float(x) for x in open(os.path.join(d, "stamps.txt")).read().split()]
fr = sorted(f for f in os.listdir(d) if f.endswith(".jpg"))
out = []
for i, f in enumerate(fr):
    dur = (st[i + 1] - st[i]) if i + 1 < len(st) else 0.15
    out.append("file '%s'\nduration %.3f" % (f, max(dur, 0.02)))
out.append("file '%s'" % fr[-1])
open(os.path.join(d, "list.txt"), "w").write("\n".join(out))
PY

if [ "$WINBASH" = 1 ]; then ffout="$(to_wsl "$out")"; else ffout="$out"; fi
echo "кодирую (crf $CRF)…"
run_ff "$frames" "ffmpeg -y -v error -f concat -safe 0 -i list.txt -vsync vfr \
  -c:v libx264 -preset slow -crf $CRF -pix_fmt yuv420p -movflags +faststart '$ffout'"

if [ -f "$out" ]; then
  echo "готово: $out"
  run_ff "$frames" "ffprobe -v error -show_entries format=duration,size:stream=width,height,nb_frames -of default=nw=1 '$ffout'"
else
  echo "ffmpeg не собрал файл; кадры остались в $frames"
fi
