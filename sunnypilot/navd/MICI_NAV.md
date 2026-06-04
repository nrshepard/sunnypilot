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
