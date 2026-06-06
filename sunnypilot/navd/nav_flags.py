"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

nav_flags — dead-simple feature flags for the mici-nav add-ons so each can be
toggled and tested INDEPENDENTLY.

A flag is ON iff the file /data/navd/flags/<name> exists. Default (no file) = OFF,
so a fresh slot behaves exactly like stock sunnypilot — no nav processes, no extra
load. Flip one on, reboot, test; flip off if it misbehaves.

Flags:
  navigationd  -> the turn-by-turn nav daemon (+ HUD maneuver chevron)
  navdestd     -> the tailnet/Siri destination endpoint (:5005)
  wifi_eager   -> the hotspot auto-reconnect watchdog
  navsteer     -> EXPERIMENTAL route-steering: inject nav maneuvers as model turn
                  desires (DesireHelper). Low-speed turns only (<20mph), supervised L2.
                  Instantiated at DesireHelper init -> reboot after toggling.
(curve slowing is the stock params SmartCruiseControlVision / SmartCruiseControlMap)
"""
import os

FLAG_DIR = os.environ.get("NAV_FLAG_DIR", "/data/navd/flags")


def enabled(name: str) -> bool:
  try:
    return os.path.exists(os.path.join(FLAG_DIR, name))
  except OSError:
    return False


def set_flag(name: str, on: bool) -> None:
  os.makedirs(FLAG_DIR, exist_ok=True)
  path = os.path.join(FLAG_DIR, name)
  if on:
    open(path, "w").close()
  elif os.path.exists(path):
    os.remove(path)
