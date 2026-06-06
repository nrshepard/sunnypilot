"""
Copyright (c) 2021-, sunnypilot and a number of other contributors.
This file is part of sunnypilot and is licensed under the MIT License.

NavSlowdown — longitudinal "slow toward the upcoming maneuver" helper.

Build-free, flag-gated (`navslow`), and ONLY-SLOWER: it returns a target speed that the
longitudinal planner folds in via min(v_cruise, target). It never speeds the car up.
A maneuver only engages when the car is close enough to reach the turn target under a
comfortable, bounded deceleration (NavSlowDecel) — i.e. it won't brake from a mile away.

Inputs come from navigationd.allManeuvers[] (distance/type/modifier); tunables from nav_tune.
"""
from __future__ import annotations

from openpilot.common.constants import CV
from openpilot.sunnypilot.navd.nav_tune import flag, tune

_UNSET = 1e3            # m/s — "no constraint" sentinel; min() against this is a no-op
_TARGET_OFFSET_S = 1.0  # arrive at target this many (target-speed) seconds early


class NavSlowdown:
  def __init__(self):
    self._cnt = -1
    self.enabled = False
    self.v_turn = 15 * CV.MPH_TO_MS
    self.v_slight = 25 * CV.MPH_TO_MS
    self.lookahead = 200.0
    self.decel = 1.5

  def update_params(self) -> None:
    self._cnt += 1
    if self._cnt % 50 == 0:
      self.enabled = flag('navslow')
      self.v_turn = tune('NavSlowTargetTurn', 15) * CV.MPH_TO_MS
      self.v_slight = tune('NavSlowTargetSlight', 25) * CV.MPH_TO_MS
      self.lookahead = max(20.0, tune('NavSlowLookahead', 200))
      self.decel = max(0.3, tune('NavSlowDecel', 1.5))

  @staticmethod
  def _is_hard_turn(mtype: str, mod: str) -> bool:
    t = (mtype or "").lower(); m = (mod or "").lower()
    if "turn" in t and ("left" in m or "right" in m) and "slight" not in m:
      return True
    return "uturn" in m or "sharp" in m

  @staticmethod
  def _is_slight(mtype: str, mod: str) -> bool:
    t = (mtype or "").lower(); m = (mod or "").lower()
    return "slight" in m or "fork" in t or "ramp" in t or "exit" in t or "merge" in t

  def target(self, sm, v_ego: float) -> float:
    """Lowest only-slower target speed (m/s) implied by an in-range maneuver, else _UNSET."""
    self.update_params()
    if not self.enabled:
      return _UNSET
    try:
      nav = sm['navigationd']
      if not nav.valid:
        return _UNSET
      maneuvers = nav.allManeuvers
    except (KeyError, AttributeError):
      return _UNSET

    best = _UNSET
    for m in maneuvers:
      d = float(getattr(m, 'distance', 0.0))
      if d <= 0.0 or d > self.lookahead:
        continue
      if self._is_hard_turn(m.type, m.modifier):
        vt = self.v_turn
      elif self._is_slight(m.type, m.modifier):
        vt = self.v_slight
      else:
        continue
      if vt >= v_ego:          # only ever slow down
        continue
      # Engage only once within comfortable braking distance (+ small time margin).
      brake_d = max(0.0, (v_ego * v_ego - vt * vt) / (2.0 * self.decel))
      if d <= brake_d + _TARGET_OFFSET_S * vt:
        best = min(best, vt)
    return best
