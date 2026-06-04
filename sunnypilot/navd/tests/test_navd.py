"""
Unit tests for the mici-nav navd port — pure logic (no device / cereal needed).
Runs in CI / on-device where openpilot.common is importable.

Covers: haversine distance, bearing (radians-correct), maneuver-direction parsing,
banner parsing, and the Mapbox directions parser incl. malformed-response robustness.
"""
import pytest

from openpilot.sunnypilot.navd.helpers import (
  Coordinate, bearing_between_two_points, string_to_direction, parse_banner_instructions,
)
from openpilot.sunnypilot.navd.navigation_helpers import mapbox_integration
from openpilot.sunnypilot.navd.navigation_helpers.mapbox_integration import MapboxIntegration


class TestCoordinate:
  def test_distance_equator_one_deg_lon(self):
    assert abs(Coordinate(0, 0).distance_to(Coordinate(0, 1)) - 111195) < 50

  def test_distance_zero(self):
    assert Coordinate(30, -97).distance_to(Coordinate(30, -97)) < 1e-6


class TestBearing:
  @pytest.mark.parametrize("a,b,expected", [
    (Coordinate(0, 0), Coordinate(1, 0), 0),     # north
    (Coordinate(0, 0), Coordinate(0, 1), 90),    # east
    (Coordinate(1, 0), Coordinate(0, 0), 180),   # south
    (Coordinate(0, 1), Coordinate(0, 0), 270),   # west
  ])
  def test_cardinal(self, a, b, expected):
    assert abs(bearing_between_two_points(a, b) - expected) < 1.0


class TestStringToDirection:
  @pytest.mark.parametrize("inp,exp", [
    ("slight left", "slightLeft"), ("sharp right", "sharpRight"),
    ("left", "left"), ("right", "right"), ("straight", "straight"), ("uturn", "none"),
  ])
  def test_maps(self, inp, exp):
    assert string_to_direction(inp) == exp


class TestBanner:
  def test_primary_and_modifier(self):
    banners = [{"distanceAlongGeometry": 500.0,
                "primary": {"text": "Main St", "type": "turn", "modifier": "left"}}]
    ins = parse_banner_instructions(banners, distance_to_maneuver=100.0)
    assert ins["maneuverPrimaryText"] == "Main St"
    assert ins["maneuverModifier"] == "left"

  def test_empty(self):
    assert parse_banner_instructions([], 0.0) is None


class _FakeResp:
  status_code = 200
  def __init__(self, j): self._j = j
  def json(self): return self._j


def _patch_requests(monkeypatch, payload):
  class FakeReq:
    RequestException = Exception
    @staticmethod
    def get(*a, **k): return _FakeResp(payload)
  monkeypatch.setattr(mapbox_integration, "requests", FakeReq)


class TestGenerateRoute:
  FIX = {"code": "Ok", "routes": [{"distance": 1000.0, "duration": 120.0,
    "geometry": {"coordinates": [[-97.70, 30.30], [-97.71, 30.31]]},
    "legs": [{"steps": [
        {"distance": 500.0, "duration": 60.0,
         "maneuver": {"type": "turn", "instruction": "Turn left onto Main St",
                      "location": [-97.70, 30.30], "modifier": "left"},
         "bannerInstructions": [{"distanceAlongGeometry": 500.0}]},
        {"distance": 0.0, "duration": 0.0,
         "maneuver": {"type": "arrive", "instruction": "Arrive", "location": [-97.71, 30.31]}},
      ],
      "annotation": {"maxspeed": [{"speed": 30, "unit": "mph"}, {"unknown": True}]}}]}]}

  def test_parses_steps(self, monkeypatch):
    _patch_requests(monkeypatch, self.FIX)
    r = MapboxIntegration.generate_route(-97.70, 30.30, -97.71, 30.31, token="pk.test")
    assert r["totalDistance"] == 1000.0
    assert r["steps"][0]["modifier"] == "left"
    assert r["steps"][1]["modifier"] == "none"            # arrive step has no modifier
    assert r["steps"][1]["bannerInstructions"] == []      # .get guard
    assert r["maxspeed"] == [{"speed": 30, "unit": "mph"}]  # no-speed item filtered

  def test_missing_annotation_no_crash(self, monkeypatch):
    payload = {"code": "Ok", "routes": [{"distance": 5.0, "duration": 1.0,
      "geometry": {"coordinates": [[0, 0], [0, 0]]},
      "legs": [{"steps": [{"distance": 5.0, "duration": 1.0,
          "maneuver": {"type": "arrive", "instruction": "Arrive", "location": [0, 0]}}]}]}]}
    _patch_requests(monkeypatch, payload)
    r = MapboxIntegration.generate_route(0, 0, 0, 0, token="pk.test")
    assert r is not None and r["maxspeed"] == []

  def test_empty_token(self):
    assert MapboxIntegration.generate_route(0, 0, 0, 0, token="") is None
