"""
Copyright (c) 2021-, rav4kumar, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from collections import deque
from dataclasses import dataclass

from openpilot.common.realtime import DT_MDL


_HOLD_FRAMES = 16
_STATUS_WINDOW = 20
_STABILITY_FLIPS_FULL = 6.0

# Lead target-switch slew: on a sudden closer dRel jump, lag reported dRel and decay to
# truth (true vRel/vLead/aLeadK pass through); urgent switches pass through.
_SWITCH_STEP_THRESH = 5.0        # m, single-frame closer jump = switch
_SWITCH_TTC_MIN = 4.0            # s, below this = urgent, no masking
_SWITCH_PASSTHROUGH_VREL = -8.0  # m/s, faster closing = no masking
_SLEW_DECAY = 0.88               # per-frame offset decay
_SLEW_OFFSET_MAX = 25.0          # m
_SLEW_OFFSET_EPS = 0.5           # m, snap to zero below this

# Phantom-lead mask: a fresh low-modelProb close lead is usually a radar ghost; mask it
# so the MPC ignores it. Established/held leads are never masked (see smooth()).
_PHANTOM_MODELPROB_MAX = 0.5
_PHANTOM_DREL_MAX = 5.0


@dataclass
class _LeadSnap:
  dRel: float = 0.0
  yRel: float = 0.0
  vRel: float = 0.0
  vLead: float = 0.0
  aLeadK: float = 0.0
  aLeadTau: float = 0.0
  modelProb: float = 0.0
  aRel: float = 0.0
  fcw: bool = False


class _LeadProxy:
  __slots__ = ('status', 'dRel', 'yRel', 'vRel', 'vLead', 'aLeadK', 'aLeadTau',
               'modelProb', 'aRel', 'fcw')

  def __init__(self, snap: _LeadSnap):
    self.status = True
    self.dRel = snap.dRel
    self.yRel = snap.yRel
    self.vRel = snap.vRel
    self.vLead = snap.vLead
    self.aLeadK = snap.aLeadK
    self.aLeadTau = snap.aLeadTau
    self.modelProb = snap.modelProb
    self.aRel = snap.aRel
    self.fcw = snap.fcw


class _LeadProxyMasked:
  __slots__ = ('status', 'dRel', 'yRel', 'vRel', 'vLead', 'aLeadK', 'aLeadTau',
               'modelProb', 'aRel', 'fcw')

  def __init__(self):
    self.status = False
    self.dRel = 0.0
    self.yRel = 0.0
    self.vRel = 0.0
    self.vLead = 0.0
    self.aLeadK = 0.0
    self.aLeadTau = 0.0
    self.modelProb = 0.0
    self.aRel = 0.0
    self.fcw = False


class _RadarStateProxy:
  __slots__ = ('_raw', '_lead_one', '_lead_two')

  def __init__(self, raw, lead_one, lead_two):
    self._raw = raw
    self._lead_one = lead_one
    self._lead_two = lead_two

  @property
  def leadOne(self):
    return self._lead_one if self._lead_one is not None else self._raw.leadOne

  @property
  def leadTwo(self):
    return self._lead_two if self._lead_two is not None else self._raw.leadTwo

  def __getattr__(self, name):
    return getattr(self._raw, name)


class LeadPersistence:
  """Internal helper. Hold last-known leadOne/leadTwo alive for HOLD_FRAMES
  after a status drop, so the MPC view of radarState ignores brief flicker.
  Also masks freshly-acquired close-range low-confidence phantom leads so the
  planner doesn't demand emergency brake on radar ghosts.
  No own param — owner (RadarDistanceController) gates via force_enabled."""

  def __init__(self):
    self._last_one: _LeadSnap | None = None
    self._last_two: _LeadSnap | None = None
    self._alive_one = 0
    self._alive_two = 0

    self._status_hist: deque[bool] = deque(maxlen=_STATUS_WINDOW)
    self._stability = 1.0

    # lead target-switch slew state (leadOne only)
    self._slew_offset = 0.0
    self._reported_one_dRel: float | None = None
    self._prev_raw_one_dRel: float | None = None
    self._prev_raw_one_vRel = 0.0

  @property
  def stability(self) -> float:
    return self._stability

  @property
  def slew_offset(self) -> float:
    return self._slew_offset

  def reset(self) -> None:
    self._last_one = None
    self._last_two = None
    self._alive_one = 0
    self._alive_two = 0
    self._status_hist.clear()
    self._stability = 1.0
    self._slew_offset = 0.0
    self._reported_one_dRel = None
    self._prev_raw_one_dRel = None
    self._prev_raw_one_vRel = 0.0

  def update(self, radarstate, force_enabled: bool = True) -> None:
    if radarstate is None:
      return
    if not force_enabled:
      self.reset()
      return

    one = radarstate.leadOne
    two = radarstate.leadTwo

    # phantom counts as not-valid so hold-alive doesn't re-inject the ghost
    one_valid = bool(one.status) and not self._is_phantom(one)
    two_valid = bool(two.status) and not self._is_phantom(two)

    self._compute_switch_slew(one, one_valid)

    if one_valid:
      self._last_one = self._snap(one)
      self._alive_one = _HOLD_FRAMES
    elif self._alive_one > 0:
      self._alive_one -= 1

    if two_valid:
      self._last_two = self._snap(two)
      self._alive_two = _HOLD_FRAMES
    elif self._alive_two > 0:
      self._alive_two -= 1

    self._status_hist.append(bool(one.status))
    if len(self._status_hist) >= 5:
      flips = sum(1 for i in range(1, len(self._status_hist))
                  if self._status_hist[i] != self._status_hist[i - 1])
      self._stability = max(0.0, 1.0 - min(1.0, flips / _STABILITY_FLIPS_FULL))
    else:
      self._stability = 1.0

  def _compute_switch_slew(self, one, one_valid: bool) -> None:
    """Carry a decaying dRel lag across a leadOne target-switch; urgent switches pass through."""
    if not one_valid:
      self._slew_offset = 0.0
      self._reported_one_dRel = None
      self._prev_raw_one_dRel = None
      self._prev_raw_one_vRel = 0.0
      return

    raw_d = float(one.dRel)
    raw_v = float(one.vRel)
    ttc = raw_d / max(0.1, -raw_v) if raw_v < 0.0 else float('inf')
    urgent = ttc <= _SWITCH_TTC_MIN or raw_v <= _SWITCH_PASSTHROUGH_VREL

    if urgent:
      self._slew_offset = 0.0
    else:
      self._slew_offset = self._slew_offset * _SLEW_DECAY if self._slew_offset > _SLEW_OFFSET_EPS else 0.0
      if self._prev_raw_one_dRel is not None and self._reported_one_dRel is not None:
        expected = self._prev_raw_one_dRel + self._prev_raw_one_vRel * DT_MDL
        if raw_d < expected - _SWITCH_STEP_THRESH:  # closer jump = switch
          new_offset = self._reported_one_dRel - raw_d
          self._slew_offset = min(_SLEW_OFFSET_MAX, max(self._slew_offset, new_offset))

    self._reported_one_dRel = raw_d + self._slew_offset
    self._prev_raw_one_dRel = raw_d
    self._prev_raw_one_vRel = raw_v

  @staticmethod
  def _is_phantom(lead) -> bool:
    if not (bool(lead.status)
            and float(lead.modelProb) < _PHANTOM_MODELPROB_MAX
            and float(lead.dRel) < _PHANTOM_DREL_MAX):
      return False
    # don't mask an urgently-closing lead
    v_rel = float(lead.vRel)
    if v_rel <= _SWITCH_PASSTHROUGH_VREL:
      return False
    ttc = float(lead.dRel) / max(0.1, -v_rel) if v_rel < 0.0 else float('inf')
    return ttc > _SWITCH_TTC_MIN

  def smooth(self, radarstate, force_enabled: bool = True):
    if not force_enabled or radarstate is None:
      return radarstate

    l1 = None
    l2 = None

    # mask only a fresh ghost (alive==0); never drop an established/held lead
    if self._is_phantom(radarstate.leadOne) and self._alive_one == 0:
      l1 = _LeadProxyMasked()
    elif not radarstate.leadOne.status and self._alive_one > 0 and self._last_one is not None:
      l1 = _LeadProxy(self._last_one)
    elif radarstate.leadOne.status and self._slew_offset > _SLEW_OFFSET_EPS:
      # target-switch slew: lag dRel only, true vRel/vLead/aLeadK pass through
      snap = self._snap(radarstate.leadOne)
      snap.dRel = snap.dRel + self._slew_offset
      l1 = _LeadProxy(snap)

    if self._is_phantom(radarstate.leadTwo) and self._alive_two == 0:
      l2 = _LeadProxyMasked()
    elif not radarstate.leadTwo.status and self._alive_two > 0 and self._last_two is not None:
      l2 = _LeadProxy(self._last_two)

    if l1 is None and l2 is None:
      return radarstate

    return _RadarStateProxy(radarstate, l1, l2)

  @staticmethod
  def _snap(lead) -> _LeadSnap:
    return _LeadSnap(
      dRel=float(lead.dRel),
      yRel=float(lead.yRel),
      vRel=float(lead.vRel),
      vLead=float(lead.vLead),
      aLeadK=float(lead.aLeadK),
      aLeadTau=float(lead.aLeadTau),
      modelProb=float(lead.modelProb),
      aRel=float(lead.aRel),
      fcw=bool(lead.fcw),
    )
