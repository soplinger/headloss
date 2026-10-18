# headloss

**Offline pipe head-loss calculator: plain-text pipe paths in, friction and minor losses, pump duty points, pipe sizing and CI limit gates out.**

`headloss` reads one readable text file that describes pipe runs, pumps and water temperature. It works out velocity, Reynolds number, friction and minor losses for every pipe, the total dynamic head, and where a pump curve meets the system. Everything runs locally with the Python 3 standard library: no servers, no accounts, no network calls.

## Features

- **Readable input format.** `path`, `pipe` and `pump` lines with `key=value` options and units written next to the numbers (`35m`, `50mm`, `2.5L/s`, `1.5m/s`, `68F`).
- **Darcy–Weisbach losses** with the Colebrook–White friction factor, solved by Newton iteration from a Swamee–Jain start.
- **Laminar and transition handling.** `64/Re` below Re 2000, a smooth blend through 2000–4000, Colebrook above that.
- **Hazen–Williams losses** per pipe (`method=hw c=130`) for water mains.
- **Minor losses** from a built-in fittings library (`elbow90*2+gate_valve+entrance`) or a plain K number.
- **Roughness by material or value.** `steel`, `galvanized`, `castiron`, `copper`, `pvc`, `pe`, `concrete`, or any `rough=0.2mm`.
- **Water properties by temperature**, interpolated for density and dynamic viscosity; set `temp 15C` or `temp 60F`.
- **Per-pipe and per-path results**: velocity, Re, friction factor, friction and minor head, loss in kPa, static lift and total dynamic head (TDH).
- **Pump duty point.** A piecewise-linear pump curve is solved against the path's system curve by bisection. The report says whether the pump meets the design flow.
- **Flow sweeps.** Tabulate velocity, losses, TDH and pump head across a range of flows.
- **Pipe sizing.** For each pipe, the smallest standard bore that keeps velocity under a limit.
- **CI gate.** `check` exits `1` on velocity, loss-per-100 m or pump-shortfall violations, printing `file:line` for each one; exits `2` on input errors.
- **Line-numbered errors.** Every problem in the file is reported with its line number, not just the first.
- **JSON output** (`report --json`) for dashboards or other tools.
- **Quick one-pipe calculator** (`calc`) with the same engine and no file needed.
- **Water property lookup** (`props`) showing density, dynamic and kinematic viscosity.

## Quick start

Run from the project directory. The example file models a well pump that lifts water 12 m into a tank, plus a garden line:

```text
# examples/pump-house.txt
temp 15C

path well-to-tank flow=2.5L/s
pipe name=suction len=6m d=50mm rough=pvc dz=0m k=entrance+check_valve+elbow90
pipe name=rise   len=38m d=40mm rough=galvanized dz=12m k=gate_valve+elbow90*2
pump curve=0L/s:38m,1L/s:36m,2L/s:32m,3L/s:26m,4L/s:16m

path garden flow=1.2L/s
pipe name=main  len=85m d=25mm rough=pe dz=0.5m k=tee_branch
pipe name=drip  len=30m d=16mm rough=pe method=hw c=140 vmax=1.5m/s
```

```bash
python3 headloss.py report examples/pump-house.txt
```

The report lists each path's pipes with velocity, Reynolds number, friction factor and losses, then the TDH and the pump's duty point.

A quick single-pipe check, without a file:

```bash
python3 headloss.py calc --len 35m --d 50mm --q 2L/s --dz 3m --k elbow90*2
```

## How to run

Requires Python 3.9 or newer. There are no third-party dependencies.

| Command | What it does |
| --- | --- |
| `python3 headloss.py report FILE [--json]` | Losses, velocities and pump duty for every path |
| `python3 headloss.py lint FILE` | Check syntax and values only; exit `2` on errors |
| `python3 headloss.py check FILE [--vmax 2m/s] [--max-loss-per-100m 3]` | CI gate; exit `0` pass, `1` violations, `2` input error |
| `python3 headloss.py size FILE --path NAME --vmax 1.5m/s` | Smallest standard bore per pipe meeting a velocity limit |
| `python3 headloss.py sweep FILE --path NAME --from 0.5L/s --to 4L/s --steps 8` | Flow sweep table |
| `python3 headloss.py calc --len 35m --d 50mm --q 2L/s [...]` | One-off single-pipe calculation |
| `python3 headloss.py props --temp 20C` | Water density and viscosity |

### File format

- `temp VALUE` (optional, default `20C`): water temperature, Celsius or Fahrenheit.
- `path NAME flow=VALUE`: starts a pipe path with its design flow. Names may use letters, digits, `-` and `_`.
- `pipe KEY=VALUE ...` (one or more per path): keys are `name`, `len` (required), `d` (required, inside diameter), `rough` (default `steel`), `dz` (elevation rise), `k` (minor losses), `method` (`dw` or `hw`), `c` (Hazen–Williams C, default 130) and `vmax` (velocity limit for `check`).
- `pump curve=Q:H,Q:H,...`: at most one per path. Flows must increase; head must not rise with flow.
- `#` starts a comment.

Fittings in `k=` are summed: `k=0.5+elbow90*2+gate_valve`. The built-in K values are typical figures; replace them with supplier data for design work.

## How to test

```bash
python3 -m pip install pytest
python3 -m pytest
```

The tests cover unit parsing, temperature conversion and the water table, Colebrook and laminar/transition friction, the Darcy–Weisbach and Hazen–Williams formulas, fitting K sums, line-numbered parse errors, pump curve validation, the pump duty point, sizing, the `check` exit codes, JSON output and the `calc` command.

## Limits

- Water only. Temperature range 0–100 °C.
- Steady, incompressible flow. No transient (water hammer) analysis.
- Fittings use constant K values, not Reynolds-dependent ones.
- Sizing picks from a list of nominal inside diameters (15–300 mm); pipe wall thickness is not modelled.
