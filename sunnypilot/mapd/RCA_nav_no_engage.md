# RCA: "nav flag on → openpilot won't engage / take-control alarms"

## Symptom
Enabling the nav feature → cascading `commIssue` ("TAKE CONTROL IMMEDIATELY") cycling
through `liveTorqueParameters`, `driverMonitoringState`, `liveParameters`,
`driverAssistance`, `modelDataV2SP`. Car will not engage.

## What it was NOT
- **Not navigationd.** Proven from on-device diag logs (boot 000010): `navigationd`
  was in `not_running` the entire window, yet `commIssue` was still **68%**. The TBT
  daemon is innocent.
- **Not SCHED_FIFO CPU starvation alone.** Demoting the map stack from `SCHED_FIFO`
  prio-5 to niced `SCHED_OTHER` (real bug, kept) cleared the *model/radar/planner*
  flapping but did **not** fix engagement.
- **Not a memory leak.** mapd RSS is only ~140 MB and steady; mem% sat at ~62%.

## Root cause
`mapd` (pfeiferj `openpilot-mapd` v1.12.0) runs **unconditionally** — `mapd_ready()`
returned True whenever the binary existed, independent of any flag. It mmaps the
downloaded OSM DB and map-matches every tick. The default `OsmStateName="All"` resolves
(via `filter_nations_and_states`) to nation **"US"**, so mapd loads the **entire United
States** road graph (multi-GB).

On the device (~31 MB free, 1.5 GB page cache, **no swap**), every map-match query into
the oversized mmap evicts cache pages → `kswapd0` churns at ~30% CPU reclaiming → I/O
stalls hit **every** process → services miss their selfdrived freq/alive deadlines →
`commIssue` → no engage.

Proof: `kswapd0` 30% → **0%** the instant nav/maps go off; `commIssue` tracks
mapd-running, not navigationd.

## Fix
1. **Gate mapd** (`process_config.mapd_ready`): also require `OsmLocal`. No offline-OSM
   enabled ⇒ no consumer ⇒ don't run the matcher. Stops the unconditional whole-US
   thrash; makes maps-off driving clean.
2. **Bound the OSM working set** (`mapd_manager.update_osm_bounds`): keep
   `OSMDownloadBounds` tracking a ~80 km (0.75°) box around the car instead of a whole
   nation. ~166×144 km vs ~4500 km — the resident set stays in page cache and always
   covers the road ahead. Hysteresis (half-radius) avoids per-tick rewrites.

## Needs one on-car validation
Confirm the v1.12.0 Go binary honors `OSMDownloadBounds` to limit the **resident/mmap**
set (not just download). Test: enable maps, drive, watch `kswapd0` (expect ~0%) and the
diag spool `commIssue` rate (expect ~0). If the binary only bounds *download*, fall back
to setting `OsmStateName` to the actual state instead of "All".

## Diagnostics that found it
`can_logger` diag records (flag `/data/navd/flags/log_diag`): `res` (per-core CPU, mem%,
free-disk%, temp), `events` (onroadEvents), `procs` (stalled procs) — drained to pCloud,
analyzed offline. `top` showed `kswapd0` as the tell.
