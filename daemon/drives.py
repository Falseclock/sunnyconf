"""
sunnyconf.drives — recorded-routes index for the app's Drives screen.

The device stores drives as 1-minute segments under /data/media/0/realdata/<route>--<N>/ (qlog.zst,
qcamera.ts, f/e/dcamera.hevc). Everything the screen needs comes from the qlog:
  carState 10Hz (speed), selfdriveState 10Hz (base/engaged/overriding like the connect timeline),
  gpsLocation 0.5Hz (track + TRUE unix time — the device wall clock can be months off in a car, so GPS
  time wins), onroadEvents (alerts with severity flags), one ~5KB JPEG thumbnail per minute (filmstrip).

ONE FILE PER DRIVE (user's design): drives_index/<route>.json =
  {"v", "summary": <the /drives list entry>, "track": <the /track payload>, "segments": {N: seg-summary}}
Each qlog is parsed exactly once; the watcher indexes a segment as soon as loggerd finishes writing it and
folds it straight into its route's file. Thumbnails sit next to it as <route>--<N>.jpg. qcamera.ts files are
H264 MPEG-TS — exactly HLS media segments — so playback is a generated .m3u8 the stock MediaPlayer streams.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from openpilot.common.swaglog import cloudlog
from . import auth

REALDATA = Path("/data/media/0/realdata")
_SEG_RE = re.compile(r"^([0-9a-f]{8}--[0-9a-f]{10})--(\d+)$")
_ROUTE_RE = re.compile(r"^[0-9a-f]{8}--[0-9a-f]{10}$")

_KMH = 3.6
_SUM_V = 4   # rollup format version — bump to make the watcher re-parse everything (4: anchored flag + re-anchor)
_ANCHOR_MIN_UNIX = 1704067200.0   # 2024-01-01: GPS timestamps before this are cold-receiver garbage, never anchors


def cache_dir() -> Path:
  """Rollups live NEXT TO the recordings on the big media partition (/data/media/0/sunnyconf/…), not in
  the small daemon state dir — they are derived data of the recordings. Falls back to the state dir on
  boxes without a media partition (dev machines)."""
  for base in (REALDATA.parent / "sunnyconf" / "drives_index", auth.state_dir() / "drives_index"):
    try:
      base.mkdir(parents=True, exist_ok=True)
      return base
    except OSError:
      continue
  return auth.state_dir()


# -- discovery / paths ---------------------------------------------------------------------------------
def scan_segments() -> list[tuple[str, int, Path]]:
  """All (route, seg#, dir) under realdata, newest route first, segment order within a route."""
  out: list[tuple[str, int, Path]] = []
  try:
    for name in os.listdir(REALDATA):
      m = _SEG_RE.match(name)
      if m:
        out.append((m.group(1), int(m.group(2)), REALDATA / name))
  except OSError:
    return []
  out.sort(key=lambda t: (t[0], t[1]), reverse=True)
  return out


def _route_path(route: str) -> Path:
  return cache_dir() / f"{route}.json"


def thumb_path(route: str, seg: int) -> Path:
  return cache_dir() / f"{route}--{seg}.jpg"


def qcam_path(route: str, seg: int) -> Path:
  return REALDATA / f"{route}--{seg}" / "qcamera.ts"


def _load_route_file(route: str) -> dict | None:
  try:
    data = json.loads(_route_path(route).read_text())
    if isinstance(data, dict) and data.get("v") == _SUM_V and isinstance(data.get("segments"), dict):
      return data
  except (OSError, ValueError):
    pass
  return None


# -- event severity ------------------------------------------------------------------------------------
def _event_kind(e) -> str:
  """Map an OnroadEvent to the connect-style timeline class."""
  name = str(e.name)
  if name == "userFlag":
    return "flag"            # bookmark — the driver marked a moment
  if getattr(e, "immediateDisable", False) or getattr(e, "softDisable", False):
    return "critical"
  if getattr(e, "userDisable", False):
    return "disengage"       # shows as a bar transition, not a tick
  if getattr(e, "warning", False):
    return "warning"         # user prompt
  return "info"


# -- per-segment qlog parse ------------------------------------------------------------------------------
def _parse_segment(route: str, seg: int, seg_dir: Path) -> dict | None:
  """Parse one qlog into a segment summary (and extract its thumbnail). Returns None when unreadable."""
  qlog = seg_dir / "qlog.zst"
  if not qlog.exists():
    qlog = seg_dir / "qlog.bz2"
    if not qlog.exists():
      return None
  from tools.lib.logreader import LogReader   # heavy import, only inside the indexer

  # NOTE on time: logMonoTime is monotonic across the whole ROUTE, and loggerd replays the route-start
  # initData at the head of EVERY segment qlog — so "first message time" is the ROUTE start, not the
  # segment's. Collect route-relative times and re-base onto the segment's first DATA message at the end.
  mono0 = None
  wall_ns = None
  gps_pts: list[list[float]] = []     # [t_rel, lat, lon, v_kmh]
  gps_anchor = None                    # (t_rel, unix_s) from the first fix
  eng: list[list[float]] = []          # [t_rel, state] transitions; 0=base 1=engaged 2=overriding
  events: list[list] = []              # [t_rel, name, kind]
  prev_state = None
  eng_samples = 0
  eng_on = 0
  dist_m = 0.0
  vmax = 0.0
  prev_v = None
  prev_v_t = None
  first_data_tr = None                 # first carState time — the segment's real beginning
  thumb = None
  prev_names: set[str] = set()   # onroadEvents re-lists ACTIVE events every message — transitions only

  try:
    for m in LogReader(str(qlog)):
      t = m.logMonoTime
      if mono0 is None:
        mono0 = t
      tr = (t - mono0) / 1e9
      w = m.which()
      if w == "initData":
        wall_ns = m.initData.wallTimeNanos
      elif w == "carState":
        v = m.carState.vEgo
        if first_data_tr is None:
          first_data_tr = tr
        if prev_v is not None and 0 < tr - prev_v_t < 2.0:
          dist_m += max(0.0, prev_v) * (tr - prev_v_t)
        prev_v, prev_v_t = v, tr
        if v * _KMH > vmax:
          vmax = v * _KMH
      elif w == "selfdriveState":
        # tri-state like the connect timeline: base / engaged / overriding (driver pressing through)
        st_name = str(m.selfdriveState.state)
        st = 2 if st_name == "overriding" else (1 if bool(m.selfdriveState.enabled) else 0)
        eng_samples += 1
        if st != 0:
          eng_on += 1
        if st != prev_state:
          eng.append([round(tr, 2), st])
          prev_state = st
      elif w in ("gpsLocation", "gpsLocationExternal"):
        g = getattr(m, w)
        if g.latitude != 0 or g.longitude != 0:
          gps_pts.append([round(tr, 2), round(g.latitude, 6), round(g.longitude, 6), round(g.speed * _KMH, 1)])
          # A cold GPS receiver can report a nonzero-but-bogus timestamp (seen: 2015) before its first
          # real time fix — only timestamps after the plausibility floor may anchor the segment. A later
          # message in the same segment with a sane time still gets to anchor it.
          if gps_anchor is None and g.unixTimestampMillis / 1e3 >= _ANCHOR_MIN_UNIX:
            gps_anchor = (tr, g.unixTimestampMillis / 1e3)
      elif w == "onroadEvents":
        names = set()
        for e in m.onroadEvents:
          name = str(e.name)
          names.add(name)
          if name not in prev_names:   # newly raised event, not a re-listing
            events.append([round(tr, 2), name, _event_kind(e)])
        prev_names = names
      elif w == "thumbnail" and thumb is None:
        thumb = bytes(m.thumbnail.thumbnail)
  except Exception:
    cloudlog.exception(f"sunnyconf.drives: qlog parse failed {qlog}")
    return None

  # re-base all times onto the segment's first data message (see the NOTE above)
  base_cands = [t for t in (first_data_tr,
                            gps_pts[0][0] if gps_pts else None,
                            eng[0][0] if eng else None,
                            events[0][0] if events else None) if t is not None]
  base = min(base_cands) if base_cands else 0.0
  for row in gps_pts:
    row[0] = round(row[0] - base, 2)
  for row in eng:
    row[0] = round(row[0] - base, 2)
  for row in events:
    row[0] = round(row[0] - base, 2)

  # start time: GPS is authoritative (car wall clocks drift), then initData, then file mtime
  if gps_anchor is not None:
    start_unix = gps_anchor[1] - (gps_anchor[0] - base)
  elif wall_ns:
    start_unix = wall_ns / 1e9 + base
  else:
    try:
      start_unix = qlog.stat().st_mtime - 60
    except OSError:
      start_unix = 0

  dur = 60.0
  if prev_v_t is not None:
    dur = max(1.0, min(90.0, prev_v_t - base))

  if thumb is not None:
    try:
      thumb_path(route, seg).write_bytes(thumb)
    except OSError:
      thumb = None

  return {
    "seg": seg,
    "start_unix": round(start_unix, 2),
    "anchored": gps_anchor is not None,   # True = trustworthy GPS time; False = wall-clock/mtime fallback
    "dur_s": round(dur, 1),
    "dist_km": round(dist_m / 1000.0, 3),
    "vmax_kmh": round(vmax, 1),
    "engaged_ratio": round(eng_on / eng_samples, 3) if eng_samples else 0.0,
    "eng": eng,
    "pts": gps_pts,
    "events": events,
    "thumb": thumb is not None,
  }


# -- rollup building -------------------------------------------------------------------------------------
def _bar_and_events(segs: list[dict], t0: float, t1: float) -> tuple[list[dict], list[dict]]:
  """Normalized engagement intervals (state 0/1/2) + event markers across a route. `info` is dropped
  (pure noise: 355/381 on a real drive)."""
  span = max(1.0, t1 - t0)
  timeline: list[tuple[float, int]] = []
  for s in segs:
    for tr, st in s["eng"]:
      timeline.append((s["start_unix"] + tr, st))
  timeline.sort()
  bar = []
  prev_t, prev_st = t0, 0
  for tt, st in timeline:
    if st != prev_st:
      if tt > prev_t:
        bar.append({"w": round((tt - prev_t) / span, 4), "s": prev_st})
      prev_t, prev_st = tt, st
  bar.append({"w": round((t1 - prev_t) / span, 4), "s": prev_st})
  events = []
  for s in segs:
    for tr, name, kind in s["events"]:
      if kind != "info":
        events.append({"frac": round((s["start_unix"] + tr - t0) / span, 4), "name": name, "kind": kind})
  return bar, events


def _summary_from_segs(route: str, segs: list[dict]) -> dict:
  start = min(s["start_unix"] for s in segs if s["start_unix"]) if segs else 0
  end = max(s["start_unix"] + s["dur_s"] for s in segs)
  dur_s = sum(s["dur_s"] for s in segs)
  km = sum(s["dist_km"] for s in segs)
  engaged = sum(s["engaged_ratio"] * s["dur_s"] for s in segs) / dur_s if dur_s else 0
  bar, ev_list = _bar_and_events(segs, start, end)
  kinds: dict[str, int] = {}
  for e in ev_list:
    kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
  first_pt = next((s["pts"][0] for s in segs if s["pts"]), None)
  last_pt = next((s["pts"][-1] for s in reversed(segs) if s["pts"]), None)
  return {
    "route": route,
    "start_unix": round(start, 1),
    "end_unix": round(end, 1),
    "minutes": round(max(end - start, dur_s) / 60.0, 1),   # wall span (honest with missing segments)
    "dist_km": round(km, 1),
    "vmax_kmh": max(s["vmax_kmh"] for s in segs),
    "engaged_pct": round(engaged * 100.0, 1),
    "events": kinds,
    "bar": bar,
    "event_marks": ev_list,
    "segs": [s["seg"] for s in segs],
    "start_latlon": first_pt[1:3] if first_pt else None,
    "end_latlon": last_pt[1:3] if last_pt else None,
    "thumbs": [s["seg"] for s in segs if s.get("thumb")],
  }


def _track_from_segs(route: str, segs: list[dict]) -> dict:
  t0 = min(s["start_unix"] for s in segs)
  t1 = max(s["start_unix"] + s["dur_s"] for s in segs)
  timeline: list[tuple[float, int]] = []
  for s in segs:
    for tr, st in s["eng"]:
      timeline.append((s["start_unix"] + tr, st))
  timeline.sort()

  def state_at(t: float) -> int:
    st = 0
    for tt, e in timeline:
      if tt <= t:
        st = e
      else:
        break
    return st

  pts = []
  for s in segs:
    for tr, lat, lon, v in s["pts"]:
      t = s["start_unix"] + tr
      pts.append([round(t - t0, 1), lat, lon, v, state_at(t)])
  bar, events = _bar_and_events(segs, t0, t1)
  return {"route": route, "start_unix": round(t0, 1), "dur_s": round(max(1.0, t1 - t0), 1),
          "pts": pts, "bar": bar, "events": events}


_roll_lock = threading.Lock()


def _reanchor(seg_list: list[dict]) -> None:
  """Repair clock-skewed segment times using GPS-anchored siblings. A car device cold-boots with an RTC
  that can be MONTHS off; NTP lands minutes later, and the first (garage) segments have no GPS fix either —
  their fallback times date the whole route into the past (seen: a live drive listed under March, elapsed
  "3404 h"). Segments are strictly 60s apart, so anchored segments fix the route's timeline.

  The anchors themselves need vetting: a cold GPS receiver can emit a bogus-but-nonzero timestamp (seen: a
  segment "anchored" to 2015 stretched a live drive to 94911 h). Every anchored segment votes with its
  implied route start (anchor - seg*60); the largest cluster of votes agreeing within 5 min wins, and
  anchors OUTSIDE the winning cluster are treated like un-anchored segments and re-based too."""
  votes = [(s, s["start_unix"] - s["seg"] * 60.0) for s in seg_list if s.get("anchored")]
  if not votes:
    return
  best_t0, best_backers = None, -1
  for _, t in votes:
    backers = sum(1 for _, u in votes if abs(u - t) <= 300.0)
    # ties go to the LATER epoch: bogus cold-receiver times replay the past, never the future
    if backers > best_backers or (backers == best_backers and best_t0 is not None and t > best_t0):
      best_t0, best_backers = t, backers
  cluster = [t for _, t in votes if abs(t - best_t0) <= 300.0]
  t0 = sorted(cluster)[len(cluster) // 2]
  good = {id(s) for s, t in votes if abs(t - best_t0) <= 300.0}
  for s in seg_list:
    if id(s) not in good:
      s["start_unix"] = round(t0 + s["seg"] * 60.0, 2)


def _update_route_file(route: str, new_seg: dict | None = None, drop_segs: set[int] | None = None) -> None:
  """Read-modify-write one drive's rollup: fold in a freshly parsed segment and/or drop rotated ones,
  then rebuild the summary + track sections. No segments left -> the rollup is removed."""
  with _roll_lock:
    data = _load_route_file(route) or {"v": _SUM_V, "segments": {}}
    segments: dict = data["segments"]
    if new_seg is not None:
      segments[str(new_seg["seg"])] = new_seg
    for n in (drop_segs or ()):
      segments.pop(str(n), None)
    if not segments:
      try:
        _route_path(route).unlink()
      except OSError:
        pass
      return
    seg_list = sorted(segments.values(), key=lambda s: s["seg"])
    _reanchor(seg_list)
    data["summary"] = _summary_from_segs(route, seg_list)
    data["track"] = _track_from_segs(route, seg_list)
    data["updated"] = round(time.time(), 1)
    tmp = _route_path(route).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(_route_path(route))


# -- the watcher-indexer -----------------------------------------------------------------------------------
_lock = threading.Lock()
_indexing = False
_progress = {"total": 0, "indexed": 0, "running": False}
_have: dict[str, set[int]] = {}   # route -> indexed segment numbers (RAM mirror of the rollups)

_SETTLE_S = 10.0        # a segment's qlog must be this stable before we treat it as finalized
_WATCH_PERIOD_S = 20.0  # how often the watcher rescans for newly-finished segments
_ONROAD_BATCH = 3       # while driving, index at most this many backlog segments per tick (keep CPU calm)
_OFFROAD_BATCH = 25     # parked: still cap the tick — a huge backlog must not run as ONE monster pass,
                        # or segments that finish DURING it (the live drive!) wait for the whole thing;
                        # capped ticks rescan often and the newest-first sort lets fresh segments jump in


def _onroad() -> bool:
  try:
    from openpilot.common.params import Params
    return bool(Params().get_bool("IsOnroad"))
  except Exception:
    return False


def _finalized(seg_dir: Path) -> bool:
  """A segment is done being written once loggerd has flushed its qlog and stopped touching it. The
  in-progress (current) segment has no qlog yet, so it's naturally skipped until it finishes."""
  q = seg_dir / "qlog.zst"
  if not q.exists():
    q = seg_dir / "qlog.bz2"
    if not q.exists():
      return False
  try:
    return (time.time() - q.stat().st_mtime) > _SETTLE_S
  except OSError:
    return False


def _bootstrap_have() -> None:
  """RAM index of what's already in the rollups; also purges files of retired formats/layouts
  (old per-segment *.json caches, rollups from older parser versions) so they get re-indexed."""
  _have.clear()
  purged = 0
  for f in cache_dir().glob("*.json"):
    if _SEG_RE.match(f.stem):        # retired layout: standalone per-segment summaries
      try:
        f.unlink()
        purged += 1
      except OSError:
        pass
      continue
    if not _ROUTE_RE.match(f.stem):
      continue
    data = _load_route_file(f.stem)  # validates the format version
    if data is None:
      try:
        f.unlink()
        purged += 1
      except OSError:
        pass
      continue
    _have[f.stem] = {int(k) for k in data["segments"]}
  if purged:
    cloudlog.info(f"sunnyconf.drives: purged {purged} outdated cache files")


def _cleanup_rotated() -> None:
  """Recordings rotate away as the disk fills; drop their segments from the rollups too."""
  for route, segs in list(_have.items()):
    gone = {n for n in segs if not (REALDATA / f"{route}--{n}").exists()}
    if not gone:
      continue
    _update_route_file(route, drop_segs=gone)
    for n in gone:
      thumb_path(route, n).unlink(missing_ok=True)
    segs -= gone
    if not segs:
      _have.pop(route, None)
    cloudlog.info(f"sunnyconf.drives: {route}: dropped {len(gone)} rotated segments")


def _watch_loop():
  """Persistent watcher: index each segment AS SOON AS it finishes recording (user's design), folding it
  straight into its route's rollup. One qlog parse (~0.6s) per finished minute is negligible; a first-run
  backlog is throttled (small sleep + onroad cap) to avoid a CPU spike."""
  _bootstrap_have()
  while True:
    leftover = 0
    try:
      _cleanup_rotated()
      segs = scan_segments()
      todo = [(r, n, p) for r, n, p in segs if n not in _have.get(r, set()) and _finalized(p)]
      with _lock:
        _progress.update(total=len(segs), indexed=len(segs) - len(todo), running=bool(todo))
      onroad = _onroad()
      done = 0
      for r, n, p in todo:               # newest first (scan order)
        s = _parse_segment(r, n, p)
        if s is not None:
          _update_route_file(r, new_seg=s)
          _have.setdefault(r, set()).add(n)
        done += 1
        with _lock:
          _progress["indexed"] += 1
        onroad = onroad or _onroad()     # a drive can START mid-backlog — flip to the gentle cap live
        if done >= (_ONROAD_BATCH if onroad else _OFFROAD_BATCH):
          break                          # spread the backlog across ticks; rescan picks fresh segs first
        time.sleep(0.3)                  # gentle: don't hog a core on the backlog
      leftover = len(todo) - done
      with _lock:
        _progress["running"] = leftover > 0
    except Exception:
      cloudlog.exception("sunnyconf.drives: watcher tick failed")
    # with a backlog pending, keep ticks coming while parked; onroad stays on the calm 20s cadence
    time.sleep(_WATCH_PERIOD_S if (_onroad() or leftover == 0) else 2.0)


def ensure_indexing() -> dict:
  """Start the persistent watcher once; return current progress. Idempotent — safe to call per /drives."""
  global _indexing
  with _lock:
    if not _indexing:
      _indexing = True
      threading.Thread(target=_watch_loop, name="drives_indexer", daemon=True).start()
    return dict(_progress)


# -- endpoints' data -----------------------------------------------------------------------------------
def route_summaries() -> list[dict]:
  """The /drives list — the summary section of each drive's rollup, newest first."""
  out = []
  for f in cache_dir().glob("*.json"):
    if not _ROUTE_RE.match(f.stem):
      continue
    data = _load_route_file(f.stem)
    if data and "summary" in data:
      out.append(data["summary"])
  out.sort(key=lambda r: r["start_unix"], reverse=True)
  return out


def route_track(route: str) -> dict | None:
  """The app-ready track for one drive — straight from its rollup."""
  data = _load_route_file(route)
  return data.get("track") if data else None


def delete_route(route: str) -> dict:
  """Remove a route's recordings + cache (the app's per-drive delete; frees ~120MB per minute of drive).
  Refuses while any of its segments is still being written."""
  import shutil
  seg_dirs = sorted(REALDATA.glob(f"{route}--*"))
  for d in seg_dirs:
    if not _finalized(d) and (d / "qlog.zst").exists() is False and any(d.iterdir()):
      return {"ok": False, "error": "recording"}
  removed = 0
  for d in seg_dirs:
    try:
      shutil.rmtree(d)
      removed += 1
    except OSError:
      pass
  try:
    _route_path(route).unlink()
  except OSError:
    pass
  for f in cache_dir().glob(f"{route}--*.jpg"):
    try:
      f.unlink()
    except OSError:
      pass
  _have.pop(route, None)
  return {"ok": True, "route": route, "segments_removed": removed}


_live_first_seen: dict[str, tuple[float, float]] = {}   # route -> (wall time, monotonic) at first sighting


def live_status(onroad: bool = False) -> dict:
  """The LIVE card's data: is a drive being recorded now, which route, since when, distance so far (from
  the already-indexed part — trails reality by up to a minute). Requires onroad: loggerd only records
  while driving, and a fresh dir mtime alone can be someone copying files onto the device."""
  if not onroad:
    return {"recording": False}
  newest = None
  for name in os.listdir(REALDATA) if REALDATA.exists() else []:
    m = _SEG_RE.match(name)
    if not m:
      continue
    try:
      mt = (REALDATA / name).stat().st_mtime
    except OSError:
      continue
    if newest is None or mt > newest[0]:
      newest = (mt, m.group(1))
  if newest is None or (time.time() - newest[0]) > 120:
    return {"recording": False}
  route = newest[1]
  # Before the first segment is indexed there's no GPS-anchored start yet, and the dir mtime keeps moving
  # while loggerd writes — so elapsed would sit near 0 for the first ~90s. Remember when we FIRST saw the
  # route instead; the indexed summary takes over as soon as it exists.
  now = time.time()
  first_wall, first_mono = _live_first_seen.setdefault(route, (now, time.monotonic()))
  data = _load_route_file(route)
  summary = data.get("summary") if data else None
  started = summary["start_unix"] if summary else first_wall
  elapsed = now - started
  # Clock-skew guard: a car device cold-boots with an RTC months off (NTP lands minutes later), so the
  # summary/dir-derived start can be absurd (seen: "3404 h"). A LIVE drive is hours old at most — outside
  # that, fall back to the monotonic clock (immune to wall-clock jumps) and back-derive the start.
  if not (0.0 <= elapsed < 24 * 3600):
    elapsed = time.monotonic() - first_mono
    started = now - elapsed
  return {
    "recording": True,
    "route": route,
    "started_unix": round(started, 1),
    "elapsed_s": round(max(0.0, elapsed), 0),
    "dist_km": summary["dist_km"] if summary else 0.0,
    "vmax_kmh": summary["vmax_kmh"] if summary else 0.0,
    "indexed_segs": len(summary["segs"]) if summary else 0,
  }


# -- playback -------------------------------------------------------------------------------------------
# Two shapes, chosen to keep CPU/IO/disk sane (user's concern about concatenating huge video):
#   ts (qcamera.ts, the default view): served PER SEGMENT via remux_mp4 (stream-copy to faststart MP4).
#     It's small (~2.7MB/min H264) so the build is cheap, and one file = one MediaPlayer with native seek
#     and NO per-minute segment swaps (those swaps caused the boundary freeze + wrong-frame flash).
#   HD (f/e/dcamera.hevc): NOT concatenated (a whole HD drive is ~37MB/min → gigabytes). Served PER SEGMENT
#     on demand; HD is for inspecting a moment, so a brief reload at a minute boundary is fine.
# Audio (AAC) exists only in qcamera.ts; it's grafted onto the HD segments too. Stream-copy only (no
# transcode) via the openpilot venv's PyAV (AGNOS has no ffmpeg); +faststart so the moov leads the file.
_CAM_FILES = {"q": "qcamera.ts", "road": "fcamera.hevc", "wide": "ecamera.hevc", "driver": "dcamera.hevc"}
_remux_lock = threading.Lock()


def _cam_cache_dir() -> Path:
  p = cache_dir().parent / "cam_cache"
  p.mkdir(parents=True, exist_ok=True)
  return p


def _cam_segs(route: str, cam: str) -> list[int]:
  """Segment numbers of a route that have the camera's file, in play order."""
  fname = _CAM_FILES.get(cam)
  out = []
  for d in REALDATA.glob(f"{route}--*"):
    m = re.match(rf"^{re.escape(route)}--(\d+)$", d.name)
    if m and fname and (d / fname).exists():
      out.append(int(m.group(1)))
  out.sort()
  return out


def _trim_cam_cache(cap_bytes: int = 2 * 1024 ** 3) -> None:
  """Size-aware LRU over the cam cache (mp4s vary from small ts concats to big HD segments)."""
  try:
    files = sorted(_cam_cache_dir().glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > cap_bytes:
      f = files.pop(0)
      total -= f.stat().st_size
      f.unlink(missing_ok=True)
  except OSError:
    pass


def _mux_video(out, ovideo, vin, vist, raw: float, base_pts: int) -> None:
  """Copy one input's video packets to `ovideo`, rebased so this segment starts at `base_pts` (in the
  output video time_base). Raw HEVC has no timestamps → synthesise 20 fps."""
  vtb = vist.time_base
  step = int(1 / (20 * float(vtb))) if vtb else 4500
  vi, vfirst = 0, None
  for p in vin.demux(vist):
    if p.size == 0:
      continue
    if raw:
      p.pts = base_pts + vi * step
      p.dts = p.pts
      vi += 1
    else:
      ref = p.dts if p.dts is not None else p.pts
      if ref is None:
        continue
      if vfirst is None:
        vfirst = ref
      p.pts = base_pts + (ref - vfirst)
      p.dts = p.pts
    p.stream = ovideo
    out.mux(p)


def _mux_audio(out, oaudio, ain, aist, base_pts: int) -> None:
  atb = aist.time_base
  afirst = None
  for p in ain.demux(aist):
    if p.size == 0:
      continue
    ref = p.dts if p.dts is not None else p.pts
    if ref is None:
      continue
    if afirst is None:
      afirst = ref
    p.pts = base_pts + (ref - afirst)
    p.dts = p.pts
    p.stream = oaudio
    out.mux(p)


def remux_mp4(route: str, seg: int, cam: str) -> Path | None:
  """ONE HD segment → faststart MP4 (HEVC video + grafted qcamera AAC audio). On demand; cached."""
  fname = _CAM_FILES.get(cam)
  if not fname:
    return None
  src = REALDATA / f"{route}--{seg}" / fname
  if not src.exists():
    return None
  dst = _cam_cache_dir() / f"{route}--{seg}--{cam}.mp4"
  if dst.exists():
    return dst
  with _remux_lock:
    if dst.exists():
      return dst
    t0 = time.monotonic()
    raw = fname.endswith(".hevc")
    tmp = dst.with_suffix(".tmp")
    qpath = REALDATA / f"{route}--{seg}" / "qcamera.ts"
    try:
      import av
      vin = av.open(str(src), format="hevc" if raw else None)
      ain = None
      try:
        try:
          ain = av.open(str(qpath))
          aist = ain.streams.audio[0] if ain.streams.audio else None
        except Exception:
          aist = None
        out = av.open(str(tmp), mode="w", format="mp4", options={"movflags": "+faststart"})
        vist = vin.streams.video[0]
        ovideo = out.add_stream_from_template(vist)
        oaudio = out.add_stream_from_template(aist) if aist is not None else None
        _mux_video(out, ovideo, vin, vist, raw, 0)
        if oaudio is not None:
          _mux_audio(out, oaudio, ain, aist, 0)
        out.close()
      finally:
        vin.close()
        if ain is not None:
          ain.close()
      tmp.replace(dst)
      cloudlog.info(f"sunnyconf.drives: remuxed {route}--{seg} {cam} in {time.monotonic() - t0:.1f}s")
    except Exception:
      cloudlog.exception(f"sunnyconf.drives: remux failed {src}")
      return None
  _trim_cam_cache()
  return dst


def prefetch_next(route: str, seg: int, cam: str) -> None:
  """Warm the next HD segment so straight-through HD viewing doesn't stall (fire-and-forget)."""
  def work():
    try:
      remux_mp4(route, seg + 1, cam)
    except Exception:
      pass
  threading.Thread(target=work, name="cam_prefetch", daemon=True).start()


# -- clip export (save an arbitrary [t0,t1] slice of the virtual timeline) ---------------------------------
MAX_CLIP_MS = 180_000   # cap an exported clip to 3 min — bounded CPU/disk (clips are short by design)


def clip_meta(route: str, cam: str, start_ms: int, end_ms: int) -> dict:
  """Friendly download filename from the drive's wall-clock start (GPS-anchored, from the rollup)."""
  base = 0
  rf = _load_route_file(route)
  if rf:
    try:
      base = int((rf.get("summary") or {}).get("start_unix") or 0)
    except (TypeError, ValueError):
      base = 0
  if base:
    day = time.strftime("%Y%m%d", time.localtime(base))
    a = time.strftime("%H%M%S", time.localtime(base + start_ms // 1000))
    b = time.strftime("%H%M%S", time.localtime(base + end_ms // 1000))
  else:
    day, a, b = "clip", f"{start_ms // 1000:06d}", f"{end_ms // 1000:06d}"
  return {"filename": f"sunnyconf_{day}_{a}-{b}_{cam}.mp4"}


def _seg_ms(p, raw: bool, tb: float, state: dict) -> float | None:
  """Time of a demuxed packet within its segment, in ms. Raw HEVC has no pts -> synthesise 20 fps by index;
  otherwise rebase to the segment's first packet. `state` carries per-segment counters ('i', 'first')."""
  if raw:
    ms = state["i"] * 50.0
    state["i"] += 1
    return ms
  ref = p.dts if p.dts is not None else p.pts
  if ref is None:
    return None
  if state["first"] is None:
    state["first"] = ref
  return (ref - state["first"]) * tb * 1000.0


def build_clip(route: str, cam: str, start_ms: int, end_ms: int) -> Path | None:
  """Export [start_ms, end_ms] of the VIRTUAL timeline (may span segment boundaries) to a faststart MP4.
  Stream-copy, keyframe-aligned start (every camera is keyframed ~1/s, so <=1s lead-in), qcamera AAC grafted
  onto every camera for sound. Cheap + bounded — a one-shot cut, NOT the continuous concat we avoid during
  playback. Cached + deduped by (route,cam,start,end)."""
  fname = _CAM_FILES.get(cam)
  if not fname:
    return None
  start_ms = max(0, int(start_ms))
  end_ms = int(end_ms)
  if end_ms <= start_ms:
    return None
  if end_ms - start_ms > MAX_CLIP_MS:
    end_ms = start_ms + MAX_CLIP_MS
  # The virtual timeline counts minutes by INDEX into the ordered segment list (== the player's chapters),
  # NOT by segment directory number — routes can start at --26 or have gaps. Map index -> real segment here.
  seg_nums = _cam_segs(route, cam)
  if not seg_nums:
    return None
  idx0 = start_ms // 60_000
  idx1 = min((end_ms - 1) // 60_000, len(seg_nums) - 1)
  if idx0 >= len(seg_nums) or idx0 > idx1:
    return None
  indices = list(range(idx0, idx1 + 1))
  dst = _cam_cache_dir() / f"clip--{route}--{cam}--{start_ms}--{end_ms}.mp4"
  if dst.exists() and dst.stat().st_size > 0:
    return dst
  raw = fname.endswith(".hevc")
  dur_ms = end_ms - start_ms
  with _remux_lock:
    if dst.exists() and dst.stat().st_size > 0:
      return dst
    t0 = time.monotonic()
    tmp = dst.with_suffix(".tmp")
    try:
      import av

      # 1) origin = latest VIDEO keyframe at/behind the requested start (within the first index), so the clip
      #    begins on a keyframe (clean decode). origin_ms is that keyframe's clip-time (<= 0).
      origin_ms = None
      vin0 = av.open(str(REALDATA / f"{route}--{seg_nums[idx0]}" / fname), format="hevc" if raw else None)
      try:
        vist0 = vin0.streams.video[0]
        vtb0 = float(vist0.time_base) if vist0.time_base else (1.0 / 20)
        st = {"i": 0, "first": None}
        for p in vin0.demux(vist0):
          if p.size == 0:
            continue
          ms = _seg_ms(p, raw, vtb0, st)
          if ms is None:
            continue
          t_clip = idx0 * 60_000 + ms - start_ms
          if p.is_keyframe and t_clip <= 0:
            origin_ms = t_clip          # keep advancing to the latest keyframe <= start
          elif t_clip > 0 and origin_ms is not None:
            break
      finally:
        vin0.close()
      if origin_ms is None:
        origin_ms = idx0 * 60_000 - start_ms   # fallback: first index's frame 0

      out = av.open(str(tmp), mode="w", format="mp4", options={"movflags": "+faststart"})
      ovideo = oaudio = None
      vtb = atb = None
      for vi in indices:
        seg = seg_nums[vi]
        vin = av.open(str(REALDATA / f"{route}--{seg}" / fname), format="hevc" if raw else None)
        ain = None
        try:
          try:
            ain = av.open(str(REALDATA / f"{route}--{seg}" / "qcamera.ts"))
            aist = ain.streams.audio[0] if ain.streams.audio else None
          except Exception:
            aist = None
          vist = vin.streams.video[0]
          if ovideo is None:   # declare both output streams from the first segment, before any mux()
            ovideo = out.add_stream_from_template(vist)
            vtb = float(vist.time_base) if vist.time_base else (1.0 / 20)
            if aist is not None:
              oaudio = out.add_stream_from_template(aist)
              atb = float(aist.time_base) if aist.time_base else None
          # video
          st = {"i": 0, "first": None}
          for p in vin.demux(vist):
            if p.size == 0:
              continue
            ms = _seg_ms(p, raw, vtb, st)
            if ms is None:
              continue
            if ms >= 60_000:   # cap to this index's 1-min slot (segments aren't exactly 60.000s; without this
              break            # a long tail overlaps the next segment -> dts goes backwards -> mux EINVAL)
            t_clip = vi * 60_000 + ms - start_ms
            if t_clip > dur_ms:
              break
            out_ms = t_clip - origin_ms
            if out_ms < 0:
              continue
            p.pts = p.dts = int(round((out_ms / 1000.0) / vtb))
            p.stream = ovideo
            out.mux(p)
          # audio (qcamera AAC), rebased the same way
          if oaudio is not None and aist is not None:
            sta = {"i": 0, "first": None}
            for p in ain.demux(aist):
              if p.size == 0:
                continue
              ms = _seg_ms(p, False, atb, sta)
              if ms is None:
                continue
              if ms >= 60_000:   # same 1-min cap as video, keeps A/V joins monotonic across segments
                break
              t_clip = vi * 60_000 + ms - start_ms
              if t_clip > dur_ms:
                break
              out_ms = t_clip - origin_ms
              if out_ms < 0:
                continue
              p.pts = p.dts = int(round((out_ms / 1000.0) / atb))
              p.stream = oaudio
              out.mux(p)
        finally:
          vin.close()
          if ain is not None:
            ain.close()
      out.close()
      tmp.replace(dst)
      cloudlog.info(f"sunnyconf.drives: clip {route} {cam} [{start_ms}-{end_ms}] in {time.monotonic() - t0:.1f}s")
    except Exception:
      cloudlog.exception(f"sunnyconf.drives: clip failed {route} {cam} {start_ms}-{end_ms}")
      try:
        tmp.unlink(missing_ok=True)
      except OSError:
        pass
      return None
  _trim_cam_cache()
  return dst
