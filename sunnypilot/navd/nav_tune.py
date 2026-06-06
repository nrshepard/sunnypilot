"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.
This file is part of sunnypilot and is licensed under the MIT License.

nav_tune — central, BUILD-FREE accessor for the mici-nav route-steering test flags
and scalar tunables. Single source of truth = nav_presets.json (same dir).

Booleans  -> files under /data/navd/flags/<name>     (via nav_flags)
Scalars   -> files under /data/navd/<KEY>            (via NavParams)

Everything defaults OFF / to the documented default when the file is absent, so a
fresh slot behaves exactly like stock. No openpilot Params keys are used (those raise
UnknownKeyName on a prebuilt release slot), so this is safe on release-mici.

Read paths are cheap file stats/reads; callers should still throttle (e.g. every
50-60 frames) in realtime loops.
"""
from __future__ import annotations

import json
import os

from openpilot.sunnypilot.navd.nav_flags import enabled as _flag_enabled
from openpilot.sunnypilot.navd.navstore import NavParams

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRESETS_PATH = os.path.join(_HERE, "nav_presets.json")

with open(_PRESETS_PATH, encoding="utf-8") as _f:
  MANIFEST = json.load(_f)

FLAGS = MANIFEST["flags"]
TUNABLES = MANIFEST["tunables"]
PRESETS = MANIFEST["presets"]
MANAGED_FLAGS = MANIFEST["managed_flags"]
REBOOT_REQUIRED_FLAGS = MANIFEST["reboot_required_flags"]

_params = NavParams()


def flag(name: str) -> bool:
  """True iff the boolean feature flag file exists."""
  return _flag_enabled(name)


def tune(key: str, fallback: float | None = None) -> float:
  """Scalar tunable as float. Falls back to manifest default, then `fallback`, then 0.0."""
  raw = _params.get(key)
  if raw is not None:
    try:
      return float(raw)
    except (TypeError, ValueError):
      pass
  if key in TUNABLES:
    return float(TUNABLES[key]["default"])
  return float(fallback) if fallback is not None else 0.0


def dump() -> dict:
  """Snapshot current device state of every managed flag + tunable (for `ctk navstate`)."""
  return {
    "flags": {name: flag(name) for name in MANAGED_FLAGS},
    "tune": {key: tune(key) for key in TUNABLES},
  }


if __name__ == "__main__":
  print(json.dumps(dump(), indent=2))
