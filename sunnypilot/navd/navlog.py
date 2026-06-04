"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

navlog — unified, non-blocking logger for the mici-nav features, with a MODE flag.

Disk is always the source of truth; offload to the Helsinki collector is best-effort
and never competes with driving.

MODES (file /data/navd/flags/logmode, or env NAVLOG_MODE; default 'opportunistic'):
  off            - logging disabled (zero overhead)
  disk           - append to /data/navd/navlog.jsonl only, never network
  stream         - append to disk AND offload ASAP (disk is the fallback)
  opportunistic  - append to disk; offload only when load is low AND network up
                   (i.e. parked/idle) so it never steals CPU/bandwidth while driving  [DEFAULT]

log(src, **fields) is cheap and never blocks. Heartbeat() gives per-process
cpu%/loop-rate/RSS so we can pin which feature tanks the CPU.
"""
import os
import sys
import glob
import json
import time
import socket
import threading
import subprocess
import urllib.request

HOST = os.environ.get("NAVLOG_HOST", "100.122.84.123")   # Helsinki tailnet IP
PORT = int(os.environ.get("NAVLOG_PORT", "5009"))
URL = f"http://{HOST}:{PORT}/ingest"
DISK = os.environ.get("NAVLOG_DISK", "/data/navd/navlog.jsonl")
OFFSET = DISK + ".offset"
MODE_FILE = os.environ.get("NAVLOG_MODE_FILE", "/data/navd/flags/logmode")
LOAD_MAX = float(os.environ.get("NAVLOG_LOAD_MAX", "5.0"))   # opportunistic offloads below this 1-min load
VALID_MODES = ("off", "disk", "stream", "opportunistic")

# which process is emitting (so the collector can tell navigationd from the UI etc.)
PROC = os.environ.get("NAVLOG_PROC") or (os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "nav")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GIT_CACHE = None
_SYSMON_ON = False
_SYSMON_PERIOD = float(os.environ.get("NAVLOG_SYS_PERIOD", "5.0"))
SESSION_PERIOD = float(os.environ.get("NAVLOG_SESSION_PERIOD", "30.0"))


def _git_info() -> dict:
  """repo / branch / commit / dirty for the running tree, captured once (best-effort).

  This is the 'what code produced this data' stamp — so a stale checkout (device N
  commits behind) is obvious in the stream instead of silently emitting nothing/old data.
  """
  global _GIT_CACHE
  if _GIT_CACHE is not None:
    return _GIT_CACHE

  def _g(args, default=""):
    try:
      return subprocess.run(["git", "-C", _REPO_ROOT, *args], capture_output=True,
                            text=True, timeout=3).stdout.strip()
    except Exception:
      return default

  upstream = _g(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
  remote = upstream.split("/")[0] if "/" in upstream else "origin"
  _GIT_CACHE = {
    "repo": _g(["remote", "get-url", remote]) or "?",
    "branch": _g(["rev-parse", "--abbrev-ref", "HEAD"]) or "?",
    "commit": _g(["rev-parse", "--short=10", "HEAD"]) or "?",
    "upstream": upstream or "?",
    "dirty": bool(_g(["status", "--porcelain"])),
  }
  return _GIT_CACHE


def _sys_sample() -> dict:
  """Device-wide stats: load, memory, /data disk, SoC temperature. Per-pass cpu% is
  added by _SysMon (needs a /proc/stat delta). All fields best-effort / guarded."""
  d = {}
  try:
    la = os.getloadavg()
    d["load1"] = round(la[0], 2)
    d["load5"] = round(la[1], 2)
  except OSError:
    pass
  try:
    mi = {}
    with open("/proc/meminfo") as f:
      for line in f:
        k, _, v = line.partition(":")
        mi[k] = int(v.split()[0])  # kB
    total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
    if total:
      d["mem_total_mb"] = total // 1024
      d["mem_used_mb"] = (total - avail) // 1024
      d["mem_pct"] = round(100.0 * (total - avail) / total, 1)
  except Exception:
    pass
  try:
    st = os.statvfs("/data")
    tot = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    if tot:
      d["disk_used_gb"] = round(used / 1e9, 1)
      d["disk_pct"] = round(100.0 * used / tot, 1)
  except Exception:
    pass
  try:
    temps = []
    for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
      try:
        t = int(open(zone).read().strip())
        temps.append(t / 1000.0 if t > 1000 else float(t))
      except Exception:
        pass
    if temps:
      d["temp_max_c"] = round(max(temps), 1)
  except Exception:
    pass
  return d


def _mode() -> str:
  try:
    m = open(MODE_FILE).read().strip().lower()
    if m in VALID_MODES:
      return m
  except OSError:
    pass
  m = os.environ.get("NAVLOG_MODE", "opportunistic").lower()
  return m if m in VALID_MODES else "opportunistic"


def _net_ok() -> bool:
  try:
    s = socket.create_connection((HOST, PORT), timeout=1.5)
    s.close()
    return True
  except OSError:
    return False


class _NavLog:
  def __init__(self):
    self._buf = []
    self._lock = threading.Lock()
    threading.Thread(target=self._disk_writer, name="navlog_disk", daemon=True).start()
    threading.Thread(target=self._offloader, name="navlog_offload", daemon=True).start()
    threading.Thread(target=self._session_stamper, name="navlog_session", daemon=True).start()

  def _session_stamper(self):
    # Emit the git/code-version stamp immediately and re-emit periodically, so a
    # collector that joins late still learns which commit produced the stream.
    while True:
      if _mode() != "off":
        self.log("session", ev="hello", proc=PROC, **_git_info())
      time.sleep(SESSION_PERIOD)

  def log(self, src, **fields):
    if _mode() == "off":
      return
    rec = {"t": round(time.time(), 3), "src": src}
    rec.update(fields)
    with self._lock:
      self._buf.append(rec)

  def _disk_writer(self):
    while True:
      time.sleep(1.0)
      with self._lock:
        batch, self._buf = self._buf, []
      if not batch or _mode() == "off":
        continue
      try:
        os.makedirs(os.path.dirname(DISK), exist_ok=True)
        with open(DISK, "a") as f:
          for r in batch:
            f.write(json.dumps(r) + "\n")
      except OSError:
        pass

  def _offloader(self):
    while True:
      time.sleep(3.0)
      mode = _mode()
      if mode in ("off", "disk"):
        continue
      if mode == "opportunistic":
        try:
          if os.getloadavg()[0] > LOAD_MAX:
            continue   # busy (likely driving) — wait until idle
        except OSError:
          pass
      if not _net_ok():
        continue
      self._drain()

  def _drain(self):
    try:
      off = int(open(OFFSET).read().strip()) if os.path.exists(OFFSET) else 0
    except (OSError, ValueError):
      off = 0
    try:
      size = os.path.getsize(DISK)
    except OSError:
      return
    if off >= size:
      return
    with open(DISK, "rb") as f:
      f.seek(off)
      chunk = f.read(512 * 1024)            # bounded per pass
      new_off = f.tell()
    try:
      urllib.request.urlopen(
        urllib.request.Request(URL, data=chunk, headers={"Content-Type": "application/x-ndjson"}),
        timeout=4).read()
    except Exception:
      return                                # leave offset; retry next pass
    try:
      open(OFFSET, "w").write(str(new_off))
    except OSError:
      pass


_LOG = None


def get() -> _NavLog:
  global _LOG
  if _LOG is None:
    _LOG = _NavLog()
  return _LOG


def log(src: str, **fields):
  get().log(src, **fields)


class _SysMon(threading.Thread):
  """Device-wide system heartbeat — runs on its own thread so it keeps streaming
  even if a nav loop stalls. Emits src='system': cpu%, load, mem, disk, temp."""
  def __init__(self, period: float):
    super().__init__(name="navlog_sysmon", daemon=True)
    self.period = period
    self._last = self._cpu_ticks()

  @staticmethod
  def _cpu_ticks():
    try:
      with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:]))
      idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
      return sum(vals), idle
    except Exception:
      return None

  def run(self):
    while True:
      time.sleep(self.period)
      if _mode() == "off":
        continue
      d = _sys_sample()
      cur = self._cpu_ticks()
      if cur and self._last and cur[0] > self._last[0]:
        dt, di = cur[0] - self._last[0], cur[1] - self._last[1]
        d["cpu_pct"] = round(100.0 * (dt - di) / dt, 1)
      self._last = cur or self._last
      log("system", ev="hb", proc=PROC, **d)


def start_system_monitor(period: float = None):
  """Begin device-wide system-stats heartbeats. Idempotent; call once from a
  long-lived nav process (e.g. navigationd)."""
  global _SYSMON_ON
  if _SYSMON_ON:
    return
  _SYSMON_ON = True
  get()  # ensure writer/offloader threads exist
  _SysMon(period or _SYSMON_PERIOD).start()


class Heartbeat:
  _HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

  def __init__(self, src: str, period: float = 5.0):
    self.src = src; self.period = period; self.n = 0
    self.t0 = time.time(); self.last_ticks = self._ticks()

  def _ticks(self) -> int:
    try:
      with open("/proc/self/stat") as f:
        p = f.read().split()
      return int(p[13]) + int(p[14])
    except Exception:
      return 0

  def _rss_mb(self) -> int:
    try:
      with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * 4096 // 1024 // 1024
    except Exception:
      return -1

  def tick(self):
    self.n += 1
    now = time.time(); dt = now - self.t0
    if dt >= self.period:
      ticks = self._ticks()
      cpu = 100.0 * (ticks - self.last_ticks) / self._HZ / dt if dt > 0 else 0.0
      log(self.src, ev="hb", loops=self.n, hz=round(self.n / dt, 1), cpu=round(cpu, 1), rss_mb=self._rss_mb())
      self.n = 0; self.t0 = now; self.last_ticks = ticks
