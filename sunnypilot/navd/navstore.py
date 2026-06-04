"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

NavParams — a tiny file-backed key/value store that mirrors the subset of the
openpilot Params API that navd uses, WITHOUT requiring keys to be registered in
the (prebuilt) params_pyx C++ extension.

Why: release slots ship a prebuilt params_pyx.so with a fixed key registry, so
new keys (MapboxRoute, AllowNavigation, ...) raise UnknownKeyName. This store
keeps navd fully build-free — it persists to plain files under NAVD_STORE_DIR
(default /data/navd). Separate navd processes (navigationd, nav_destination_server,
the route helpers) share state through these files, which doubles as their IPC.

It is a DROP-IN for the handful of methods navd calls; it is NOT a general Params
replacement. Imported as `from ...navd.navstore import NavParams as Params`.
"""
from __future__ import annotations

import json
import os

STORE_DIR = os.environ.get("NAVD_STORE_DIR", "/data/navd")

# Keys whose value is a structured object (stored/loaded as JSON). All other
# keys round-trip as plain strings, matching openpilot Params get/put semantics.
_JSON_KEYS = {"MapboxSettings"}

# Defaults returned when a key is absent and the caller passed return_default=True.
_DEFAULTS = {
  "AllowNavigation": "0",
  "MapboxRecompute": "0",
  "NavDesiresAllowed": "0",
  "MapboxToken": "",
}


class NavParams:
  def __init__(self, d: str | None = None):
    self._dir = d or STORE_DIR
    try:
      os.makedirs(self._dir, exist_ok=True)
    except OSError:
      pass

  def _path(self, key) -> str:
    if isinstance(key, bytes):
      key = key.decode()
    return os.path.join(self._dir, key)

  def _read(self, key) -> str | None:
    try:
      with open(self._path(key), encoding="utf-8") as f:
        return f.read()
    except (FileNotFoundError, OSError):
      return None

  def _write(self, key, text: str) -> None:
    path = self._path(key)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      f.write(text)
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic — a reader never sees a partial value

  # --- Params-compatible surface used by navd ---
  def get(self, key, return_default: bool = False, block: bool = False, encoding=None):
    raw = self._read(key)
    if isinstance(key, bytes):
      key = key.decode()
    if raw is None:
      return _DEFAULTS.get(key) if return_default else None
    if key in _JSON_KEYS:
      try:
        return json.loads(raw)
      except (ValueError, TypeError):
        return None
    return raw

  def get_bool(self, key, return_default: bool = False) -> bool:
    raw = self._read(key)
    if raw is None:
      return _DEFAULTS.get(key.decode() if isinstance(key, bytes) else key, "0") == "1" if return_default else False
    return raw.strip() == "1"

  def put(self, key, dat) -> None:
    if isinstance(dat, (dict, list)):
      text = json.dumps(dat)
    elif isinstance(dat, bytes):
      text = dat.decode("utf-8", "replace")
    else:
      text = str(dat)
    self._write(key, text)

  # navd uses these in non-time-critical paths; plain synchronous writes are fine.
  def put_nonblocking(self, key, dat) -> None:
    self.put(key, dat)

  def put_bool(self, key, val) -> None:
    self._write(key, "1" if val else "0")

  def remove(self, key) -> None:
    try:
      os.remove(self._path(key))
    except (FileNotFoundError, OSError):
      pass
