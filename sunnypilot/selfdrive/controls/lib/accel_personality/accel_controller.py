"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from typing import NamedTuple

from cereal import custom
import numpy as np
from openpilot.common.constants import CV
from openpilot.common.realtime import DT_MDL
from openpilot.common.params import Params
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX
from opendbc.car.interfaces import ACCEL_MIN

AccelPersonality = custom.LongitudinalPlanSP.AccelerationPersonality
ACCEL_PERSONALITY_OPTIONS = [AccelPersonality.eco, AccelPersonality.normal, AccelPersonality.sport]

A_MAX_BP = [0.0, 4.0, 8.0, 16.0, 40.0]
A_MAX_V = {
  AccelPersonality.eco:    [1.40, 1.40, 1.30, 0.43, 0.08],
  AccelPersonality.normal: [1.80, 1.80, 1.45, 0.50, 0.15],
  AccelPersonality.sport:  [2.20, 2.20, 1.60, 0.70, 0.25],
}

A_MIN_BP = [0.0, 5.0, 15.0, 35.0]
A_MIN_V = {
  AccelPersonality.eco:    [-0.35, -0.50, -0.75, -1.00],
  AccelPersonality.normal: [-0.45, -0.70, -1.00, -1.30],
  AccelPersonality.sport:  [-0.60, -0.90, -1.30, -1.70],
}

RAMP_OFF_RANGE = 5.0

T_FOLLOW = {
  AccelPersonality.eco:    1.55,
  AccelPersonality.normal: 1.45,
  AccelPersonality.sport:  1.30,
}

JERK_SCALE = {
  AccelPersonality.eco:    1.2,
  AccelPersonality.normal: 1.0,
  AccelPersonality.sport:  0.8,
}

PARAM_REFRESH_FRAMES = max(1, int(1.0 / DT_MDL))

LEAD_STOP_BUFFER_BASE = 2.0
LEAD_STOP_BUFFER_SPEED_GAIN = 0.20
LEAD_STOP_BUFFER_MAX = 6.0
LEAD_BRAKE_WEIGHT = 0.35
LEAD_SAFETY_SCALE = 1.25
LEAD_RELEASE_TTC = 3.0
LEAD_CRITICAL_TTC = 1.25
LEAD_CRITICAL_LEAD_BRAKE = 2.0
LEAD_CRITICAL_STOCK_FRAC = 0.75
LEAD_PREBRAKE_CLOSING = 0.45
LEAD_PREBRAKE_DECEL = 0.08
LEAD_PREBRAKE_TTC = 5.0
LEAD_COMFORT_MARGIN = 0.8
LEAD_COMFORT_HEADWAY_SCALE = 0.75
LEAD_COAST_MIN_CLOSING = 0.5
LEAD_COAST_DECEL = 0.20
LEAD_MPC_T_FOLLOW_RELIEF = 0.35
LEAD_MPC_T_FOLLOW_MIN = 1.15

LEAD_PREBRAKE_SCALE = {
  AccelPersonality.eco:    0.65,
  AccelPersonality.normal: 0.85,
  AccelPersonality.sport:  1.00,
}


class LeadBrakeState(NamedTuple):
  closing: float
  critical: bool
  headway: float
  lead_brake: float
  required_decel: float
  score: float
  ttc: float


class LeadMpcProfile(NamedTuple):
  accel_min: float
  accel_max: float
  jerk_scale: float
  t_follow: float


class AccelPersonalityController:
  def __init__(self):
    self.params = Params()
    self.frame = 0
    val = self.params.get('AccelPersonality')
    self._personality = val if val is not None else AccelPersonality.normal
    self._enabled = self.params.get_bool('AccelPersonalityEnabled')
    self._v_cruise = 0.0

  def update(self, sm=None):
    self.frame += 1
    if sm is not None:
      vc_kph = sm['carState'].vCruise
      self._v_cruise = vc_kph * CV.KPH_TO_MS if np.isfinite(vc_kph) and vc_kph < V_CRUISE_MAX else 0.0

    if self.frame % PARAM_REFRESH_FRAMES == 0:
      val = self.params.get('AccelPersonality')
      self._personality = val if val is not None else AccelPersonality.normal
      self._enabled = self.params.get_bool('AccelPersonalityEnabled')

  @property
  def accel_personality(self) -> int:
    return self._personality

  def get_accel_personality(self) -> int:
    return int(self._personality)

  def set_accel_personality(self, personality: int):
    if personality in ACCEL_PERSONALITY_OPTIONS:
      self._personality = personality
      self.params.put('AccelPersonality', personality)

  def cycle_accel_personality(self) -> int:
    idx = ACCEL_PERSONALITY_OPTIONS.index(self._personality) if self._personality in ACCEL_PERSONALITY_OPTIONS else 0
    nxt = ACCEL_PERSONALITY_OPTIONS[(idx + 1) % len(ACCEL_PERSONALITY_OPTIONS)]
    self.set_accel_personality(nxt)
    return int(nxt)

  def is_enabled(self) -> bool:
    return self._enabled

  def set_enabled(self, enabled: bool):
    self._enabled = bool(enabled)
    self.params.put_bool('AccelPersonalityEnabled', self._enabled)

  def toggle_enabled(self) -> bool:
    self.set_enabled(not self._enabled)
    return self._enabled

  def reset(self, personality: int | None = None):
    if personality is None or personality not in ACCEL_PERSONALITY_OPTIONS:
      personality = AccelPersonality.normal
    self._personality = personality
    self.params.put('AccelPersonality', self._personality)
    self.frame = 0
    self._v_cruise = 0.0

  def get_max_accel(self, v_ego: float) -> float:
    base = float(np.interp(max(0.0, v_ego), A_MAX_BP, A_MAX_V[self._personality]))
    if not np.isfinite(self._v_cruise) or self._v_cruise <= 0.0:
      return base
    ramp = float(np.clip((self._v_cruise - v_ego) / RAMP_OFF_RANGE, 0.0, 1.0))
    return base * ramp

  def get_profile_min_accel(self, v_ego: float) -> float:
    return float(np.interp(max(0.0, v_ego), A_MIN_BP, A_MIN_V[self._personality]))

  def _lead_brake_state(self, lead, v_ego: float) -> LeadBrakeState | None:
    if lead is None or not bool(lead.status):
      return None

    d_rel = max(0.0, float(lead.dRel))
    v_lead = max(0.0, float(lead.vLead))
    v_rel = float(lead.vRel)
    if abs(v_rel) < 1e-3 and abs(v_lead - v_ego) > 1e-3:
      v_rel = v_lead - v_ego

    closing = max(0.0, -v_rel)
    lead_brake = max(0.0, -float(lead.aLeadK))
    stop_buffer = float(np.clip(LEAD_STOP_BUFFER_BASE + LEAD_STOP_BUFFER_SPEED_GAIN * max(0.0, v_ego),
                                LEAD_STOP_BUFFER_BASE, LEAD_STOP_BUFFER_MAX))
    usable_gap = max(0.25, d_rel - stop_buffer)
    headway = d_rel / max(0.1, v_ego)
    closing_load = closing + 0.4 * lead_brake
    ttc = usable_gap / closing_load if closing_load > 0.1 else float('inf')
    required_decel = closing * closing / (2.0 * usable_gap) + LEAD_BRAKE_WEIGHT * lead_brake
    critical = (lead.fcw or lead_brake > LEAD_CRITICAL_LEAD_BRAKE or
                (ttc < LEAD_CRITICAL_TTC and closing > 0.3) or required_decel > abs(ACCEL_MIN) * LEAD_CRITICAL_STOCK_FRAC)
    inv_ttc = 1.0 / max(0.1, ttc) if np.isfinite(ttc) else 0.0

    return LeadBrakeState(
      closing=closing,
      critical=critical,
      headway=headway,
      lead_brake=lead_brake,
      required_decel=required_decel,
      score=required_decel + 0.25 * lead_brake + inv_ttc,
      ttc=ttc,
    )

  def _best_lead_brake_state(self, radarstate, v_ego: float) -> LeadBrakeState | None:
    if radarstate is None:
      return None
    states = [s for s in (self._lead_brake_state(radarstate.leadOne, v_ego),
                          self._lead_brake_state(radarstate.leadTwo, v_ego)) if s is not None]
    return max(states, key=lambda s: s.score) if states else None

  def get_min_accel(self, v_ego: float, radarstate=None, should_stop: bool = False, force_decel: bool = False) -> float:
    if not self._enabled:
      return ACCEL_MIN
    if should_stop or force_decel:
      return ACCEL_MIN

    profile_min = self.get_profile_min_accel(v_ego)
    lead_state = self._best_lead_brake_state(radarstate, v_ego)
    if lead_state is None:
      return profile_min
    if lead_state.critical:
      return ACCEL_MIN

    risk_min = float(np.clip(-lead_state.required_decel * LEAD_SAFETY_SCALE, ACCEL_MIN, 0.0))
    if lead_state.required_decel <= 0.05 and lead_state.ttc > LEAD_RELEASE_TTC:
      return profile_min
    return min(profile_min, risk_min)

  def get_output_min_accel(self, v_ego: float, radarstate=None, e2e: bool = False,
                           should_stop: bool = False, force_decel: bool = False) -> float:
    if e2e:
      return ACCEL_MIN
    return self.get_min_accel(v_ego, radarstate, should_stop, force_decel)

  def get_mpc_profile(self, v_ego: float, radarstate=None, e2e: bool = False, should_stop: bool = False, force_decel: bool = False) -> LeadMpcProfile:
    accel_max = self.get_max_accel(v_ego)
    t_follow = self.get_t_follow()
    jerk_scale = self.get_jerk_scale()
    accel_min = self.get_output_min_accel(v_ego, radarstate, e2e, should_stop, force_decel)
    # blended (model drives), stop, force-decel: full stock braking authority, no lead coast/relief
    if not self._enabled or e2e or should_stop or force_decel:
      return LeadMpcProfile(accel_min, accel_max, jerk_scale, t_follow)

    lead_state = self._best_lead_brake_state(radarstate, v_ego)
    profile_min = self.get_profile_min_accel(v_ego)
    low_risk = (lead_state is not None and not lead_state.critical and
                lead_state.closing > LEAD_COAST_MIN_CLOSING and
                lead_state.ttc > LEAD_PREBRAKE_TTC and
                lead_state.required_decel < abs(profile_min) * LEAD_COMFORT_MARGIN)
    if low_risk:
      relief = LEAD_MPC_T_FOLLOW_RELIEF * float(np.clip((lead_state.ttc - LEAD_PREBRAKE_TTC) / LEAD_PREBRAKE_TTC, 0.0, 1.0))
      t_follow = max(LEAD_MPC_T_FOLLOW_MIN, t_follow - relief)

    return LeadMpcProfile(accel_min, accel_max, jerk_scale, t_follow)

  def shape_decel(self, v_ego: float, a_target: float, radarstate=None, should_stop: bool = False, force_decel: bool = False) -> float:
    if not self._enabled or should_stop or force_decel:
      return a_target

    lead_state = self._best_lead_brake_state(radarstate, v_ego)
    if lead_state is None or lead_state.critical:
      return a_target

    shaped = float(a_target)
    profile_min = self.get_profile_min_accel(v_ego)
    low_risk = (lead_state.ttc > LEAD_RELEASE_TTC and
                lead_state.required_decel < abs(profile_min) * LEAD_COMFORT_MARGIN and
                lead_state.headway > self.get_t_follow() * LEAD_COMFORT_HEADWAY_SCALE)
    if low_risk and lead_state.closing > LEAD_COAST_MIN_CLOSING:
      coast_limit = max(-lead_state.required_decel * LEAD_SAFETY_SCALE, -LEAD_COAST_DECEL)
      shaped = max(shaped, coast_limit)

    if lead_state.closing > LEAD_PREBRAKE_CLOSING and lead_state.required_decel > LEAD_PREBRAKE_DECEL and lead_state.ttc < LEAD_PREBRAKE_TTC:
      dynamic_min = self.get_min_accel(v_ego, radarstate, should_stop, force_decel)
      prebrake_target = max(dynamic_min, -lead_state.required_decel * LEAD_PREBRAKE_SCALE[self._personality])
      shaped = min(shaped, prebrake_target)

    return shaped

  def get_t_follow(self) -> float:
    return T_FOLLOW[self._personality]

  def get_jerk_scale(self) -> float:
    return JERK_SCALE[self._personality]
