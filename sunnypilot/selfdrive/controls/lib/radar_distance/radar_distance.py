"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.lead_persistence.lead_persistence import LeadPersistence


_PARAM_REFRESH_FRAMES = max(1, int(1.0 / DT_MDL))


class RadarDistanceController:
  """Holds last-known radar leads alive through brief flicker and masks close-range
  phantom leads, so the MPC view of radarState stays stable. The behavior lives in
  LeadPersistence; this owns the RadarDistance param gate."""

  def __init__(self):
    self.params = Params()
    self._frame = 0
    self._enabled = self.params.get_bool('RadarDistance')
    self._lead_persistence = LeadPersistence()

  def is_enabled(self) -> bool:
    return self._enabled

  def set_enabled(self, enabled: bool):
    self._enabled = bool(enabled)
    self.params.put_bool('RadarDistance', self._enabled)

  def toggle(self) -> bool:
    self.set_enabled(not self._enabled)
    return self._enabled

  def update(self, sm=None, sm_sp=None) -> None:
    self._frame += 1
    if self._frame % _PARAM_REFRESH_FRAMES == 0:
      self._enabled = self.params.get_bool('RadarDistance')

    radarstate = None
    if sm is not None:
      try:
        radarstate = sm['radarState']
      except (KeyError, AttributeError, TypeError):
        radarstate = None

    self._lead_persistence.update(radarstate, force_enabled=self._enabled)

  def smooth_radarstate(self, radarstate):
    if not self._enabled:
      return radarstate
    return self._lead_persistence.smooth(radarstate, force_enabled=True)

  def reset(self) -> None:
    self._lead_persistence.reset()
