#!/usr/bin/env python3
"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

nav_destination_server — tailnet destination setter for navigationd.

This is the target for the iPhone Siri Shortcut ("Hey Siri, navigate to ...").
It does NOT geocode or route; it only writes the place-name string to the
`MapboxRoute` param. navigationd picks that up, geocodes it via Mapbox, fetches
the route, and publishes maneuvers. No shell, no eval — params only.

Reachable over the device's tailnet IP (and its own hotspot). Bind is on all
interfaces so the phone can reach it on any shared network without comma Prime.

Endpoints:
  GET  /set?dest=<place>      -> sets MapboxRoute, AllowNavigation=1
  GET  /cancel               -> clears route
  POST /  {"destination":"..."} -> same as /set
  GET  /status               -> current destination + nav validity
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

PORT = 5005
_CLEAR_WORDS = ("", "cancel", "clear", "stop", "end", "off")


class Handler(BaseHTTPRequestHandler):
  def _send(self, code: int, obj: dict):
    body = json.dumps(obj).encode()
    self.send_response(code)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def _set_dest(self, dest: str) -> dict:
    dest = (dest or "").strip()
    p = Params()
    if dest.lower() in _CLEAR_WORDS:
      p.put("MapboxRoute", "")
      p.put_bool("AllowNavigation", False)
      cloudlog.warning("navdestd: route cleared")
      return {"ok": True, "action": "cleared"}
    p.put("MapboxRoute", dest)
    p.put_bool("AllowNavigation", True)
    cloudlog.warning(f"navdestd: destination set -> {dest!r}")
    return {"ok": True, "destination": dest}

  def _status(self) -> dict:
    p = Params()
    return {
      "ok": True,
      "destination": p.get("MapboxRoute") or "",
      "allow_navigation": p.get_bool("AllowNavigation"),
      "has_token": bool(p.get("MapboxToken")),
    }

  def do_GET(self):
    path = urlparse(self.path).path.rstrip("/")
    q = parse_qs(urlparse(self.path).query)
    if path.endswith("/status"):
      return self._send(200, self._status())
    if path.endswith("/cancel"):
      return self._send(200, self._set_dest(""))
    dest = (q.get("dest") or q.get("destination") or [""])[0]
    self._send(200, self._set_dest(unquote(dest)))

  def do_POST(self):
    n = int(self.headers.get("Content-Length", 0) or 0)
    raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
    dest = ""
    if raw:
      try:
        dest = json.loads(raw).get("destination", "")
      except Exception:
        dest = raw  # allow a raw "address" body straight from a Shortcut
    self._send(200, self._set_dest(dest))

  def log_message(self, *_):  # silence default stderr access log
    pass


def main():
  cloudlog.warning(f"navdestd: listening on :{PORT}")
  ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
  main()
