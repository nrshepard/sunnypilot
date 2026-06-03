"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.sunnypilot.selfdrive.controls.lib.accel_personality.accel_controller import (
  AccelPersonalityController,
  AccelPersonality,
  A_MAX_V,
  A_MIN_V,
  LEAD_COAST_DECEL,
  LEAD_MPC_T_FOLLOW_MIN,
)
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpcSP
from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import (
  LongitudinalPlannerSP,
  JERK_RELEASE,
  JERK_RELEASE_CLOSING,
  JERK_BRAKE,
  JERK_BRAKE_COMFORT,
)
from openpilot.common.realtime import DT_MDL
from opendbc.car.interfaces import ACCEL_MIN

PERSONALITIES = (AccelPersonality.eco, AccelPersonality.normal, AccelPersonality.sport)


def _make(personality, v_cruise=0.0):
  c = AccelPersonalityController()
  c._personality = personality
  c._enabled = True
  c._v_cruise = v_cruise
  return c


class FakeLead:
  def __init__(self, status=False, d_rel=0.0, v_rel=0.0, v_lead=0.0, a_lead=0.0, fcw=False):
    self.status = status
    self.dRel = d_rel
    self.vRel = v_rel
    self.vLead = v_lead
    self.aLeadK = a_lead
    self.fcw = fcw


class FakeRadarState:
  def __init__(self, lead_one=None, lead_two=None):
    self.leadOne = lead_one or FakeLead()
    self.leadTwo = lead_two or FakeLead()


class FakeCarState:
  def __init__(self, v_ego=0.0, v_cruise=0.0):
    self.vEgo = v_ego
    self.vCruise = v_cruise


class FakeControlsState:
  def __init__(self, force_decel=False):
    self.forceDecel = force_decel


class FakeSelfdriveState:
  def __init__(self, experimental_mode=True):
    self.experimentalMode = experimental_mode


class FakeSM:
  def __init__(self, radarstate, v_ego=0.0, force_decel=False, v_cruise=0.0, experimental_mode=True):
    self._data = {
      'radarState': radarstate,
      'carState': FakeCarState(v_ego, v_cruise),
      'controlsState': FakeControlsState(force_decel),
      'selfdriveState': FakeSelfdriveState(experimental_mode),
    }

  def __getitem__(self, k):
    return self._data[k]


class FakeMpc:
  def __init__(self):
    self.crash_cnt = 0
    self.last_update = None
    self.last_weights = None

  def set_weights(self, prev_accel_constraint=True, personality=None, jerk_scale=1.0):
    self.last_weights = (prev_accel_constraint, personality, jerk_scale)

  def update(self, radarstate, v_cruise, personality=None, t_follow=None, accel_limits=None):
    self.last_update = (radarstate, v_cruise, personality, t_follow, accel_limits)


class FakeMpcProfileSink:
  def __init__(self):
    self.profile = None

  def set_profile(self, profile):
    self.profile = profile


class FakeDec:
  def __init__(self, mode="acc", active=True):
    self._mode = mode
    self._active = active

  def active(self):
    return self._active

  def mode(self):
    return self._mode


def _sm(lead_one=None, lead_two=None, v_ego=0.0, force_decel=False, v_cruise=0.0, experimental_mode=True):
  return FakeSM(FakeRadarState(lead_one, lead_two), v_ego, force_decel, v_cruise, experimental_mode)


def _rs(lead_one=None, lead_two=None):
  return FakeRadarState(lead_one, lead_two)


class TestGasCeiling:
  def test_positive(self):
    c = _make(AccelPersonality.normal)
    assert c.get_max_accel(8.0) > 0.5

  def test_sport_at_least_eco(self):
    eco = _make(AccelPersonality.eco)
    sport = _make(AccelPersonality.sport)
    for v in (0.0, 4.0, 8.0, 16.0, 40.0):
      assert sport.get_max_accel(v) >= eco.get_max_accel(v) - 1e-9

  def test_unset_cruise_no_rampoff(self):
    c = _make(AccelPersonality.normal, v_cruise=0.0)
    assert abs(c.get_max_accel(8.0) - A_MAX_V[AccelPersonality.normal][2]) < 1e-6

  def test_nan_cruise_no_rampoff(self):
    c = _make(AccelPersonality.normal, v_cruise=20.0)
    c.update(_sm(v_cruise=float("nan")))
    assert abs(c.get_max_accel(8.0) - A_MAX_V[AccelPersonality.normal][2]) < 1e-6

  def test_rampoff_at_and_above_setpoint(self):
    c = _make(AccelPersonality.normal, v_cruise=20.0)
    assert c.get_max_accel(20.0) == 0.0
    assert c.get_max_accel(25.0) == 0.0
    assert c.get_max_accel(10.0) > 0.0

  def test_rampoff_partial(self):
    c = _make(AccelPersonality.normal, v_cruise=20.0)
    full = _make(AccelPersonality.normal, v_cruise=0.0)
    assert 0.0 < c.get_max_accel(17.5) < full.get_max_accel(17.5)


class TestFollowDistance:
  def test_eco_loosest_sport_tightest(self):
    eco = _make(AccelPersonality.eco)
    normal = _make(AccelPersonality.normal)
    sport = _make(AccelPersonality.sport)
    assert eco.get_t_follow() > normal.get_t_follow() > sport.get_t_follow()

  def test_values_sane(self):
    for p in PERSONALITIES:
      assert 1.0 <= _make(p).get_t_follow() <= 2.0


class TestJerkScale:
  def test_eco_smoother_sport_snappier(self):
    eco = _make(AccelPersonality.eco)
    normal = _make(AccelPersonality.normal)
    sport = _make(AccelPersonality.sport)
    assert eco.get_jerk_scale() > normal.get_jerk_scale() > sport.get_jerk_scale()

  def test_normal_is_stock(self):
    assert _make(AccelPersonality.normal).get_jerk_scale() == 1.0


class TestMpcProfile:
  def test_far_closing_lead_lowers_follow_distance(self):
    c = _make(AccelPersonality.eco)
    lead = FakeLead(status=True, d_rel=60.0, v_lead=11.0)
    profile = c.get_mpc_profile(12.0, _rs(lead))
    assert LEAD_MPC_T_FOLLOW_MIN <= profile.t_follow < c.get_t_follow()
    assert profile.accel_min == c.get_min_accel(12.0, _rs(lead))
    assert profile.accel_max == c.get_max_accel(12.0)

  def test_non_closing_lead_keeps_base_follow_distance(self):
    c = _make(AccelPersonality.eco)
    lead = FakeLead(status=True, d_rel=60.0, v_lead=12.0)
    assert c.get_mpc_profile(12.0, _rs(lead)).t_follow == c.get_t_follow()

  def test_force_decel_keeps_stock_floor(self):
    c = _make(AccelPersonality.normal)
    assert c.get_mpc_profile(8.0, force_decel=True).accel_min == ACCEL_MIN

  def test_should_stop_keeps_stock_floor(self):
    c = _make(AccelPersonality.normal)
    assert c.get_mpc_profile(8.0, should_stop=True).accel_min == ACCEL_MIN


class TestLongitudinalMpcSP:
  def test_update_passes_profile_to_mpc(self):
    c = _make(AccelPersonality.eco)
    mpc = FakeMpc()
    mpc_sp = LongitudinalMpcSP(mpc, c)
    profile = c.get_mpc_profile(12.0, _rs(FakeLead(status=True, d_rel=60.0, v_lead=11.0)))
    mpc_sp.set_profile(profile)
    mpc_sp.update(_rs(), 20.0, personality=1, t_follow=9.0)

    assert mpc.last_update[3] == profile.t_follow
    assert mpc.last_update[4] == [profile.accel_min, profile.accel_max]

  def test_disabled_mpc_is_passthrough(self):
    c = _make(AccelPersonality.eco)
    c._enabled = False
    mpc = FakeMpc()
    mpc_sp = LongitudinalMpcSP(mpc, c)
    mpc_sp.set_profile(c.get_mpc_profile(12.0, _rs()))
    mpc_sp.update(_rs(), 20.0, personality=1, t_follow=1.5)

    assert mpc.last_update[3] == 1.5
    assert mpc.last_update[4] is None

  def test_nan_cruise_is_not_sent_to_mpc(self):
    c = _make(AccelPersonality.normal)
    mpc = FakeMpc()
    mpc_sp = LongitudinalMpcSP(mpc, c)
    mpc_sp.update(_rs(), float("nan"), personality=1)

    assert mpc.last_update[1] > 0.0


class TestPersonalityApi:
  def test_cycle(self):
    c = _make(AccelPersonality.eco)
    seen = {c.cycle_accel_personality() for _ in range(3)}
    assert seen == {int(p) for p in PERSONALITIES}

  def test_toggle_enabled(self):
    c = _make(AccelPersonality.normal)
    c.set_enabled(False)
    assert c.toggle_enabled() is True
    assert c.toggle_enabled() is False


class TestBrakeFloor:
  def test_profile_brake_floor(self):
    c = _make(AccelPersonality.normal)
    assert abs(c.get_profile_min_accel(15.0) - A_MIN_V[AccelPersonality.normal][2]) < 1e-6

  def test_eco_softest_sport_deepest(self):
    eco = _make(AccelPersonality.eco)
    normal = _make(AccelPersonality.normal)
    sport = _make(AccelPersonality.sport)
    for v in (0.0, 5.0, 15.0, 35.0):
      assert eco.get_min_accel(v) > normal.get_min_accel(v) > sport.get_min_accel(v)

  def test_disabled_uses_stock_floor(self):
    c = _make(AccelPersonality.normal)
    c.set_enabled(False)
    assert c.get_min_accel(10.0) == ACCEL_MIN

  def test_closing_lead_opens_more_brake(self):
    c = _make(AccelPersonality.normal)
    lead = FakeLead(status=True, d_rel=14.0, v_lead=7.0)
    profile_min = c.get_profile_min_accel(14.0)
    assert ACCEL_MIN <= c.get_min_accel(14.0, _rs(lead)) < profile_min

  def test_critical_lead_uses_stock_floor(self):
    c = _make(AccelPersonality.normal)
    lead = FakeLead(status=True, d_rel=8.0, v_lead=1.0)
    assert c.get_min_accel(16.0, _rs(lead)) == ACCEL_MIN

  def test_stop_and_force_decel_use_stock_floor(self):
    c = _make(AccelPersonality.normal)
    assert c.get_min_accel(8.0, should_stop=True) == ACCEL_MIN
    assert c.get_min_accel(8.0, force_decel=True) == ACCEL_MIN

  def test_e2e_output_uses_stock_floor(self):
    c = _make(AccelPersonality.eco)
    assert c.get_output_min_accel(12.0, e2e=True) == ACCEL_MIN

  def test_non_e2e_with_lead_keeps_lead_floor(self):
    c = _make(AccelPersonality.eco)
    lead = FakeLead(status=True, d_rel=45.0, v_lead=12.0)
    radarstate = _rs(lead)
    assert c.get_output_min_accel(12.0, radarstate) == c.get_min_accel(12.0, radarstate)

  def test_non_e2e_keeps_comfort_floor(self):
    c = _make(AccelPersonality.eco)
    assert c.get_output_min_accel(12.0) == c.get_min_accel(12.0)


class TestBrakeShaping:
  def test_far_closing_lead_coasts(self):
    c = _make(AccelPersonality.eco)
    lead = FakeLead(status=True, d_rel=60.0, v_lead=11.0)
    shaped = c.shape_decel(12.0, -2.0, _rs(lead))
    assert shaped > c.get_profile_min_accel(12.0)
    assert shaped >= -LEAD_COAST_DECEL

  def test_non_closing_lead_brake_untouched(self):
    c = _make(AccelPersonality.eco)
    lead = FakeLead(status=True, d_rel=45.0, v_lead=12.0)
    assert c.shape_decel(12.0, -2.0, _rs(lead)) == -2.0

  def test_closing_lead_adds_early_brake(self):
    c = _make(AccelPersonality.normal)
    lead = FakeLead(status=True, d_rel=24.0, v_lead=10.0)
    shaped = c.shape_decel(14.0, 0.1, _rs(lead))
    assert shaped < 0.1
    assert shaped < -LEAD_COAST_DECEL
    assert shaped >= c.get_min_accel(14.0, _rs(lead))

  def test_disabled_shape_is_noop(self):
    c = _make(AccelPersonality.normal)
    c.set_enabled(False)
    lead = FakeLead(status=True, d_rel=45.0, v_lead=12.0)
    assert c.shape_decel(12.0, -2.0, _rs(lead)) == -2.0


class TestPlannerBrakeHook:
  def test_stop_not_raised_to_comfort_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(FakeLead(status=True, d_rel=45.0, v_lead=12.0), v_ego=12.0)
    p._smoothed_radarstate = None
    p.output_should_stop = True

    assert p._apply_accel_personality_decel(-2.0) == -2.0

  def test_release_rate_tightens_when_lead_closing(self):
    p = object.__new__(LongitudinalPlannerSP)
    p._smoothed_radarstate = _rs(FakeLead(status=True, v_rel=-5.0))
    assert p._release_rate() == JERK_RELEASE_CLOSING
    p._smoothed_radarstate = _rs(lead_two=FakeLead(status=True, v_rel=-5.0))
    assert p._release_rate() == JERK_RELEASE_CLOSING
    p._smoothed_radarstate = _rs(FakeLead(status=True, v_rel=1.0))
    assert p._release_rate() == JERK_RELEASE
    p._smoothed_radarstate = None
    assert p._release_rate() == JERK_RELEASE

  def test_brake_rate_is_comfort_without_urgency(self):
    p = object.__new__(LongitudinalPlannerSP)
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()
    p.output_should_stop = False

    assert p._brake_rate() == JERK_BRAKE_COMFORT

  def test_brake_rate_keeps_authority_for_stop(self):
    p = object.__new__(LongitudinalPlannerSP)
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()
    p.output_should_stop = True

    assert p._brake_rate() == JERK_BRAKE

  def test_brake_rate_keeps_authority_for_closing_lead(self):
    p = object.__new__(LongitudinalPlannerSP)
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs(FakeLead(status=True, v_rel=-5.0))
    p.output_should_stop = False

    assert p._brake_rate() == JERK_BRAKE

  def test_output_target_smooths_comfort_brake(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()
    p.output_should_stop = False
    p._output_a_target = 0.0

    p.output_a_target = -1.0

    assert abs(p.output_a_target - (-JERK_BRAKE_COMFORT * DT_MDL)) < 1e-9

  def test_output_target_smooths_return_to_accel(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()
    p.output_should_stop = False
    p._output_a_target = -1.0

    p.output_a_target = 1.0

    assert abs(p.output_a_target - (-1.0 + JERK_RELEASE * DT_MDL)) < 1e-9

  def test_mpc_profile_feeds_planner_hooks(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p._mpc_profile = p.accel_controller.get_mpc_profile(
      12.0, _rs(FakeLead(status=True, d_rel=60.0, v_lead=11.0)))

    assert p.get_t_follow() == p._mpc_profile.t_follow
    assert p.get_jerk_scale() == p._mpc_profile.jerk_scale

  def test_accel_clip_builds_mpc_profile(self):
    lead = FakeLead(status=True, d_rel=60.0, v_lead=11.0)
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p.mpc = FakeMpcProfileSink()
    p._last_plan_sm = _sm(lead, v_ego=12.0)
    p._mpc_profile = None
    p._smoothed_radarstate = _rs(lead)
    p.output_should_stop = False

    clip = p.get_accel_clip(12.0)
    assert clip[0] == p._mpc_profile.accel_min
    assert p.get_t_follow() == p._mpc_profile.t_follow
    assert p.mpc.profile == p._mpc_profile

  def test_accel_clip_stop_uses_stock_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p.mpc = FakeMpcProfileSink()
    p._last_plan_sm = _sm(v_ego=8.0)
    p._mpc_profile = None
    p._smoothed_radarstate = _rs()
    p.output_should_stop = True

    assert p.get_accel_clip(8.0)[0] == ACCEL_MIN

  def test_accel_clip_blended_uses_stock_floor(self):
    lead = FakeLead(status=True, d_rel=60.0, v_lead=11.0)  # would get gentle floor in ACC
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec(mode="blended")
    p.mpc = FakeMpcProfileSink()
    p._last_plan_sm = _sm(lead, v_ego=12.0)
    p._mpc_profile = None
    p._smoothed_radarstate = _rs(lead)
    p.output_should_stop = False

    assert p.get_accel_clip(12.0)[0] == ACCEL_MIN  # blended -> stock min, model not capped

  def test_update_accel_clip_stop_uses_stock_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(v_ego=8.0)
    p._smoothed_radarstate = _rs()

    clip = p.update_accel_clip([-0.5, 0.4], should_stop=True, force_decel=False)
    assert clip == [ACCEL_MIN, 0.4]

  def test_update_accel_clip_blended_uses_stock_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec("blended")
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()

    assert p.update_accel_clip([-0.6, 0.4], should_stop=False, force_decel=False) == [ACCEL_MIN, 0.4]

  def test_update_accel_clip_acc_policy_with_lead_keeps_lead_floor(self):
    lead = FakeLead(status=True, d_rel=45.0, v_lead=12.0)
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(lead, v_ego=12.0)
    p._smoothed_radarstate = _rs(lead)

    clip = p.update_accel_clip([-0.6, 0.4], should_stop=False, force_decel=False)
    assert clip == [p.accel_controller.get_min_accel(12.0, _rs(lead)), 0.4]

  def test_update_accel_clip_disabled_is_noop(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.accel_controller._enabled = False

    assert p.update_accel_clip([-0.5, 0.4], should_stop=True, force_decel=False) == [-0.5, 0.4]

  def test_disabled_output_is_not_rate_limited(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.accel_controller._enabled = False
    p._output_a_target = -2.0

    p.output_a_target = 1.0
    assert p.output_a_target == 1.0

  def test_disabled_stop_hold_is_noop(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.accel_controller._enabled = False

    assert p.stop_hold(0.1, 0.5, True) == (0.5, True)

  def test_force_decel_not_raised_to_comfort_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(FakeLead(status=True, d_rel=45.0, v_lead=12.0), v_ego=12.0, force_decel=True)
    p._smoothed_radarstate = None
    p.output_should_stop = False

    assert p._apply_accel_personality_decel(-2.0) == -2.0

  def test_blended_decel_not_raised_to_comfort_floor(self):
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec("blended")
    p._last_plan_sm = _sm(v_ego=12.0)
    p._smoothed_radarstate = _rs()
    p.output_should_stop = False

    assert p._apply_accel_personality_decel(-1.5) == -1.5

  def test_acc_policy_lead_decel_uses_lead_floor(self):
    lead = FakeLead(status=True, d_rel=45.0, v_lead=12.0)
    p = object.__new__(LongitudinalPlannerSP)
    p.accel_controller = _make(AccelPersonality.eco)
    p.dec = FakeDec()
    p._last_plan_sm = _sm(lead, v_ego=12.0)
    p._smoothed_radarstate = _rs(lead)
    p.output_should_stop = False

    assert p._apply_accel_personality_decel(-1.5) == p.accel_controller.get_min_accel(12.0, _rs(lead))
