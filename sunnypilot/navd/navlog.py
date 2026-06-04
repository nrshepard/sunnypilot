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
import json
import time
import socket
import threading
import urllib.request

HOST = os.environ.get("NAVLOG_HOST", "100.122.84.123")   # Helsinki tailnet IP
PORT = int(os.environ.get("NAVLOG_PORT", "5009"))
URL = f"http://{HOST}:{PORT}/ingest"
DISK = os.environ.get("NAVLOG_DISK", "/data/navd/navlog.jsonl")
OFFSET = DISK + ".offset"
MODE_FILE = os.environ.get("NAVLOG_MODE_FILE", "/data/navd/flags/logmode")
LOAD_MAX = float(os.environ.get("NAVLOG_LOAD_MAX", "5.0"))   # opportunistic offloads below this 1-min load
VALID_MODES = ("off", "disk", "stream", "opportunistic")


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
