# Route-Steering Test Matrix (mici-nav)

Atomic feature flags + scalar tunables for isolating which route-steering combo works.
**Single source of truth:** `sunnypilot/navd/nav_presets.json`. Toggle from Helsinki with `ctk`.

All flags default **OFF** → stock behavior. Booleans = files under `/data/navd/flags/<name>`;
scalars = files under `/data/navd/<KEY>`. EXPERIMENTAL / supervised L2 — hands-on, eyes-on.

## Tooling (Helsinki)
```
ctk presets              # list the named combos
ctk preset <name>        # apply a combo atomically (sets managed flags on/off + tunables)
ctk navstate             # dump current device flags + tunables
ctk tune <KEY> <value>   # set one scalar (e.g. ctk tune NavSteerTurnSpeed 18)
ctk flags on|off <name>  # toggle a single flag by hand
```
**`navsteer` is read at DesireHelper init → REBOOT (ignition cycle) after changing it.**
All other flags + tunables are read live (~3 s) — no reboot needed.

## Flags (atomic)
| Flag | Layer | Effect | Reboot? |
|------|-------|--------|---------|
| `navsteer` | lateral | MASTER: instantiate nav→desire injection | **yes** |
| `navsteer_turn` | lateral | hard turnLeft/turnRight at intersections (< `NavSteerTurnSpeed`) | live |
| `navsteer_keep` | lateral | keepLeft/keepRight slight-turn / fork nudges | live |
| `navsteer_handsoff` | lateral | drop the steeringPressed/torque gate on keep nudges | live |
| `navslow` | longitudinal | decelerate toward the maneuver (only-slower v_cruise clamp) | live |
| `navhud_persist` | UI | keep the maneuver cue on continuously (vs final ~10 s window) | live |

## Tunables (scalars)
| Key | Default | Unit | Meaning |
|-----|---------|------|---------|
| `NavSteerTurnSpeed` | 20 | mph | hard-turn desire only below this |
| `NavSlowTargetTurn` | 15 | mph | target speed reached for a hard turn |
| `NavSlowTargetSlight` | 25 | mph | target for slight / fork / ramp |
| `NavSlowLookahead` | 200 | m | max maneuver distance considered for slowdown |
| `NavSlowDecel` | 1.5 | m/s² | comfortable decel cap for slow-point onset |

## Presets — each isolates ONE axis from its neighbor
| Preset | Adds vs. previous | What it proves |
|--------|-------------------|----------------|
| `baseline` | — (all off) | **Control.** Confirm nothing regressed; HUD windowed. |
| `hud` | `navhud_persist` | HUD persistence works + doesn't overdraw/lag the UI. |
| `slow` | `navslow` | Longitudinal decel into turns **alone** (no steering): does it slow smoothly, only-slower, no false braking? |
| `turn` | `navsteer`+`navsteer_turn` | Hard intersection turn-steering **alone** (no slow): does the model take the turn when slowed manually? |
| `keep` | `navsteer`+`navsteer_keep` | Slight/fork nudge, hands-on (driver torque required). |
| `handsoff` | `navsteer_handsoff` | Same slight nudge with the hands-on gate removed. |
| `turn+slow` | `navsteer_turn`+`navslow` | **The real intersection combo:** decel + turn together. |
| `full` | everything | Kitchen sink — only after the atoms pass. |

### Suggested order
`baseline → hud → slow → turn → turn+slow → keep → handsoff → full`.
Run each as its own short drive; change exactly one preset between runs so a regression
points at exactly one flag.

### Per-run checklist
- `ctk navstate` before driving (confirm the intended combo is live).
- Watch: engagement holds (no commIssue/take-control), decel comfort, turn execution, HUD.
- After: `ctk commissue` + `ctk schedstat` to confirm the safety loop wasn't re-starved.

## Safety notes
- `navslow` is **only-slower** (`min(v_cruise, target)`) and decel-bounded; it can never speed up
  and won't override a closer lead/radar constraint.
- Turn desires are **low-speed gated**; the model executes the same maneuver class as a
  blinker low-speed turn.
- This is L2: it will get turns wrong. Supervise every run.
