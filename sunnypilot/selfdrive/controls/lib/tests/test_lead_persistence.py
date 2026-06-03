"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.lead_persistence.lead_persistence import (
  LeadPersistence,
  _HOLD_FRAMES,
)


class FakeLead:
  def __init__(self, status=False, d_rel=0.0, v_rel=0.0, v_lead=0.0,
               a_lead=0.0, a_tau=0.0, model_prob=0.0, y_rel=0.0, a_rel=0.0, fcw=False):
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
  def __init__(self, lead_one=None, lead_two=None, extra='ok'):
    self.leadOne = lead_one or FakeLead()
    self.leadTwo = lead_two or FakeLead()
    self.extra = extra


def _make():
  return LeadPersistence()


class TestLeadPersistence:
  def test_disabled_passthrough(self):
    lp = _make()
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    lp.update(raw, force_enabled=False)
    out = lp.smooth(raw, force_enabled=False)
    assert out is raw

  def test_status_true_passthrough(self):
    lp = _make()
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0, v_lead=20.0))
    for _ in range(3):
      lp.update(raw)
    out = lp.smooth(raw)
    # leadOne still truly present → passthrough (or wrapper with same .status=True)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 30.0

  def test_dropout_held(self):
    lp = _make()
    raw_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0, v_lead=20.0, a_lead=-1.0))
    for _ in range(5):
      lp.update(raw_on)
    raw_off = FakeRadarState(lead_one=FakeLead(status=False))
    lp.update(raw_off)
    out = lp.smooth(raw_off)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 30.0
    assert out.leadOne.aLeadK == -1.0

  def test_dropout_expires(self):
    lp = _make()
    raw_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(5):
      lp.update(raw_on)
    raw_off = FakeRadarState(lead_one=FakeLead(status=False))
    for _ in range(_HOLD_FRAMES + 2):
      lp.update(raw_off)
    out = lp.smooth(raw_off)
    assert out.leadOne.status is False

  def test_reappearance_resets_hold(self):
    lp = _make()
    raw_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(5):
      lp.update(raw_on)
    raw_off = FakeRadarState(lead_one=FakeLead(status=False))
    for _ in range(3):
      lp.update(raw_off)
    raw_on2 = FakeRadarState(lead_one=FakeLead(status=True, d_rel=28.0))
    lp.update(raw_on2)
    out = lp.smooth(raw_on2)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 28.0

  def test_other_attrs_passthrough(self):
    lp = _make()
    raw_on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0), extra='special')
    for _ in range(3):
      lp.update(raw_on)
    raw_off = FakeRadarState(lead_one=FakeLead(status=False), extra='special2')
    lp.update(raw_off)
    out = lp.smooth(raw_off)
    assert out.extra == 'special2'

  def test_stability_high_on_solid_lead(self):
    lp = _make()
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(10):
      lp.update(raw)
    assert lp.stability >= 0.9

  def test_stability_low_on_churn(self):
    lp = _make()
    on = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    off = FakeRadarState(lead_one=FakeLead(status=False))
    for _ in range(10):
      lp.update(on)
      lp.update(off)
    assert lp.stability < 0.5

  def test_leadtwo_independent(self):
    lp = _make()
    raw_on = FakeRadarState(
      lead_one=FakeLead(status=True, d_rel=30.0),
      lead_two=FakeLead(status=True, d_rel=80.0),
    )
    for _ in range(5):
      lp.update(raw_on)
    raw_off2 = FakeRadarState(
      lead_one=FakeLead(status=True, d_rel=30.0),
      lead_two=FakeLead(status=False),
    )
    lp.update(raw_off2)
    out = lp.smooth(raw_off2)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 30.0
    assert out.leadTwo.status is True
    assert out.leadTwo.dRel == 80.0

  def test_reset(self):
    lp = _make()
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=30.0))
    for _ in range(5):
      lp.update(raw)
    lp.reset()
    raw_off = FakeRadarState(lead_one=FakeLead(status=False))
    lp.update(raw_off)
    out = lp.smooth(raw_off)
    assert out.leadOne.status is False


def _step(lp, lead):
  raw = FakeRadarState(lead_one=lead)
  lp.update(raw)
  return lp.smooth(raw)


class TestPhantomMask:
  def test_masked_when_not_urgent(self):
    lp = _make()
    # fresh, low modelProb, close, slow closing (TTC 8s) -> phantom ghost -> masked
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=4.0, v_rel=-0.5, model_prob=0.3))
    lp.update(raw)
    out = lp.smooth(raw)
    assert out.leadOne.status is False

  def test_not_masked_when_urgent_ttc(self):
    lp = _make()
    # fresh, low modelProb, close, TTC 3s <= 4 -> real cut-in risk -> NOT masked
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=3.0, v_rel=-1.0, model_prob=0.3))
    lp.update(raw)
    out = lp.smooth(raw)
    assert out.leadOne.status is True
    assert out.leadOne.dRel == 3.0

  def test_not_masked_when_fast_closing(self):
    lp = _make()
    # fresh, low modelProb, close, vRel -10 <= -8 -> NOT masked
    raw = FakeRadarState(lead_one=FakeLead(status=True, d_rel=4.0, v_rel=-10.0, model_prob=0.3))
    lp.update(raw)
    out = lp.smooth(raw)
    assert out.leadOne.status is True


class TestLeadSwitchSlew:
  def _settle_far(self, lp, d=45.0):
    out = None
    for _ in range(6):
      out = _step(lp, FakeLead(status=True, d_rel=d, v_rel=-3.0, v_lead=22.0))
    return out

  def test_steady_lead_no_slew(self):
    lp = _make()
    out = self._settle_far(lp, d=45.0)
    assert out.leadOne.dRel == 45.0
    assert lp.slew_offset == 0.0

  def test_switch_lags_dRel_keeps_true_vrel(self):
    lp = _make()
    self._settle_far(lp, d=45.0)
    # switch to a nearer car: 28 m, closing -5 (TTC 5.6 s > 4) -> non-urgent
    out = _step(lp, FakeLead(status=True, d_rel=28.0, v_rel=-5.0, v_lead=19.0))
    assert out.leadOne.dRel > 40.0          # reported stays continuous, not stepped to 28
    assert out.leadOne.dRel >= 28.0          # never under-reports distance
    assert out.leadOne.vRel == -5.0          # true closing speed passes through
    assert out.leadOne.vLead == 19.0

  def test_switch_converges_to_truth(self):
    lp = _make()
    self._settle_far(lp, d=45.0)
    out = _step(lp, FakeLead(status=True, d_rel=28.0, v_rel=-5.0, v_lead=19.0))
    prev = out.leadOne.dRel
    for _ in range(40):
      out = _step(lp, FakeLead(status=True, d_rel=28.0, v_rel=-5.0, v_lead=19.0))
      assert out.leadOne.dRel <= prev + 1e-6      # monotonically eases down
      assert out.leadOne.dRel >= 28.0 - 1e-6      # never below truth
      prev = out.leadOne.dRel
    assert abs(out.leadOne.dRel - 28.0) < 1.0     # converged to real distance

  def test_urgent_switch_passes_through(self):
    lp = _make()
    self._settle_far(lp, d=45.0)
    # nearer car at 12 m closing -5 -> TTC 2.4 s < 4 -> urgent, no masking
    out = _step(lp, FakeLead(status=True, d_rel=12.0, v_rel=-5.0, v_lead=10.0))
    assert out.leadOne.dRel == 12.0
    assert lp.slew_offset == 0.0

  def test_fast_closing_switch_passes_through(self):
    lp = _make()
    self._settle_far(lp, d=60.0)
    # vRel -10 <= -8 -> emergency closing, no masking even though TTC ok
    out = _step(lp, FakeLead(status=True, d_rel=40.0, v_rel=-10.0, v_lead=15.0))
    assert out.leadOne.dRel == 40.0
    assert lp.slew_offset == 0.0

  def test_normal_closing_not_slewed(self):
    lp = _make()
    d = 45.0
    self._settle_far(lp, d=d)
    for _ in range(20):
      d -= 0.25  # ~5 m/s closing at 20 Hz, continuous (not a switch)
      out = _step(lp, FakeLead(status=True, d_rel=d, v_rel=-5.0, v_lead=20.0))
      assert out.leadOne.dRel == d
      assert lp.slew_offset == 0.0

  def test_lead_moving_away_not_slewed(self):
    lp = _make()
    self._settle_far(lp, d=30.0)
    # sudden farther jump (lead drops out / switch to far car) -> no brake risk, no lag
    out = _step(lp, FakeLead(status=True, d_rel=60.0, v_rel=-1.0, v_lead=24.0))
    assert out.leadOne.dRel == 60.0
    assert lp.slew_offset == 0.0
