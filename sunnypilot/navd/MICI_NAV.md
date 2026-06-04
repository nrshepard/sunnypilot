# C4 (mici) turn-by-turn maneuvers — build & destination input

Backport of sunnypilot's navd to the Comma 4 `release-mici` line. **Maneuvers only**
(banner + driving-model lane context). No on-screen moving map by design — the C4
screen is tiny and a map widget is a separate from-scratch raylib build.

## What this branch adds
- `sunnypilot/navd/*` — the navigation daemon (geocode + route + maneuvers via Mapbox).
- `sunnypilot/navd/nav_destination_server.py` — tailnet HTTP setter (Siri Shortcut target).
- `cereal/custom.capnp` — `CustomReserved10` → `struct Navigationd` (same id `0xcb9fd56c7057593a`, the slot sunnypilot reserved for it).
- `cereal/log.capnp` — union slot `@136` → `navigationd :Custom.Navigationd`.
- `cereal/services.py` — `navigationd` @ 3 Hz.
- `system/manager/process_config.py` — launches `navigationd` (onroad) + `navdestd` (always).

## Build-free deploy (Path A — current)
Release slots are PREBUILT (no top-level SConstruct; params_pyx.so has a fixed key
registry that rejects new keys). So navd stores its state as plain files via
`navstore.NavParams` under `/data/navd/` instead of openpilot Params — **no compile
needed**. pycapnp loads the `navigationd` message + `services.py` at runtime.
Deploy = drop the branch into a slot + restart the manager. Set the token with:
  printf '%s' "$MAPBOX_PUBLIC_TOKEN" > /data/navd/MapboxToken
(The params_keys.h additions remain in-branch but are only relevant to a future
source build / Path B; they are inert for the file-store deploy.)

## Params it uses
- `MapboxToken` — public `pk.` token. File: `/data/navd/MapboxToken`. **Required.**
- `MapboxRoute` — destination place-name string. File: `/data/navd/MapboxRoute`.
- `AllowNavigation` — on/off. File: `/data/navd/AllowNavigation`.

## Build on the car (into a SEPARATE slot — daily driver stays pristine)
```sh
# 1. clone fork's mici-nav into a new slot (do NOT touch the live sunnypilot-mici slot)
SLOT=/data/op_slots/sunnypilot-mici-nav
git clone -b mici-nav https://github.com/nrshepard/sunnypilot.git "$SLOT"
cd "$SLOT" && git submodule update --init --recursive

# 2. set the Mapbox public token (pulled from Helsinki secrets, see install helper)
printf '%s' "$MAPBOX_PUBLIC_TOKEN" > /data/params/d/MapboxToken

# 3. regenerate cereal capnp + build
cd "$SLOT" && scons -j$(nproc)        # capnp schema regen happens here; build fails LOUDLY if the schema patch is off (safe — nothing flashes)

# 4. swap to the new slot with the existing opswap manager, then reboot
opswap sunnypilot-mici-nav && reboot
```
Revert anytime: `opswap sunnypilot-mici` (10-second fallback to the known-good slot).

## Destination input — Siri Shortcut (phase 1)
The device runs `navdestd` on **:5005**, reachable over your tailnet.

**iOS Shortcut "Navigate Comma":**
1. Action: *Ask for Input* (Text) → prompt "Where to?"  (or *Dictate Text* for voice)
2. Action: *URL* → `http://<comma-tailscale-ip>:5005/set?dest=[Provided Input]`
   (URL-encode the input; Shortcuts' "URL Encode" action on the text first)
3. Action: *Get Contents of URL* → Method GET
4. (optional) *Show Result* of the response JSON.
Add it to Siri: "Hey Siri, Navigate Comma" → speak the address.

Cancel route: GET `http://<comma-ip>:5005/cancel`.  Status: `/status`.

## Also works
- **Tell the assistant** "drive to <address>" → it writes `MapboxRoute` over SSH (no app needed).

## On-screen signal (in this branch)
`NavManeuver` widget in `selfdrive/ui/mici/onroad/hud_renderer.py`. Redesigned 2026-06-04
(old top-center chevron trashed — it drew every UI frame and was a CPU suspect).

Now: a thin **right-edge vertical bar** (~10px wide) that starts FULL and **drains UP** to
zero as you approach the maneuver, with a small **vector action-icon above** it (no PNG assets).
- Trigger: **time-to-maneuver** gate — `distance / vEgo <= 10s` (speed-aware lead). Bar turns
  orange when <25% remaining.
- Perf discipline: recompute maneuver state **only on a fresh `navigationd` message**
  (`sm.updated`), **draw only when active** (`alpha > 0`), `FirstOrderFilter` fade. This fixes
  the old per-frame draw cost.
- Icon set (vectors, mirrored for side): left/right, slight L/R, sharp L/R, uturn, straight,
  merge L/R, fork/keep L/R, exit/ramp L/R, roundabout, depart (dot), **arrive (finish flag)**.
- Mapbox maneuvers covered (`type`+`modifier`): turn, slight, sharp, uturn, straight/continue,
  merge, fork/keep, on/off-ramp, roundabout/rotary, depart, arrive (~10 glyphs, mirrored).

## Telemetry logging (`navlog`) — modes & flags
`sunnypilot/navd/navlog.py`. Per-process heartbeats (cpu/hz/rss) + events. Mode flag at
`/data/navd/flags/logmode`:
- `off` — no logging.
- `disk` — local disk only (`comma_logs/`).
- `stream` — disk + live stream to Helsinki collector.
- `opportunistic` (**DEFAULT**) — always disk (source of truth); offloads to Helsinki only when
  1-min load < 5 AND network up (parked/idle), incremental + offset-tracked. Never competes with
  driving. Streaming validated end-to-end: `navlog -> collector -> comma_logs/stream.jsonl`.

## Independent feature flags (all decoupled)
`navigationd | navdestd | wifi_eager | can_capture | logmode` — each gated separately under
`/data/navd/flags/`. Notable: **`can_capture` is fully decoupled from nav** — own flag, own
`process_config` entry, and in selfdrived `ignored_processes` so it can never block engagement.
It subscribes to the full CAN firehose (heavy), so it runs only when explicitly flagged for a
logging session. `wifi_eager` self-gates on connectivity (does nothing while online) — left as-is.

## Future (not in this branch)
- On-device favorites/recents tap-list (no-keyboard destination on the tiny screen).
- Audible chime ~200m before a maneuver.
- Read the **factory nav** turn-by-turn guidance off the CAN bus (Hyundai broadcasts maneuver
  arrows/distance to the cluster) — bypasses Mapbox entirely. Needs CAN reverse-engineering on the Palisade.

## FUTURE TODO — ESCC (keep factory AEB + openpilot longitudinal) — NOT week 1
Goal: run openpilot longitudinal AND retain factory AEB simultaneously. Currently impossible on the
Palisade because op-long calls disable_ecu(0x7d0) once at init() — kills the MANDO radar (which IS the
AEB) for the entire power cycle. No dash switch, no live swap; changing long mode = setting + reboot.

The one supported "both" path = ENHANCED_SCC (ESCC):
- Code is already present and NOT Palisade-gated. Auto-enables when CAN msg 0x2AB (ESCC_MSG) appears in
  the bus-0 fingerprint (interface.py:176 -> sets HyundaiFlagsSP.ENHANCED_SCC, safetyParam ESCC).
- When enabled: init() SKIPS disable_ecu (interface.py:235 guard) -> radar stays ALIVE -> factory AEB
  preserved. openpilot does long by forwarding the radars

## FUTURE TODO — ESCC (keep factory AEB + openpilot longitudinal) — NOT week 1
Goal: run openpilot longitudinal AND retain factory AEB simultaneously. Currently impossible on the
Palisade because op-long calls disable_ecu(0x7d0) once at init() — kills the MANDO radar (which IS the
AEB) for the entire power cycle. No dash switch, no live swap; changing long mode = setting + reboot.

The one supported "both" path = ENHANCED_SCC (ESCC):
- Code is already present and NOT Palisade-gated. Auto-enables when CAN msg 0x2AB (ESCC_MSG) appears in
  the bus-0 fingerprint (interface.py:176 -> sets HyundaiFlagsSP.ENHANCED_SCC, safetyParam ESCC).
- When enabled: init() SKIPS disable_ecu (interface.py:235 guard) -> radar stays ALIVE -> factory AEB
  preserved. openpilot does long by forwarding the radar real AEB/FCA decel into SCC12
  (escc.py update_scc12: AEB_Status=2 = enabled, decel cmds + AEB_CmdAct sourced from the radar).
- CATCH (why it is scary / not week 1): stock MANDO radar does NOT broadcast 0x2AB. That message only
  exists after REFLASHING the radar with community ESCC firmware (makes radar emit 0x2AB AEB cmds while
  staying quiet on the SCC msgs that would conflict with op). i.e. flashing a SAFETY ECU. Reversible but
  real risk.
- OPEN QUESTIONS to research before attempting: (1) does a known-good ESCC firmware image exist for the
  Palisade radar part number? (2) flashing procedure + tooling + brick risk + reversibility. (3) panda
  safety param ESCC support confirmed for this platform.
- Files: opendbc/sunnypilot/car/hyundai/escc.py, opendbc/car/hyundai/interface.py:175-180,235,
  hyundaican.py:167-169,214-215,251-252, carcontroller.py:111,193.


## SESSION UPDATE #3 (2026-06-04) — live telemetry + UI CPU verdict + version stamping

### THE BUG THAT WASTED THE FIRST BOOT: device was 6 commits stale
- Device /data/openpilot was on mici-nav @ 260a49ea69 — BEFORE navlog.py even existed.
  Flipping logmode/nav flags did nothing because the code that emits had not been pulled.
- Fix: git merge --ff-only navfork/mici-nav -> 89caf59 (clean tree, prebuilt device, all-Python
  delta so no compile). Then reboot. Stream immediately flowed.
- ROOT-CAUSE PREVENTION (shipped this session): navlog now emits a `session` hello record
  (repo/branch/commit/upstream/dirty) at startup + every 30s. A stale checkout is now obvious
  in the stream instead of silently emitting nothing. See navlog._git_info / _session_stamper.

### CPU VERDICT (from live capture, device parked, ~56 fps)
  navigationd : cpu avg 2.0% max 6.5%  @ 1.0 Hz, rss 89 MB   -> trivial (1 Hz change confirmed)
  nav_hud     : upd 16 us/frame, draw 0 us, active_pct 0     -> ~0.09% of a core; NOT the hog
  nav_ui      : cpu avg 58.7% max 63%  @ ~56 fps, rss 222 MB -> the cost, but it is the BASE
                mici UI render loop, not nav.
  => The nav port is EXONERATED. navigationd is ~2%, the nav HUD widget is ~0.09%. The ~59%
     is the stock mici UI rendering a full GPU frame every tick.

### WHY the UI is ~59% and the real lever
- system/ui/lib/application.py: _DEFAULT_FPS = 60 (non-tizi). render() does begin_drawing ->
  clear -> render top widget(s) -> end_drawing EVERY frame at target_fps. Onroad the top widget
  is augmented_road_view (camera texture + model overlay) — a full redraw at 60 fps, no
  dirty-rect / render-on-change skip. That is the ~59%, and it is GPU-composite + model draw,
  not navigation.
- LEVERS (not applied — need on-device A/B, do not change blind):
  (a) target_fps: env FPS=<n> or gui_app.init_window(fps=). 60->30 ~halves UI CPU but affects
      camera smoothness globally. Could gate lower fps to the nav/offroad screens only.
  (b) render-on-change for static widgets (big refactor of stock UI; risky).
- EXACT NEXT-SESSION TOOL: the UI has a built-in render profiler. Launch UI with
  PROFILE_RENDER=600 (10 s @ 60 fps); after N frames it dumps pstats top-100 by cumtime
  (PROFILE_STATS, default 100) -> pinpoints the precise hot functions inside the 59%.
  Refs: application.py:42 PROFILE_RENDER, :586 enable, :673/:826 dump.

### NEW TELEMETRY shipped this session (navlog.py)
- src=session : ev=hello, proc, repo, branch, commit, upstream, dirty   (startup + 30 s)
- src=system  : ev=hb, proc, cpu_pct (WHOLE DEVICE via /proc/stat delta), load1/load5,
                mem_total_mb/mem_used_mb/mem_pct, disk_used_gb/disk_pct (/data),
                temp_max_c (thermal zones). Own thread (navlog_sysmon) so it streams even if
                the nav loop stalls. Started once from navigationd.main via
                navlog.start_system_monitor(). Gated by logmode; verified e2e to collector.


## SESSION UPDATE #5 (2026-06-04) — UI 59% CPU: SMOKING GUN via in-car cProfile

Device updated to b903f78b8a (session+system telemetry live, version stamp reads OK not STALE).
System stats confirmed flowing: cpu_pct (whole device), load, mem, /data disk, temp_max_c ~75C.

### THE QUESTION: is the UI's 59% CPU real compute or just waiting?
Three methods, escalating confidence:

1) PER-THREAD CPU (/proc/PID/task/*/stat, 1s delta):
   main thread = +85 cs/s (85% of a core); every other thread <=8 cs/s.
   => the ENTIRE cost is the single render thread. Nothing else matters.

2) NON-INTRUSIVE /proc STATE+SYSCALL SAMPLING (100 samples @10ms, zero process stop):
   state: 59% R (running on CPU) / 41% S (sleeping)  -> matches navlog's 59% exactly
   syscall: 66% running in USERSPACE (no syscall) / 31% ioctl#29 (drmWaitVBlank) / 3% clock_nanosleep
   => the 59% is GENUINE userspace render compute, NOT blocked-wait. The vblank/sleep
      waits are the free 41% S. Verdict: real CPU, full redraw every frame.
   (gdb stack sampling is WALL-biased here -- attach latency lands you in the EndDrawing
    vblank wait; it under-counts the active draw. Use /proc state sampling instead, or cProfile.)

3) cProfile via PROFILE_RENDER (definitive, self-time = real CPU). 400 frames:
   ncalls  tottime  cumtime  function
   400     1.069    1.764    raylib EndDrawing            <- #1: GPU submit + 60fps busy-wait/swap
   1757    0.372    0.529    model_renderer:405 _map_line_to_polygon  <- lane/path->polygon tessellation (numpy)
   1830    0.150    0.946    application:174 _handle_mouse_event       <- input poll EVERY frame (high while parked!)
   7350    0.131    6.317    widgets/__init__:106 render               <- widget-tree dispatch (recursive)
   252     0.060    0.433    cameraview:226 _render
   253     0.037    2.814    augmented_road_view:191 _render           <- camera + model overlay chain
   1829    0.035   10.204    realtime:72 keep_time                     <- FRAME PACER (sleep) = most WALL time, ~0 CPU
   Run: 400 frames in 13081 ms, avg 32.7 ms (30.6 FPS, profiler-slowed).

### VERDICT
- keep_time cumtime 10.2s of 13s => most WALL time is the pacer sleeping (free). Real CPU is
  concentrated in EndDrawing + the per-frame draw tree.
- #1 CPU consumer = raylib EndDrawing (1.07s self): GPU draw submission + raylib's WaitTime
  busy-wait tail + SwapScreenBuffer->drmWaitVBlank. "Full GPU frame every tick @60fps" confirmed.
- Biggest pure-app cost = model_renderer._map_line_to_polygon (path/lane tessellation). NOTE: it
  is already gated to model rate (_update_model ran 251x / 400 frames ~= 20Hz, NOT every frame),
  so it is genuinely-costly numpy, NOT a redundant-recompute bug. ~7 calls per model packet
  (4 lane lines + 2 road edges + path).
- SURPRISE: _handle_mouse_event = 0.95s cumtime over the run (1830 calls, ~4.5/frame) polling
  input every frame while PARKED with no touch. Worth throttling -- cheap win.
- Nav port stays EXONERATED: navigationd ~2% @1Hz, nav_hud widget ~0.09% (0 draw_us). The cost
  is 100% the base mici UI render, not navigation.

### LEVERS (ranked)
1. target_fps 60 -> 30 (env FPS=30, supported at application.py:27). Halves EndDrawing/sec AND
   every per-frame draw. The profiled run AT 30fps already showed ~30% CPU vs 59% @60. Highest ROI.
   Could gate low fps to offroad/nav screens only to keep camera smooth onroad.
2. Throttle _handle_mouse_event when no touch session active (~7% cumtime back).
3. _map_line_to_polygon: already model-rate-gated; only worth micro-opt if pushing further.

### REUSABLE TECHNIQUE: profile the comma UI WITHOUT tearing down the stack
The managed `ui` has restart_if_crash=True, and setproctitle CLOBBERS /proc/PID/environ (reads as
spaces -- you CANNOT recover env from the running ui). To run a faithful standalone profile while
camerad/modeld stay live (so the camera composite is realistic):
  SLOT=/data/op_slots/sunnypilot-mici-nav ; PY=/usr/local/venv/bin/python   # capnp lives in this venv
  MPIDS=$(pgrep -f manager.py); UIPID=$(pgrep -f selfdrive.ui.ui|head -1)
  trap "kill -CONT $MPIDS" EXIT          # ALWAYS resume manager, even on failure
  kill -STOP $MPIDS                      # pause supervision; children keep running
  kill -9 $UIPID                         # SIGTERM is ignored; must SIGKILL, then wait for display release
  cd $SLOT && . launch_env.sh && PYTHONPATH=$SLOT \
    PROFILE_RENDER=400 PROFILE_STATS=45 $PY -c "import importlib; importlib.import_module('selfdrive.ui.ui').main()"
  # profiler dumps pstats to stdout then sys.exit(0); manager respawns ui on CONT.
Gotchas: use the venv python (system python3 lacks capnp); replicate launcher exactly
(importlib.import_module('selfdrive.ui.ui').main()); PROFILE_RENDER=N frames, PROFILE_STATS=top-N.
Refs: application.py:42/586/673/826 (profiler), process.py:20 (launcher).
