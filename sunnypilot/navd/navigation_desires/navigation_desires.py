"""
Copyright (c) 2021-, James Vecellio, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

NavigationDesires — maps a navigationd upcoming maneuver to a model turn/keep Desire.
Atomic, build-free, flag-gated (see nav_tune / nav_presets.json):
  navsteer            master (this class is only instantiated when ON)
  navsteer_turn       allow hard turnLeft/turnRight (speed-gated by NavSteerTurnSpeed)
  navsteer_keep       allow keepLeft/keepRight slight/fork nudges
  navsteer_handsoff   drop the steeringPressed/torque requirement on keep nudges
EXPERIMENTAL / supervised L2 — turns are low-speed only.
"""
import cereal.messaging as messaging
from cereal import car, log
from openpilot.common.constants import CV
from openpilot.sunnypilot.navd.nav_tune import flag, tune


class NavigationDesires:
  def __init__(self):
    self.sm = messaging.SubMaster(['navigationd'])
    self.desire = log.Desire.none
    self.param_counter = -1
    # live-read flags/tunables (defaults until first read)
    self.master = False
    self.allow_turn = False
    self.allow_keep = False
    self.handsoff = False
    self.turn_speed = 20 * CV.MPH_TO_MS

  def update_params(self):
    self.param_counter += 1
    if self.param_counter % 60 == 0:  # every 3 seconds at 20hz
      self.master = flag('navsteer')
      self.allow_turn = flag('navsteer_turn')
      self.allow_keep = flag('navsteer_keep')
      self.handsoff = flag('navsteer_handsoff')
      self.turn_speed = tune('NavSteerTurnSpeed', 20) * CV.MPH_TO_MS

  def update(self, CS: car.CarState, lateral_active: bool) -> log.Desire:
    self.update_params()
    self.sm.update(0)
    nav_msg = self.sm['navigationd']
    self.desire = log.Desire.none
    if not (self.master and nav_msg.valid and lateral_active):
      return self.desire

    upcoming = nav_msg.upcomingTurn

    # Slight turn / fork -> keep nudge. Hands-on by default; handsoff drops the torque gate.
    if self.allow_keep:
      if upcoming == 'slightLeft' and not CS.rightBlinker and not CS.leftBlindspot \
         and (self.handsoff or (CS.steeringPressed and CS.steeringTorque > 0)):
        self.desire = log.Desire.keepLeft
      elif upcoming == 'slightRight' and not CS.leftBlinker and not CS.rightBlindspot \
           and (self.handsoff or (CS.steeringPressed and CS.steeringTorque < 0)):
        self.desire = log.Desire.keepRight

    # Hard turn -> turnLeft/turnRight, speed-gated (model can only execute low-speed turns).
    if self.desire == log.Desire.none and self.allow_turn:
      if upcoming == 'left' and not CS.rightBlinker and not CS.leftBlindspot and CS.vEgo < self.turn_speed:
        self.desire = log.Desire.turnLeft
      elif upcoming == 'right' and not CS.leftBlinker and not CS.rightBlindspot and CS.vEgo < self.turn_speed:
        self.desire = log.Desire.turnRight

    return self.desire
