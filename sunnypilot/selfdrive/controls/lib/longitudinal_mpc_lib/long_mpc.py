"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import math

from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX


class LongitudinalMpcSP:
  def __init__(self, mpc, accel_controller):
    self._mpc = mpc
    self._accel_controller = accel_controller
    self._mpc_profile = None

  @property
  def crash_cnt(self):
    return self._mpc.crash_cnt

  @property
  def v_solution(self):
    return self._mpc.v_solution

  @property
  def a_solution(self):
    return self._mpc.a_solution

  @property
  def j_solution(self):
    return self._mpc.j_solution

  @property
  def solve_time(self):
    return self._mpc.solve_time

  @property
  def source(self):
    return self._mpc.source

  @source.setter
  def source(self, source):
    self._mpc.source = source

  def set_profile(self, profile) -> None:
    self._mpc_profile = profile

  def set_cur_state(self, v: float, a: float) -> None:
    self._mpc.set_cur_state(v, a)

  def set_weights(self, prev_accel_constraint=True, personality=None, jerk_scale=1.0):
    if personality is None:
      return self._mpc.set_weights(prev_accel_constraint, jerk_scale=jerk_scale)
    return self._mpc.set_weights(prev_accel_constraint, personality=personality, jerk_scale=jerk_scale)

  def update(self, radarstate, v_cruise, personality=None, t_follow=None):
    if not math.isfinite(v_cruise):
      v_cruise = V_CRUISE_MAX * CV.KPH_TO_MS

    accel_limits = None
    if self._accel_controller.is_enabled() and self._mpc_profile is not None:
      t_follow = self._mpc_profile.t_follow
      accel_limits = [self._mpc_profile.accel_min, self._mpc_profile.accel_max]

    if personality is None:
      return self._mpc.update(radarstate, v_cruise, t_follow=t_follow, accel_limits=accel_limits)
    return self._mpc.update(radarstate, v_cruise, personality=personality, t_follow=t_follow, accel_limits=accel_limits)
