"""
Copyright (c) 2021-, James Vecellio, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import threading
import time
from math import degrees
from numpy import interp

import cereal.messaging as messaging
from cereal import custom
from openpilot.sunnypilot.navd.navstore import NavParams as Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog

from openpilot.sunnypilot.navd.constants import NAV_CV
from openpilot.sunnypilot.navd.helpers import Coordinate, parse_banner_instructions
from openpilot.sunnypilot.navd.navigation_helpers.mapbox_integration import MapboxIntegration
from openpilot.sunnypilot.navd.navigation_helpers.nav_instructions import NavigationInstructions

RECOMPUTE_COOLDOWN_FRAMES = 30  # ~10s at 3Hz between (re)compute attempts — avoids hammering Mapbox


class Navigationd:
  def __init__(self):
    self.params = Params()
    self.mapbox = MapboxIntegration()
    self.nav_instructions = NavigationInstructions()

    self.sm = messaging.SubMaster(['carState', 'liveLocationKalman'])
    self.pm = messaging.PubMaster(['navigationd'])
    self.rk = Ratekeeper(3)  # 3 Hz

    self.route = None
    self.destination: str | None = None
    self.new_destination: str = ''

    self.allow_navigation: bool = False
    self.recompute_allowed: bool = False
    self.reroute_counter: int = 0
    self.cancel_route_counter: int = 0

    self.frame: int = -1
    self.last_position: Coordinate | None = None
    self.last_bearing: float | None = None
    self.valid: bool = False

    # --- background recompute worker ---------------------------------------------------
    # Mapbox geocode + directions are blocking HTTP. Running them on the 3Hz publish loop
    # stalls navigationd (it stops publishing -> blank HUD). The worker thread owns all
    # network calls; the main loop only ever does non-blocking disk reads + publishes.
    self._lock = threading.Lock()
    self._req: tuple | None = None        # (dest, lon, lat, bearing) pending for the worker
    self._inflight: bool = False
    self._route_reload: bool = False      # worker -> main: fresh route on disk, reload it
    self._computed_dest: str | None = None
    self._next_attempt_frame: int = 0
    self._worker = threading.Thread(target=self._recompute_worker, name='navd_recompute', daemon=True)
    self._worker.start()

  def _recompute_worker(self):
    while True:
      req = None
      with self._lock:
        if self._req is not None and not self._inflight:
          req, self._req, self._inflight = self._req, None, True
      if req is None:
        time.sleep(0.2)
        continue
      dest, lon, lat, bearing = req
      valid = False
      try:
        # blocking HTTP (geocode + directions); writes MapboxSettings on success
        _, valid = self.mapbox.set_destination({'place_name': dest}, lon, lat, bearing)
      except Exception:
        cloudlog.exception('navigationd: recompute worker failed')
      with self._lock:
        if valid:
          self._computed_dest = dest
          self._route_reload = True
        self._inflight = False

  def _queue_recompute(self, dest: str) -> bool:
    with self._lock:
      if self._inflight or self._req is not None:
        return False
      self._req = (dest, self.last_position.longitude, self.last_position.latitude, self.last_bearing)
      return True

  def _update_params(self):
    if self.last_position is None:
      return
    self.frame += 1
    if self.frame % 15 == 0:
      self.allow_navigation = self.params.get('AllowNavigation', return_default=True)
      self.new_destination = self.params.get('MapboxRoute')
      self.recompute_allowed = self.params.get('MapboxRecompute', return_default=True)

    new_dest = self.new_destination or ''

    # apply a route the worker just computed (disk read only — non-blocking)
    with self._lock:
      reload_now, computed = self._route_reload, self._computed_dest
      self._route_reload = False
    if reload_now:
      self.destination = computed
      self.nav_instructions.clear_route_cache()
      self.route = self.nav_instructions.get_current_route()
      self.cancel_route_counter = 0
      self.reroute_counter = 0

    # decide whether a (re)compute is needed and hand it to the worker — never block here
    need_new = new_dest != '' and new_dest != self.destination
    need_reroute = bool(self.recompute_allowed and self.reroute_counter > 9 and self.route)
    if (need_new or need_reroute) and self.frame >= self._next_attempt_frame:
      dest = new_dest if need_new else self.destination
      if dest and self._queue_recompute(dest):
        self._next_attempt_frame = self.frame + RECOMPUTE_COOLDOWN_FRAMES
        self.reroute_counter = 0

    # route cancellation (disk only)
    if self.cancel_route_counter == 30:
      self.cancel_route_counter = 0
      self.params.put_nonblocking("MapboxRoute", "")
      self.nav_instructions.clear_route_cache()
      self.route = None
      self.destination = None

    self.valid = self.route is not None

  def _update_navigation(self) -> tuple[str, dict | None, dict]:
    banner_instructions: str = ''
    nav_data: dict = {}
    if self.allow_navigation and self.route and self.last_position is not None:
      if progress := self.nav_instructions.get_route_progress(self.last_position.latitude, self.last_position.longitude):
        v_ego = float(max(self.sm['carState'].vEgo, 0.0))
        nav_data['upcoming_turn'] = self.nav_instructions.get_upcoming_turn_from_progress(progress, self.last_position.latitude,
                                                                                          self.last_position.longitude, v_ego)
        speed_limit, _ = progress['current_maxspeed']
        nav_data['current_speed_limit'] = speed_limit
        arrived = self.nav_instructions.arrived_at_destination(progress, v_ego)

        if progress['current_step']:
          if parsed := parse_banner_instructions(progress['current_step']['bannerInstructions'], progress['distance_to_end_of_step']):
            banner_instructions = parsed['maneuverPrimaryText']

        nav_data['distance_from_route'] = progress['distance_from_route']
        speed_breakpoints: list = [0.0, 5.0, 10.0, 20.0, 40.0]
        distance_list: list = [100.0, 125.0, 150.0, 200.0, 250.0]
        large_distance: bool = progress['distance_from_route'] > float(interp(v_ego, speed_breakpoints, distance_list))

        route_bearing_misalign: bool = self.nav_instructions.route_bearing_misalign(self.route, self.last_bearing, v_ego)

        if large_distance and not arrived:
          self.cancel_route_counter = self.cancel_route_counter + 1 if progress['distance_from_route'] > NAV_CV.QUARTER_MILE else 0
          if self.recompute_allowed:
            self.reroute_counter += 1
        elif arrived:
          self.cancel_route_counter += 1
          self.recompute_allowed = False
        elif route_bearing_misalign:
          self.cancel_route_counter += 1
          if self.recompute_allowed:
            self.reroute_counter += 1
        else:
          self.cancel_route_counter = 0
          self.reroute_counter = 0

        # Don't recompute in last segment to prevent reroute loops
        if progress['current_step_idx'] == len(self.route['steps']) - 1:
          self.recompute_allowed = False
          self.allow_navigation = False
    else:
      banner_instructions = ''
      progress = None
      nav_data = {}

    return banner_instructions, progress, nav_data

  def _build_navigation_message(self, banner_instructions: str, progress: dict | None, nav_data: dict, valid: bool):
    msg = messaging.new_message('navigationd')
    msg.valid = valid
    msg.navigationd.upcomingTurn = nav_data.get('upcoming_turn', 'none')
    msg.navigationd.currentSpeedLimit = nav_data.get('current_speed_limit', 0)
    msg.navigationd.bannerInstructions = banner_instructions
    msg.navigationd.distanceFromRoute = nav_data.get('distance_from_route', 0.0)
    msg.navigationd.valid = self.valid

    all_maneuvers = (
      [custom.Navigationd.Maneuver.new_message(distance=m['distance'], type=m['type'], modifier=m['modifier'],
                                               instruction=m['instruction']) for m in progress['all_maneuvers']]
      if progress
      else []
    )
    msg.navigationd.allManeuvers = all_maneuvers
    return msg

  def run(self):
    cloudlog.warning('navigationd init')

    while True:
      try:
        self.sm.update(0)
        location = self.sm['liveLocationKalman']
        localizer_valid = location.positionGeodetic.valid if location else False

        if localizer_valid:
          self.last_bearing = degrees(location.calibratedOrientationNED.value[2])
          self.last_position = Coordinate(location.positionGeodetic.value[0], location.positionGeodetic.value[1])

        self._update_params()
        banner_instructions, progress, nav_data = self._update_navigation()

        msg = self._build_navigation_message(banner_instructions, progress, nav_data, valid=localizer_valid)

        self.pm.send('navigationd', msg)
      except Exception:
        # one bad cycle must never kill nav (or stall the publish loop)
        cloudlog.exception('navigationd iteration failed')

      self.rk.keep_time()


def main():
  nav = Navigationd()
  nav.run()


if __name__ == "__main__":
  main()
