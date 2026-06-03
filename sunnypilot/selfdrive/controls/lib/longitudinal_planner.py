"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import math

from cereal import messaging, custom
from opendbc.car import structs
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX
from openpilot.sunnypilot.selfdrive.controls.lib.dec.dec import DynamicExperimentalController
from openpilot.sunnypilot.selfdrive.controls.lib.e2e_alerts_helper import E2EAlertsHelper
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control.smart_cruise_control import SmartCruiseControl
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_assist import SpeedLimitAssist
from openpilot.sunnypilot.selfdrive.controls.lib.speed_limit.speed_limit_resolver import SpeedLimitResolver
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.models.helpers import get_active_bundle

from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.accel_personality.accel_controller import AccelPersonalityController
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpcSP
from openpilot.sunnypilot.selfdrive.controls.lib.radar_distance.radar_distance import RadarDistanceController
from opendbc.car.interfaces import ACCEL_MIN

DecState = custom.LongitudinalPlanSP.DynamicExperimentalControl.DynamicExperimentalControlState
LongitudinalPlanSource = custom.LongitudinalPlanSP.LongitudinalPlanSource

JERK_RELEASE = 1.8
JERK_RELEASE_CLOSING = 0.8
JERK_BRAKE = 8.0
JERK_BRAKE_COMFORT = 4.0
CLOSING_VREL = -2.0


def rate_limit_a_target(prev: float, value: float, release_rate: float = JERK_RELEASE, brake_rate: float = JERK_BRAKE) -> float:
  if value > prev:
    return min(value, prev + release_rate * DT_MDL)
  return max(value, prev - brake_rate * DT_MDL)


# stop-hold: once stopped, hold the stop and suppress creep until a sustained go, so the
# stop/go transition is smooth and never gas-brakes at standstill.
V_STOP_HOLD = 0.5     # m/s, latch the hold below this speed
STOP_GO_FRAMES = 6    # consecutive not-should-stop frames required to release (~0.3 s)


def apply_stop_hold(held: bool, go_count: int, v_ego: float, a_target: float, should_stop: bool):
  if should_stop and v_ego < V_STOP_HOLD:
    held = True
  if held:
    if should_stop:
      go_count = 0
    else:
      go_count += 1
      if go_count >= STOP_GO_FRAMES:
        held = False
    if held:
      should_stop = True
      a_target = min(a_target, 0.0)
  return a_target, should_stop, held, go_count


class LongitudinalPlannerSP:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP, mpc):
    self.events_sp = EventsSP()
    self.accel_controller = AccelPersonalityController()
    self.mpc = LongitudinalMpcSP(mpc, self.accel_controller)
    self.dec = DynamicExperimentalController(CP, self.mpc)
    self.radar_distance = RadarDistanceController()
    self.sm_sp = messaging.SubMaster(['liveTracks'])
    self.scc = SmartCruiseControl()
    self.resolver = SpeedLimitResolver()
    self.sla = SpeedLimitAssist(CP, CP_SP)
    self.generation = int(model_bundle.generation) if (model_bundle := get_active_bundle()) else None
    self.source = LongitudinalPlanSource.cruise
    self.e2e_alerts_helper = E2EAlertsHelper()

    self.output_v_target = 0.
    self._output_a_target = 0.
    self._last_plan_sm = None
    self._mpc_profile = None
    self._smoothed_radarstate = None
    self._stop_held = False
    self._stop_go_count = 0

  @property
  def output_a_target(self) -> float:
    return self._output_a_target

  def _apply_accel_personality_decel(self, value: float) -> float:
    if not self.accel_controller.is_enabled():
      return value

    sm = self._last_plan_sm
    if sm is None:
      return value

    radarstate = self._smoothed_radarstate
    should_stop = bool(self.output_should_stop)
    v_ego = sm['carState'].vEgo
    force_decel = sm['controlsState'].forceDecel
    value = self.accel_controller.shape_decel(v_ego, value, radarstate, should_stop, force_decel)
    accel_min = self.accel_controller.get_output_min_accel(
      v_ego, radarstate, self.is_e2e(sm), should_stop, force_decel)
    return max(value, accel_min)

  def _release_rate(self) -> float:
    return JERK_RELEASE_CLOSING if self._lead_closing() else JERK_RELEASE

  def _brake_rate(self) -> float:
    sm = self._last_plan_sm
    if sm is None:
      return JERK_BRAKE_COMFORT
    if self.output_should_stop or sm['controlsState'].forceDecel:
      return JERK_BRAKE
    return JERK_BRAKE if self._lead_closing() else JERK_BRAKE_COMFORT

  def _lead_closing(self) -> bool:
    rs = self._smoothed_radarstate
    if rs is None:
      return False

    lead_one = rs.leadOne
    lead_two = rs.leadTwo
    return ((lead_one.status and lead_one.vRel < CLOSING_VREL) or
            (lead_two.status and lead_two.vRel < CLOSING_VREL))

  def _set_mpc_profile(self, v_ego: float) -> None:
    sm = self._last_plan_sm
    if sm is None:
      return

    radarstate = self._smoothed_radarstate
    if radarstate is None:
      radarstate = self.smooth_radarstate(sm['radarState'])

    self._mpc_profile = self.accel_controller.get_mpc_profile(
      v_ego, radarstate, e2e=self.is_e2e(sm), should_stop=self.output_should_stop, force_decel=sm['controlsState'].forceDecel)
    self.mpc.set_profile(self._mpc_profile)

  @output_a_target.setter
  def output_a_target(self, value: float) -> None:
    value = float(value)
    if math.isfinite(value):
      if not self.accel_controller.is_enabled():
        self._output_a_target = value
        return
      value = self._apply_accel_personality_decel(value)
      self._output_a_target = rate_limit_a_target(self._output_a_target, value, self._release_rate(), self._brake_rate())

  def is_e2e(self, sm: messaging.SubMaster) -> bool:
    experimental_mode = sm['selfdriveState'].experimentalMode
    if not self.dec.active():
      return experimental_mode

    return experimental_mode and self.dec.mode() == "blended"

  def get_accel_clip(self, v_ego: float) -> list[float] | None:
    if self.accel_controller.is_enabled():
      self._set_mpc_profile(v_ego)
      if self._mpc_profile is not None:
        return [self._mpc_profile.accel_min, self._mpc_profile.accel_max]
      return [ACCEL_MIN, self.accel_controller.get_max_accel(v_ego)]
    return None

  def update_accel_clip(self, accel_clip: list[float], should_stop: bool, force_decel: bool) -> list[float]:
    if self.accel_controller.is_enabled():
      sm = self._last_plan_sm
      radarstate = self._smoothed_radarstate
      v_ego = sm['carState'].vEgo
      accel_clip[0] = self.accel_controller.get_output_min_accel(
        v_ego, radarstate, self.is_e2e(sm), should_stop, force_decel)
    return accel_clip

  def get_t_follow(self) -> float | None:
    if self.accel_controller.is_enabled():
      if self._mpc_profile is not None:
        return self._mpc_profile.t_follow
      return self.accel_controller.get_t_follow()
    return None

  def get_jerk_scale(self) -> float:
    if self.accel_controller.is_enabled():
      if self._mpc_profile is not None:
        return self._mpc_profile.jerk_scale
      return self.accel_controller.get_jerk_scale()
    return 1.0

  def stop_hold(self, v_ego: float, a_target: float, should_stop: bool) -> tuple[float, bool]:
    if not self.accel_controller.is_enabled():
      return a_target, should_stop
    a_target, should_stop, self._stop_held, self._stop_go_count = apply_stop_hold(
      self._stop_held, self._stop_go_count, v_ego, a_target, should_stop)
    return a_target, should_stop

  def update_targets(self, sm: messaging.SubMaster, v_ego: float, a_ego: float, v_cruise: float) -> tuple[float, float]:
    CS = sm['carState']
    v_cruise_cluster_kph = min(CS.vCruiseCluster, V_CRUISE_MAX)
    v_cruise_cluster = v_cruise_cluster_kph * CV.KPH_TO_MS

    long_enabled = sm['carControl'].enabled
    long_override = sm['carControl'].cruiseControl.override

    # Smart Cruise Control
    self.scc.update(sm, long_enabled, long_override, v_ego, a_ego, v_cruise)

    # Speed Limit Resolver
    self.resolver.update(v_ego, sm)

    # Speed Limit Assist
    has_speed_limit = self.resolver.speed_limit_valid or self.resolver.speed_limit_last_valid
    self.sla.update(long_enabled, long_override, v_ego, a_ego, v_cruise_cluster, self.resolver.speed_limit,
                    self.resolver.speed_limit_final_last, has_speed_limit, self.resolver.distance, self.events_sp)

    targets = {
      LongitudinalPlanSource.cruise: (v_cruise, a_ego),
      LongitudinalPlanSource.sccVision: (self.scc.vision.output_v_target, self.scc.vision.output_a_target),
      LongitudinalPlanSource.sccMap: (self.scc.map.output_v_target, self.scc.map.output_a_target),
      LongitudinalPlanSource.speedLimitAssist: (self.sla.output_v_target, self.sla.output_a_target),
    }

    self.source = min(targets, key=lambda k: targets[k][0])
    self.output_v_target, a_target = targets[self.source]
    return self.output_v_target, a_target

  def smooth_radarstate(self, radarstate):
    if self._smoothed_radarstate is None:
      self._smoothed_radarstate = self.radar_distance.smooth_radarstate(radarstate)
    return self._smoothed_radarstate

  def update(self, sm: messaging.SubMaster) -> None:
    self._last_plan_sm = sm
    self._mpc_profile = None
    self.mpc.set_profile(None)
    self._smoothed_radarstate = None
    self.events_sp.clear()
    self.dec.update(sm)
    self.e2e_alerts_helper.update(sm, self.events_sp)
    self.sm_sp.update(0)
    self.accel_controller.update(sm)
    self.radar_distance.update(sm, self.sm_sp)

  def publish_longitudinal_plan_sp(self, sm: messaging.SubMaster, pm: messaging.PubMaster) -> None:
    plan_sp_send = messaging.new_message('longitudinalPlanSP')

    plan_sp_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])

    longitudinalPlanSP = plan_sp_send.longitudinalPlanSP
    longitudinalPlanSP.longitudinalPlanSource = self.source
    longitudinalPlanSP.vTarget = float(self.output_v_target)
    longitudinalPlanSP.aTarget = float(self.output_a_target)
    longitudinalPlanSP.events = self.events_sp.to_msg()

    # Dynamic Experimental Control
    dec = longitudinalPlanSP.dec
    dec.state = DecState.blended if self.dec.mode() == 'blended' else DecState.acc
    dec.enabled = self.dec.enabled()
    dec.active = self.dec.active()

    longitudinalPlanSP.accelPersonality = int(self.accel_controller.get_accel_personality())

    # Smart Cruise Control
    smartCruiseControl = longitudinalPlanSP.smartCruiseControl
    # Vision Control
    sccVision = smartCruiseControl.vision
    sccVision.state = self.scc.vision.state
    sccVision.vTarget = float(self.scc.vision.output_v_target)
    sccVision.aTarget = float(self.scc.vision.output_a_target)
    sccVision.currentLateralAccel = float(self.scc.vision.current_lat_acc)
    sccVision.maxPredictedLateralAccel = float(self.scc.vision.max_pred_lat_acc)
    sccVision.enabled = self.scc.vision.is_enabled
    sccVision.active = self.scc.vision.is_active
    # Map Control
    sccMap = smartCruiseControl.map
    sccMap.state = self.scc.map.state
    sccMap.vTarget = float(self.scc.map.output_v_target)
    sccMap.aTarget = float(self.scc.map.output_a_target)
    sccMap.enabled = self.scc.map.is_enabled
    sccMap.active = self.scc.map.is_active

    # Speed Limit
    speedLimit = longitudinalPlanSP.speedLimit
    resolver = speedLimit.resolver
    resolver.speedLimit = float(self.resolver.speed_limit)
    resolver.speedLimitLast = float(self.resolver.speed_limit_last)
    resolver.speedLimitFinal = float(self.resolver.speed_limit_final)
    resolver.speedLimitFinalLast = float(self.resolver.speed_limit_final_last)
    resolver.speedLimitValid = self.resolver.speed_limit_valid
    resolver.speedLimitLastValid = self.resolver.speed_limit_last_valid
    resolver.speedLimitOffset = float(self.resolver.speed_limit_offset)
    resolver.distToSpeedLimit = float(self.resolver.distance)
    resolver.source = self.resolver.source
    assist = speedLimit.assist
    assist.state = self.sla.state
    assist.enabled = self.sla.is_enabled
    assist.active = self.sla.is_active
    assist.vTarget = float(self.sla.output_v_target)
    assist.aTarget = float(self.sla.output_a_target)

    # E2E Alerts
    e2eAlerts = longitudinalPlanSP.e2eAlerts
    e2eAlerts.greenLightAlert = self.e2e_alerts_helper.green_light_alert
    e2eAlerts.leadDepartAlert = self.e2e_alerts_helper.lead_depart_alert

    pm.send('longitudinalPlanSP', plan_sp_send)
