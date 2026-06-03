"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.radar_distance.radar_distance import RadarDistanceController
from openpilot.sunnypilot.selfdrive.controls.lib.lead_persistence.lead_persistence import _HOLD_FRAMES


class FakeLead:
  def __init__(self, status=False, d_rel=0.0, v_rel=0.0, v_lead=0.0, a_lead=0.0,
               a_tau=0.0, model_prob=0.95, y_rel=0.0, a_rel=0.0, fcw=False):
    self.status = status
    self.dRel = d_rel
    self.yRel = y_rel
    self.vRel = v_rel
    self.vLead = v_lead
    self.aLeadK = a_lead
    self.aLeadTau = a_tau
    self.modelProb = model_prob
    self.aRel = a_rel
    self.fcw = fcw


class FakeRadarState:
  def __init__(self, lead_one=None, lead_two=None):
    self.leadOne = lead_one or FakeLead()
    self.leadTwo = lead_two or FakeLead()


class FakeSM:
  def __init__(self, radarstate):
    self._data = {'radarState': radarstate}

  def __getitem__(self, k):
    return self._data[k]


def _make(enabled=True):
  c = RadarDistanceController()
  c.set_enabled(enabled)
  return c


class TestRadarDistanceController:
  def test_toggle(self):
    c = _make(enabled=False)
    assert c.is_enabled() is False
    assert c.toggle() is True
    assert c.is_enabled() is True
    assert c.toggle() is False

  def test_disabled_passthrough(self):
    c = _make(enabled=False)
    rs = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    c.update(FakeSM(rs))
    assert c.smooth_radarstate(rs) is rs

  def test_no_sm_safe(self):
    c = _make()
    c.update(None, None)  # must not raise

  def test_holds_dropped_lead(self):
    c = _make()
    rs_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0, v_lead=20.0, a_lead=-1.0))
    for _ in range(5):
      c.update(FakeSM(rs_on))
    rs_off = FakeRadarState(lead_one=FakeLead(status=False))
    c.update(FakeSM(rs_off))
    out = c.smooth_radarstate(rs_off)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 30.0
    assert out.leadOne.aLeadK == -1.0

  def test_hold_expires(self):
    c = _make()
    rs_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(5):
      c.update(FakeSM(rs_on))
    rs_off = FakeRadarState(lead_one=FakeLead(status=False))
    for _ in range(_HOLD_FRAMES + 2):
      c.update(FakeSM(rs_off))
    out = c.smooth_radarstate(rs_off)
    assert out.leadOne.status is False

  def test_reset_clears_hold(self):
    c = _make()
    rs_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(5):
      c.update(FakeSM(rs_on))
    c.reset()
    rs_off = FakeRadarState(lead_one=FakeLead(status=False))
    c.update(FakeSM(rs_off))
    out = c.smooth_radarstate(rs_off)
    assert out.leadOne.status is False
