#!/usr/bin/env python3
"""
Capture factory-nav CAN + GPS + our navd output for offline decoding.

Full CAN (changed frames) goes to DISK (durable, bandwidth-safe — streaming
thousands of frames/s over a phone hotspot would starve the nav it serves).
A low-rate HEARTBEAT (frames/s, GPS fix, line count) STREAMS via navlog so we
can watch the capture live from Helsinki. Run during a drive with factory nav set.
"""
import sys, time, json
sys.path.insert(0, "/data/openpilot")
import cereal.messaging as m
from openpilot.sunnypilot.navd import navlog

OUT = "/data/navcap.jsonl"
DUR = 3600
sm = m.SubMaster(["can", "gpsLocationExternal", "liveLocationKalman", "navigationd"])
f = open(OUT, "w")
t0 = time.time()
last = {}
frames = 0
hbt = t0


def main():
  global frames, hbt
  while time.time() - t0 < DUR:
    sm.update(50)
    ts = round(time.time() - t0, 2)
    if sm.updated.get("can"):
      for c in sm["can"]:
        if c.src in (0, 1, 2):
          h = bytes(c.dat).hex(); k = (c.src, c.address)
          frames += 1
          if last.get(k) != h:
            last[k] = h
            f.write(json.dumps({"t": ts, "k": "can", "bus": c.src, "a": hex(c.address), "d": h}) + "\n")
    if sm.updated.get("gpsLocationExternal"):
      g = sm["gpsLocationExternal"]
      if getattr(g, "hasFix", False):
        f.write(json.dumps({"t": ts, "k": "gps", "lat": g.latitude, "lon": g.longitude, "brg": g.bearingDeg, "spd": g.speed}) + "\n")
    if sm.updated.get("liveLocationKalman"):
      pv = sm["liveLocationKalman"].positionGeodetic
      if pv.valid:
        f.write(json.dumps({"t": ts, "k": "llk", "lat": pv.value[0], "lon": pv.value[1]}) + "\n")
    if sm.updated.get("navigationd"):
      d = sm["navigationd"]
      f.write(json.dumps({"t": ts, "k": "navd", "valid": bool(d.valid), "banner": d.bannerInstructions,
                          "man": [{"ty": x.type, "mo": x.modifier, "d": round(x.distance)} for x in d.allManeuvers]}) + "\n")
    f.flush()
    now = time.time()
    if now - hbt >= 5.0:
      gps_fix = bool(sm.alive.get("gpsLocationExternal") or sm.alive.get("liveLocationKalman"))
      navlog.log("can_capture", ev="hb", fps=round(frames / (now - hbt)), gps=gps_fix, distinct=len(last))
      frames = 0
      hbt = now


if __name__ == "__main__":
  main()
