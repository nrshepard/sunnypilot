"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

navlog — unified, non-blocking logger for the mici-nav features.

- Callers do navlog.log(src, **fields) — cheap, never blocks (drops to a queue).
- A background thread batches and STREAMS to the Helsinki collector over tailnet.
- On send failure OR queue overflow it SPILLS to disk (/data/navd/navlog.jsonl),
  so nothing is lost when offline — that's the "fallback to disk".
- Heartbeat() gives per-process CPU%/loop-rate/RSS telemetry so we can see exactly
  which nav feature is tanking the CPU when flipped on.

The logger thread is the ONLY thing that does network/disk I/O, so logging never
adds latency to the caller's loop (important — we don't want the telemetry to be
the thing that overloads the CPU).
"""
import os
import json
import time
import queue
import threading
import urllib.request

HOST = os.environ.get("NAVLOG_HOST", "100.122.84.123")   # Helsinki tailnet IP
PORT = int(os.environ.get("NAVLOG_PORT", "5009"))
URL = f"http://{HOST}:{PORT}/ingest"
DISK = os.environ.get("NAVLOG_DISK", "/data/navd/navlog.jsonl")
BATCH = 200
QMAX = 20000


class _NavLog:
  def __init__(self):
    self.q: queue.Queue = queue.Queue(maxsize=QMAX)
    self.dropped = 0
    self._t = threading.Thread(target=self._worker, name="navlog", daemon=True)
    self._t.start()

  def log(self, src: str, **fields):
    rec = {"t": round(time.time(), 3), "src": src}
    rec.update(fields)
    try:
      self.q.put_nowait(rec)
    except queue.Full:
      # queue backed up (e.g. stream can't keep up) -> spill straight to disk
      self.dropped += 1
      self._to_disk([rec])

  def _worker(self):
    while True:
      batch = []
      try:
        batch.append(self.q.get(timeout=1.0))
        while len(batch) < BATCH:
          batch.append(self.q.get_nowait())
      except queue.Empty:
        pass
      except Exception:
        pass
      if not batch:
        continue
      if not self._send(batch):
        self._to_disk(batch)

  def _send(self, batch) -> bool:
    try:
      data = ("\n".join(json.dumps(r) for r in batch)).encode()
      req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/x-ndjson"})
      urllib.request.urlopen(req, timeout=2).read()
      return True
    except Exception:
      return False

  def _to_disk(self, batch):
    try:
      os.makedirs(os.path.dirname(DISK), exist_ok=True)
      with open(DISK, "a") as f:
        for r in batch:
          f.write(json.dumps(r) + "\n")
    except Exception:
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
  """Per-process CPU%/loop-rate/RSS telemetry. Call tick() each loop iteration;
  it emits a heartbeat via navlog every `period` seconds. Cheap (a few /proc reads)."""
  _HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

  def __init__(self, src: str, period: float = 5.0):
    self.src = src
    self.period = period
    self.n = 0
    self.t0 = time.time()
    self.last_ticks = self._ticks()

  def _ticks(self) -> int:
    try:
      with open("/proc/self/stat") as f:
        p = f.read().split()
      return int(p[13]) + int(p[14])  # utime + stime
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
    now = time.time()
    dt = now - self.t0
    if dt >= self.period:
      ticks = self._ticks()
      cpu = 100.0 * (ticks - self.last_ticks) / self._HZ / dt if dt > 0 else 0.0
      log(self.src, ev="hb", loops=self.n, hz=round(self.n / dt, 1), cpu=round(cpu, 1), rss_mb=self._rss_mb())
      self.n = 0
      self.t0 = now
      self.last_ticks = ticks
