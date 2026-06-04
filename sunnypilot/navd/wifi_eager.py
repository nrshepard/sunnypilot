#!/usr/bin/env python3
"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

wifi_eager — make the comma reconnect to a sleeping iOS Personal Hotspot without a
manual tap.

iOS stops broadcasting Personal Hotspot when no client is connected, so NetworkManager
often can't see the SSID to auto-join — you normally have to tap it, which actively
probes the SSID and wakes it. This daemon does that probe automatically:

  - Does NOTHING while the device already has internet (e.g. home wifi) — no churn.
  - When OFFLINE: rescans wifi and actively brings up the preferred hotspot connection.
    The association attempt is what wakes the sleeping iOS hotspot.

Build-free (pure subprocess/nmcli). Registered as an always_run process.
"""
import subprocess
import time

from openpilot.common.swaglog import cloudlog

# NetworkManager connection name for the phone hotspot (see `nmcli connection show`).
HOTSPOT_CONN = "openpilot connection Nathan's iPhone 17 Pro"
PERIOD_S = 20.0          # base loop interval
CONNECT_WAIT_S = 12      # how long to let an association attempt probe before giving up


def _run(args, timeout=20):
  try:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
  except Exception:
    return None


def is_online() -> bool:
  # NetworkManager's own verdict: "full" == real internet reachable.
  r = _run(["nmcli", "-t", "-f", "CONNECTIVITY", "general", "status"], timeout=8)
  return bool(r and r.returncode == 0 and "full" in r.stdout.lower())


def hotspot_active() -> bool:
  r = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"], timeout=8)
  return bool(r and r.returncode == 0 and HOTSPOT_CONN in r.stdout)


def main():
  cloudlog.warning("wifi_eager: started")
  while True:
    try:
      if not is_online():
        # nudge a fresh scan (throttled by NM; errors are harmless)
        _run(["nmcli", "dev", "wifi", "rescan"], timeout=20)
        time.sleep(2)
        if not hotspot_active():
          # actively probe/associate — this is what wakes a sleeping iOS hotspot
          r = _run(["nmcli", "--wait", str(CONNECT_WAIT_S), "con", "up", HOTSPOT_CONN],
                   timeout=CONNECT_WAIT_S + 8)
          if r is not None and r.returncode == 0:
            cloudlog.warning("wifi_eager: hotspot connected")
    except Exception:
      cloudlog.exception("wifi_eager: iteration error")
    time.sleep(PERIOD_S)


if __name__ == "__main__":
  main()
