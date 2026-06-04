import pyray as rl
import time
from math import pi, cos, sin
from dataclasses import dataclass
from openpilot.common.constants import CV
from openpilot.selfdrive.ui.mici.onroad.torque_bar import TorqueBar
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.common.filter_simple import FirstOrderFilter
from cereal import log

# Telemetry: pin the UI-process + NavManeuver per-frame cost into the streaming logger
# so a CPU overdraw can be isolated to the widget vs. the rest of the heavy UI process.
# Guarded import — UI must never crash if the navd tree is absent.
try:
  from openpilot.sunnypilot.navd import navlog as _navlog
except Exception:
  _navlog = None

EventName = log.OnroadEvent.EventName

# Constants
SET_SPEED_NA = 255
KM_TO_MILE = 0.621371
CRUISE_DISABLED_CHAR = '–'

SET_SPEED_PERSISTENCE = 2.5  # seconds


@dataclass(frozen=True)
class FontSizes:
  current_speed: int = 176
  speed_unit: int = 66
  max_speed: int = 36
  set_speed: int = 112


@dataclass(frozen=True)
class Colors:
  WHITE = rl.WHITE
  WHITE_TRANSLUCENT = rl.Color(255, 255, 255, 200)


FONT_SIZES = FontSizes()
COLORS = Colors()


class TurnIntent(Widget):
  FADE_IN_ANGLE = 30  # degrees

  def __init__(self):
    super().__init__()
    self._pre = False
    self._turn_intent_direction: int = 0

    self._turn_intent_alpha_filter = FirstOrderFilter(0, 0.05, 1 / gui_app.target_fps)
    self._turn_intent_rotation_filter = FirstOrderFilter(0, 0.1, 1 / gui_app.target_fps)

    self._txt_turn_intent_left: rl.Texture = gui_app.texture('icons_mici/turn_intent_left.png', 50, 20)
    self._txt_turn_intent_right: rl.Texture = gui_app.texture('icons_mici/turn_intent_left.png', 50, 20, flip_x=True)

  def _render(self, _):
    if self._turn_intent_alpha_filter.x > 1e-2:
      turn_intent_texture = self._txt_turn_intent_right if self._turn_intent_direction == 1 else self._txt_turn_intent_left
      src_rect = rl.Rectangle(0, 0, turn_intent_texture.width, turn_intent_texture.height)
      dest_rect = rl.Rectangle(self._rect.x + self._rect.width / 2, self._rect.y + self._rect.height / 2,
                               turn_intent_texture.width, turn_intent_texture.height)

      origin = (turn_intent_texture.width / 2, self._rect.height / 2)
      color = rl.Color(255, 255, 255, int(255 * self._turn_intent_alpha_filter.x))
      rl.draw_texture_pro(turn_intent_texture, src_rect, dest_rect, origin, self._turn_intent_rotation_filter.x, color)

  def _update_state(self) -> None:
    sm = ui_state.sm

    left = any(e.name == EventName.preLaneChangeLeft for e in sm['onroadEvents'])
    right = any(e.name == EventName.preLaneChangeRight for e in sm['onroadEvents'])
    if left or right:
      # pre lane change
      if not self._pre:
        self._turn_intent_rotation_filter.x = self.FADE_IN_ANGLE if left else -self.FADE_IN_ANGLE

      self._pre = True
      self._turn_intent_direction = -1 if left else 1
      self._turn_intent_alpha_filter.update(1)
      self._turn_intent_rotation_filter.update(0)
    elif any(e.name == EventName.laneChange for e in sm['onroadEvents']):
      # fade out and rotate away
      self._pre = False
      self._turn_intent_alpha_filter.update(0)

      if self._turn_intent_direction == 0:
        # unknown. missed pre frame?
        self._turn_intent_rotation_filter.update(0)
      else:
        self._turn_intent_rotation_filter.update(self._turn_intent_direction * self.FADE_IN_ANGLE)
    else:
      # didn't complete lane change, just hide
      self._pre = False
      self._turn_intent_direction = 0
      self._turn_intent_alpha_filter.update(0)
      self._turn_intent_rotation_filter.update(0)


class NavManeuver(Widget):
  """Minimal turn-by-turn cue on the RIGHT edge: a thin vertical bar that starts full
  and drains UP to zero as you approach, with a small vector action-icon above it.
  Shows only inside a ~10s time-to-maneuver window (so lead scales with speed).
  Throttled: maneuver state recomputed only on a fresh navigationd message; drawn
  only while active. Vector-drawn (no assets)."""
  WINDOW_S = 10.0          # show when time-to-maneuver <= this
  BAR_W = 10
  BAR_H = 180
  ICON = 30

  # mapbox (type, modifier) -> our icon kind
  @staticmethod
  def _kind(mtype, mod):
    t = (mtype or "").lower(); m = (mod or "").lower()
    if "arrive" in t: return "flag"
    if "depart" in t: return "dot"
    if "roundabout" in t or "rotary" in t: return "round"
    if "merge" in t: return "merge_r" if "right" in m else "merge_l"
    if "ramp" in t or "exit" in t: return "exit_r" if "right" in m else "exit_l"
    if "fork" in t or "keep" in t: return "fork_r" if "right" in m else "fork_l"
    if "uturn" in m: return "uturn"
    if "sharp left" in m: return "sharp_l"
    if "sharp right" in m: return "sharp_r"
    if "slight left" in m: return "slight_l"
    if "slight right" in m: return "slight_r"
    if "left" in m: return "left"
    if "right" in m: return "right"
    return "straight"

  _ANG = {"straight":0,"left":-90,"right":90,"slight_l":-45,"slight_r":45,
          "sharp_l":-135,"sharp_r":135,"uturn":170,
          "merge_l":-45,"merge_r":45,"fork_l":-30,"fork_r":30,"exit_l":-70,"exit_r":70}

  HB_PERIOD = 5.0          # telemetry emit cadence (s)

  def __init__(self):
    super().__init__()
    self._alpha = FirstOrderFilter(0.0, 0.15, 1 / gui_app.target_fps)
    self._kindcur = "straight"
    self._dist = 0.0
    self._ttm = 1e9
    self._frame = -1
    # --- telemetry counters (window-accumulated, emitted every HB_PERIOD) ---
    self._hb = _navlog.Heartbeat("nav_ui") if _navlog else None   # whole UI-proc cpu/hz/rss
    self._t_upd = 0.0        # accumulated _update_state seconds this window
    self._t_drw = 0.0        # accumulated _render seconds this window (active frames only)
    self._n_frame = 0        # frames seen this window
    self._n_drawn = 0        # frames actually drawn (bar visible) this window
    self._win_t0 = time.time()

  def _emit_telemetry(self):
    # per-widget cost: avg update micros over all frames, avg draw micros over drawn frames,
    # and what fraction of frames actually drew (the bar is usually idle).
    if _navlog is None:
      return
    now = time.time(); dt = now - self._win_t0
    if dt < self.HB_PERIOD:
      return
    nf = max(self._n_frame, 1)
    _navlog.log("nav_hud", ev="hb",
                fps=round(self._n_frame / dt, 1),
                upd_us=round(1e6 * self._t_upd / nf, 1),
                draw_us=round(1e6 * self._t_drw / max(self._n_drawn, 1), 1),
                active_pct=round(100.0 * self._n_drawn / nf),
                frames=self._n_frame, drawn=self._n_drawn)
    self._t_upd = self._t_drw = 0.0
    self._n_frame = self._n_drawn = 0
    self._win_t0 = now

  def _update_state(self):
    _t0 = time.perf_counter()
    sm = ui_state.sm
    # recompute the maneuver only when a fresh navigationd message arrives (cheap rest-of-time)
    if sm.updated.get("navigationd"):
      try:
        nav = sm["navigationd"]; mans = nav.allManeuvers
        if nav.valid and len(mans):
          m0 = mans[0]; self._kindcur = self._kind(m0.type, m0.modifier); self._dist = float(m0.distance)
        else:
          self._dist = 0.0
      except (KeyError, AttributeError):
        self._dist = 0.0
    # time-to-maneuver gate (speed-aware)
    v = max(float(ui_state.sm["carState"].vEgo), 0.1)
    self._ttm = self._dist / v if self._dist > 0 else 1e9
    self._alpha.update(1.0 if self._ttm <= self.WINDOW_S else 0.0)
    # telemetry: per-frame cost + UI-proc heartbeat (both gated by navlog mode)
    self._t_upd += time.perf_counter() - _t0
    self._n_frame += 1
    if self._hb is not None:
      self._hb.tick()
    self._emit_telemetry()

  def _render(self, rect):
    a = self._alpha.x
    if a < 1e-2:
      return
    _t0 = time.perf_counter()
    frac = max(0.0, min(1.0, self._ttm / self.WINDOW_S))   # 1 far -> 0 at maneuver
    urgent = frac < 0.25
    col = rl.Color(255, 170, 40, int(255 * a)) if urgent else rl.Color(255, 255, 255, int(255 * a))
    dim = rl.Color(255, 255, 255, int(60 * a))
    bx = rect.x + rect.width - 22 - self.BAR_W
    by = rect.y + rect.height * 0.42
    # track + fill (anchored at top, shrinks upward as you approach)
    rl.draw_rectangle(int(bx), int(by), self.BAR_W, self.BAR_H, dim)
    rl.draw_rectangle(int(bx), int(by), self.BAR_W, int(self.BAR_H * frac), col)
    # icon above the bar
    self._icon(bx + self.BAR_W / 2, by - 34, self.ICON / 2, col)
    # telemetry: accumulate this active frame's draw cost
    self._t_drw += time.perf_counter() - _t0
    self._n_drawn += 1

  def _icon(self, cx, cy, s, col):
    import math
    k = self._kindcur; th = 4
    if k == "flag":
      rl.draw_line_ex(rl.Vector2(cx - s*0.6, cy - s), rl.Vector2(cx - s*0.6, cy + s), th, col)
      rl.draw_rectangle(int(cx - s*0.6), int(cy - s), int(s*1.2), int(s*0.8), col)
      return
    if k == "dot":
      rl.draw_circle(int(cx), int(cy), s*0.5, col); return
    if k == "round":
      rl.draw_circle_lines(int(cx), int(cy), s*0.7, col)
      rl.draw_line_ex(rl.Vector2(cx, cy), rl.Vector2(cx + s, cy - s), th, col); return
    ang = math.radians(self._ANG.get(k, 0))
    bx2, by2 = cx, cy + s
    tx, ty = cx + s*math.sin(ang), cy - s*math.cos(ang)
    rl.draw_line_ex(rl.Vector2(bx2, by2), rl.Vector2(cx, cy), th, col)   # stem
    rl.draw_line_ex(rl.Vector2(cx, cy), rl.Vector2(tx, ty), th, col)     # turn
    for da in (math.radians(150), math.radians(-150)):                   # arrowhead
      hx = tx + s*0.5*math.sin(ang + da); hy = ty - s*0.5*math.cos(ang + da)
      rl.draw_line_ex(rl.Vector2(tx, ty), rl.Vector2(hx, hy), th, col)


class HudRenderer(Widget):
  def __init__(self):
    super().__init__()
    """Initialize the HUD renderer."""
    self.is_cruise_set: bool = False
    self.is_cruise_available: bool = True
    self.set_speed: float = SET_SPEED_NA
    self._set_speed_changed_time: float = 0
    self.speed: float = 0.0
    self.v_ego_cluster_seen: bool = False
    self._engaged: bool = False

    self._can_draw_top_icons = True
    self._show_wheel_critical = False

    self._font_bold: rl.Font = gui_app.font(FontWeight.BOLD)
    self._font_medium: rl.Font = gui_app.font(FontWeight.MEDIUM)
    self._font_semi_bold: rl.Font = gui_app.font(FontWeight.SEMI_BOLD)
    self._font_display: rl.Font = gui_app.font(FontWeight.DISPLAY)

    self._turn_intent = TurnIntent()
    self._torque_bar = TorqueBar()
    self._nav_maneuver = NavManeuver()

    self._txt_wheel: rl.Texture = gui_app.texture('icons_mici/wheel.png', 50, 50)
    self._txt_wheel_critical: rl.Texture = gui_app.texture('icons_mici/wheel_critical.png', 50, 50)
    self._txt_exclamation_point: rl.Texture = gui_app.texture('icons_mici/exclamation_point.png', 9, 44)

    self._wheel_alpha_filter = FirstOrderFilter(0, 0.05, 1 / gui_app.target_fps)
    self._wheel_y_filter = FirstOrderFilter(0, 0.1, 1 / gui_app.target_fps)

    self._set_speed_alpha_filter = FirstOrderFilter(0.0, 0.1, 1 / gui_app.target_fps)

  def set_wheel_critical_icon(self, critical: bool):
    """Set the wheel icon to critical or normal state."""
    self._show_wheel_critical = critical

  def set_can_draw_top_icons(self, can_draw_top_icons: bool):
    """Set whether to draw the top part of the HUD."""
    self._can_draw_top_icons = can_draw_top_icons

  def drawing_top_icons(self) -> bool:
    # whether we're drawing any top icons currently
    return bool(self._set_speed_alpha_filter.x > 1e-2)

  def _update_state(self) -> None:
    """Update HUD state based on car state and controls state."""
    sm = ui_state.sm
    if sm.recv_frame["carState"] < ui_state.started_frame:
      self.is_cruise_set = False
      self.set_speed = SET_SPEED_NA
      self.speed = 0.0
      return

    controls_state = sm['controlsState']
    car_state = sm['carState']

    v_cruise_cluster = car_state.vCruiseCluster
    set_speed = (
      controls_state.deprecated.vCruise if v_cruise_cluster == 0.0 else v_cruise_cluster
    )
    engaged = sm['selfdriveState'].enabled
    if (set_speed != self.set_speed and engaged) or (engaged and not self._engaged):
      self._set_speed_changed_time = rl.get_time()
    self._engaged = engaged
    self.set_speed = set_speed
    self.is_cruise_set = 0 < self.set_speed < SET_SPEED_NA
    self.is_cruise_available = self.set_speed != -1

    v_ego_cluster = car_state.vEgoCluster
    self.v_ego_cluster_seen = self.v_ego_cluster_seen or v_ego_cluster != 0.0
    v_ego = v_ego_cluster if self.v_ego_cluster_seen else car_state.vEgo
    speed_conversion = CV.MS_TO_KPH if ui_state.is_metric else CV.MS_TO_MPH
    self.speed = max(0.0, v_ego * speed_conversion)

  def _render(self, rect: rl.Rectangle) -> None:
    """Render HUD elements to the screen."""

    self._torque_bar.render(rect)

    if self.is_cruise_set:
      self._draw_set_speed(rect)

    self._draw_steering_wheel(rect)

    self._nav_maneuver.render(rect)

  def _draw_steering_wheel(self, rect: rl.Rectangle) -> None:
    wheel_txt = self._txt_wheel_critical if self._show_wheel_critical else self._txt_wheel

    bsm_detected = self._has_blind_spot_detected() if gui_app.sunnypilot_ui() else False

    if self._show_wheel_critical:
      self._wheel_alpha_filter.update(255)
      self._wheel_y_filter.update(0)
    else:
      if ui_state.status == UIStatus.DISENGAGED or bsm_detected:
        self._wheel_alpha_filter.update(0)
        self._wheel_y_filter.update(wheel_txt.height / 2)
      else:
        self._wheel_alpha_filter.update(255 * 0.9)
        self._wheel_y_filter.update(0)

    # pos
    pos_x = int(rect.x + 21 + wheel_txt.width / 2)
    pos_y = int(rect.y + rect.height - 14 - wheel_txt.height / 2 + self._wheel_y_filter.x)
    rotation = -ui_state.sm['carState'].steeringAngleDeg

    turn_intent_margin = 25
    self._turn_intent.render(rl.Rectangle(
      pos_x - wheel_txt.width / 2 - turn_intent_margin,
      pos_y - wheel_txt.height / 2 - turn_intent_margin,
      wheel_txt.width + turn_intent_margin * 2,
      wheel_txt.height + turn_intent_margin * 2,
    ))

    src_rect = rl.Rectangle(0, 0, wheel_txt.width, wheel_txt.height)
    dest_rect = rl.Rectangle(pos_x, pos_y, wheel_txt.width, wheel_txt.height)
    origin = (wheel_txt.width / 2, wheel_txt.height / 2)

    # color and draw
    color = rl.Color(255, 255, 255, int(self._wheel_alpha_filter.x))
    rl.draw_texture_pro(wheel_txt, src_rect, dest_rect, origin, rotation, color)

    if self._show_wheel_critical:
      # Draw exclamation point icon
      EXCLAMATION_POINT_SPACING = 10
      exclamation_pos_x = pos_x - self._txt_exclamation_point.width / 2 + wheel_txt.width / 2 + EXCLAMATION_POINT_SPACING
      exclamation_pos_y = pos_y - self._txt_exclamation_point.height / 2
      rl.draw_texture_ex(self._txt_exclamation_point, rl.Vector2(exclamation_pos_x, exclamation_pos_y), 0.0, 1.0, rl.WHITE)

  def _draw_set_speed(self, rect: rl.Rectangle) -> None:
    """Draw the MAX speed indicator box."""
    alpha = self._set_speed_alpha_filter.update(0 < rl.get_time() - self._set_speed_changed_time < SET_SPEED_PERSISTENCE and
                                                self._can_draw_top_icons and self._engaged)
    if alpha < 1e-2:
      return

    x = rect.x
    y = rect.y

    # draw drop shadow
    circle_radius = 162 // 2
    rl.draw_circle_gradient(int(x + circle_radius), int(y + circle_radius), circle_radius,
                            rl.Color(0, 0, 0, int(255 / 2 * alpha)), rl.BLANK)

    set_speed_color = rl.Color(255, 255, 255, int(255 * 0.9 * alpha))
    max_color = rl.Color(255, 255, 255, int(255 * 0.9 * alpha))

    set_speed = self.set_speed
    if self.is_cruise_set and not ui_state.is_metric:
      set_speed *= KM_TO_MILE

    set_speed_text = CRUISE_DISABLED_CHAR if not self.is_cruise_set else str(round(set_speed))
    rl.draw_text_ex(
      self._font_display,
      set_speed_text,
      rl.Vector2(x + 13 + 4, y + 3 - 8 - 3 + 4),
      FONT_SIZES.set_speed,
      0,
      set_speed_color,
    )

    max_text = tr("MAX")
    rl.draw_text_ex(
      self._font_semi_bold,
      max_text,
      rl.Vector2(x + 25, y + FONT_SIZES.set_speed - 7 + 4),
      FONT_SIZES.max_speed,
      0,
      max_color,
    )

  def _draw_current_speed(self, rect: rl.Rectangle) -> None:
    """Draw the current vehicle speed and unit."""
    speed_text = str(round(self.speed))
    speed_text_size = measure_text_cached(self._font_bold, speed_text, FONT_SIZES.current_speed)
    speed_pos = rl.Vector2(rect.x + rect.width / 2 - speed_text_size.x / 2, 180 - speed_text_size.y / 2)
    rl.draw_text_ex(self._font_bold, speed_text, speed_pos, FONT_SIZES.current_speed, 0, COLORS.WHITE)

    unit_text = tr("km/h") if ui_state.is_metric else tr("mph")
    unit_text_size = measure_text_cached(self._font_medium, unit_text, FONT_SIZES.speed_unit)
    unit_pos = rl.Vector2(rect.x + rect.width / 2 - unit_text_size.x / 2, 290 - unit_text_size.y / 2)
    rl.draw_text_ex(self._font_medium, unit_text, unit_pos, FONT_SIZES.speed_unit, 0, COLORS.WHITE_TRANSLUCENT)
