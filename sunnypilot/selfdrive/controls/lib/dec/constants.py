class WMACConstants:
  # Lead detection parameters
  LEAD_WINDOW_SIZE = 6  # Stable detection window
  LEAD_PROB = 0.45  # Balanced threshold for lead detection

  # Slow down detection parameters
  SLOW_DOWN_WINDOW_SIZE = 5  # Responsive but stable
  SLOW_DOWN_PROB = 0.2  # was 0.3; lower => commits to an upcoming stop sooner, so it stops
                        # adding throttle and starts coasting earlier toward red lights / stop signs.

  # Optimized slow down distance curve - smooth and progressive.
  # Bumped ~15% so the car begins reacting to a detected stop from farther out.
  SLOW_DOWN_BP = [0., 10., 20., 30., 40., 50., 55., 60.]
  SLOW_DOWN_DIST = [37., 53., 74., 99., 124., 150., 167., 190.]

  # Slowness detection parameters
  SLOWNESS_WINDOW_SIZE = 10  # Stable slowness detection
  SLOWNESS_PROB = 0.55  # Clear threshold for slowness
  SLOWNESS_CRUISE_OFFSET = 1.025  # Conservative cruise speed offset
