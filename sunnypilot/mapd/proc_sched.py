"""
proc_sched — scheduling policy for the mapd / live-map-matcher processes.

WHY THIS EXISTS
The map stack previously called `config_realtime_process([0,1,2,3], 5)`, which puts
it under SCHED_FIFO (hard real-time) priority 5 on cores 0-3 — the SAME cores and
priority class as dmonitoringd (driver monitoring) and torqued. Map matching is NOT
millisecond-safety-critical, but as a FIFO real-time task a heavy match burst runs to
completion and preempts the monitoring/locator procs on those cores. They miss their
deadlines, openpilot's timing watchdog fires, and the car REFUSES TO ENGAGE
(observed as "CPU starvation" when the nav feature flag is enabled).

THE FIX
Run the map stack at NORMAL scheduling (SCHED_OTHER) with max niceness, pinned to the
shared cores. Under SCHED_OTHER nice 19 it always yields to the SCHED_FIFO safety/
monitoring procs, so it can never starve them — it just uses whatever slack is left.
"""
import os
import sys

try:
  from openpilot.system.hardware import PC
except Exception:
  PC = False

try:
  from openpilot.common.swaglog import cloudlog
except Exception:
  cloudlog = None


def config_background_process(cores=(0, 1, 2, 3), niceness: int = 19) -> None:
  """Low-priority replacement for config_realtime_process for non-safety map work.
  SCHED_OTHER + max niceness + affinity to `cores`. Never raises."""
  applied = {"sched": "OTHER", "nice": None, "cores": list(cores), "pc": PC}
  if sys.platform == "linux" and not PC:
    # ensure we're on the normal scheduler (in case something set FIFO earlier)
    try:
      os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except Exception:
      pass
    try:
      os.sched_setaffinity(0, list(cores))
    except Exception:
      applied["cores"] = "unset"
  try:
    os.nice(niceness)
    applied["nice"] = niceness
  except Exception:
    pass
  if cloudlog is not None:
    cloudlog.warning("mapd proc_sched: background policy applied %s" % applied)
  return None
