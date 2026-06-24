# mici-nav — Longitudinal Tuning Test Checklist

Run after restarting the comma (so it has pulled the latest `mici-nav`).
Test somewhere quiet first. Hands ready, foot near the brake — these change accel/braking behavior.

## 0. Pre-flight (in the car, parked)
- [ ] Device restarted and online; no update pending (Settings → Software).
- [ ] On branch `mici-nav`, commit matches GitHub HEAD.
- [ ] Speed Limit settings show: **Mode = Assist**, **Offset = Fixed +5**. (Migration applies once on boot.)

## 1. Speed-limit follow (+5)  — params migration
- [ ] On a road with a known limit, set speed tracks **limit + 5** automatically.
- [ ] Changing the offset in the UI sticks (migration shouldn't override a manual change).
- [ ] Does NOT force itself back on if you switch Mode to Warning/Off.

## 2. Aggressive launch from a stop  — A_CRUISE_MAX_VALS [2.0,1.9,1.5,1.0]
- [ ] From a full stop, takeoff is strong — no need to feather the gas.
- [ ] Pulls confidently through low/mid speed and **reaches the set speed** (no capping out low).
- [ ] Not jerky/uncomfortable. If too eager: dial standstill 2.0 → 1.8.

## 3. Coast earlier when the limit drops  — LIMIT_ADAPT_ACC -0.5
- [ ] Entering a lower-limit zone, it starts easing **earlier** and glides down.
- [ ] No late, hard brake to shed the speed.
- [ ] Still brakes normally for hazards (this only changed *when* easing starts).

## 4. Smoother stops for lights / stop signs  — COMFORT_BRAKE 1.8 + DEC SLOW_DOWN
- [ ] Approaching a red light/stop sign, it **stops adding throttle promptly** once the stop is visible.
- [ ] Begins slowing **earlier**, bleeds speed gradually — no late brake-slam.
- [ ] Comes to a smooth, complete stop at a reasonable distance behind the line/lead.
- [ ] Lead-car following (stop-and-go traffic) still feels normal.

## Safety / sanity (all phases)
- [ ] No unexpected hard braking on open road.
- [ ] No surging or accel oscillation.
- [ ] Disengage (brake/gas) instantly overrides as expected.
- [ ] Note anything off → report back for a tuning pass.

## Knobs to dial if needed (file : constant)
- Launch too eager → `selfdrive/controls/lib/longitudinal_planner.py` : `A_CRUISE_MAX_VALS[0]` 2.0→1.8
- Stops still late/hard → `selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` : `COMFORT_BRAKE` 1.8→1.5
- Coast-down too soft/early → `sunnypilot/selfdrive/controls/lib/speed_limit/__init__.py` : `LIMIT_ADAPT_ACC` -0.5→-0.7
- Commit-to-stop still late → `sunnypilot/selfdrive/controls/lib/dec/constants.py` : `SLOW_DOWN_PROB` 0.2→0.15
