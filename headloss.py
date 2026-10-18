#!/usr/bin/env python3
"""headloss: offline pipe-network head-loss calculator.

Reads a plain-text file of pipe paths, works out friction and minor losses
(Darcy-Weisbach with Colebrook-White, or Hazen-Williams), finds pump duty
points, sizes pipes against a velocity limit and gates CI on limits.
Standard library only.
"""

import argparse
import dataclasses
import json
import math
import re
import sys
from dataclasses import dataclass, field

G = 9.80665  # m/s^2

LENGTH_UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "km": 1000.0, "ft": 0.3048, "in": 0.0254}
FLOW_UNITS = {"m3/s": 1.0, "m3/h": 1.0 / 3600.0, "l/s": 0.001, "l/min": 0.001 / 60.0, "gpm": 6.30902e-5}
VELOCITY_UNITS = {"m/s": 1.0, "ft/s": 0.3048}

# Absolute roughness in millimetres for common materials.
ROUGHNESS_MM = {
    "steel": 0.045,
    "galvanized": 0.15,
    "castiron": 0.26,
    "copper": 0.0015,
    "pvc": 0.0015,
    "pe": 0.007,
    "concrete": 0.3,
}

# Typical loss coefficients (K). Replace with your supplier's data where known.
FITTING_K = {
    "entrance": 0.5,
    "exit": 1.0,
    "elbow90": 0.9,
    "elbow45": 0.4,
    "tee_branch": 1.0,
    "tee_run": 0.4,
    "gate_valve": 0.2,
    "ball_valve": 0.05,
    "globe_valve": 10.0,
    "check_valve": 2.0,
}

# Nominal inside diameters offered when sizing, in millimetres.
STANDARD_ID_MM = (15, 20, 25, 32, 40, 50, 65, 80, 100, 125, 150, 200, 250, 300)

# Water density (kg/m^3) and dynamic viscosity (mPa.s) by temperature (C).
_WATER_T = (0, 10, 20, 30, 40, 50, 60, 80, 100)
_WATER_RHO = (999.84, 999.70, 998.21, 995.65, 992.22, 988.04, 983.20, 971.80, 958.40)
_WATER_MU = (1.792, 1.307, 1.002, 0.798, 0.653, 0.547, 0.466, 0.355, 0.282)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
QTY_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z/³0-9]*)$")
TEMP_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([CcFf]?)$")
FITTING_RE = re.compile(r"^([a-z][a-z0-9_]*)(?:\*(\d+))?$")

PIPE_KEYS = {"name", "len", "d", "rough", "dz", "k", "method", "c", "vmax"}


# ---------------------------------------------------------------- parsing

def parse_quantity(text, table, what):
    """Parse '35m' or '2.5L/s' into SI using a unit table."""
    m = QTY_RE.match(text.strip())
    if not m:
        raise ValueError(f"{what} '{text}' is not a number with a unit")
    unit = m.group(2).lower().replace("³", "3")
    if unit not in table:
        raise ValueError(f"unknown {what} unit '{m.group(2)}' (use {', '.join(table)})")
    return float(m.group(1)) * table[unit]


def parse_number(text, what):
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{what} '{text}' is not a number") from None


def parse_temp(text):
    m = TEMP_RE.match(text.strip())
    if not m:
        raise ValueError(f"temperature '{text}' should look like 20C or 68F")
    value = float(m.group(1))
    if m.group(2).lower() == "f":
        value = (value - 32.0) * 5.0 / 9.0
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"temperature {value:.1f} C is outside the 0-100 C water table")
    return value


def parse_roughness(text):
    key = text.strip().lower()
    if key in ROUGHNESS_MM:
        return ROUGHNESS_MM[key] * 1e-3, key
    eps = parse_quantity(text, LENGTH_UNITS, "roughness")
    if eps < 0:
        raise ValueError("roughness cannot be negative")
    return eps, text


def parse_k(text):
    """K as a number, a fitting name, or a sum like 0.5+elbow90*2+gate_valve."""
    total = 0.0
    for term in text.split("+"):
        term = term.strip().lower()
        try:
            total += parse_number(term, "k")
            continue
        except ValueError:
            pass
        m = FITTING_RE.match(term)
        if not m or m.group(1) not in FITTING_K:
            known = ", ".join(sorted(FITTING_K))
            raise ValueError(f"unknown fitting '{term}' (known: {known})")
        total += FITTING_K[m.group(1)] * int(m.group(2) or 1)
    if total < 0:
        raise ValueError("k cannot be negative")
    return total


def _kv(rest):
    out = {}
    for token in rest.split():
        if "=" not in token:
            raise ValueError(f"expected key=value, got '{token}'")
        key, _, value = token.partition("=")
        key = key.lower()
        if key in out:
            raise ValueError(f"duplicate key '{key}'")
        out[key] = value
    return out


def _check_keys(opts, allowed, where):
    unknown = sorted(set(opts) - allowed)
    if unknown:
        raise ValueError(f"unknown {where} key '{unknown[0]}'")


@dataclass
class Pipe:
    line: int
    length: float
    diameter: float
    roughness: float
    rough_label: str
    dz: float
    k: float
    method: str
    c: float
    vmax: float = None
    name: str = ""


@dataclass
class Pump:
    line: int
    points: list  # [(flow m^3/s, head m)], flow increasing


@dataclass
class Path:
    name: str
    line: int
    flow: float
    pipes: list = field(default_factory=list)
    pump: Pump = None


@dataclass
class Model:
    temp_c: float
    paths: list


def parse_pipe(rest, line):
    opts = _kv(rest)
    _check_keys(opts, PIPE_KEYS, "pipe")
    for required in ("len", "d"):
        if required not in opts:
            raise ValueError(f"pipe needs {required}=")
    length = parse_quantity(opts["len"], LENGTH_UNITS, "length")
    diameter = parse_quantity(opts["d"], LENGTH_UNITS, "diameter")
    if length <= 0 or diameter <= 0:
        raise ValueError("len and d must be positive")
    roughness, label = parse_roughness(opts.get("rough", "steel"))
    dz = parse_quantity(opts["dz"], LENGTH_UNITS, "elevation") if "dz" in opts else 0.0
    k = parse_k(opts.get("k", "0"))
    method = opts.get("method", "dw").lower()
    if method not in ("dw", "hw"):
        raise ValueError("method must be dw (Darcy-Weisbach) or hw (Hazen-Williams)")
    c = parse_number(opts.get("c", "130"), "c")
    if c <= 0:
        raise ValueError("c must be positive")
    vmax = None
    if "vmax" in opts:
        vmax = parse_quantity(opts["vmax"], VELOCITY_UNITS, "velocity")
        if vmax <= 0:
            raise ValueError("vmax must be positive")
    return Pipe(line, length, diameter, roughness, label, dz, k, method, c, vmax,
                opts.get("name", ""))


def parse_path_header(rest, line):
    parts = rest.split(None, 1)
    if not parts:
        raise ValueError("path needs a name")
    name = parts[0]
    if not NAME_RE.match(name):
        raise ValueError(f"path name '{name}' may only use letters, digits, - and _")
    opts = _kv(parts[1] if len(parts) > 1 else "")
    _check_keys(opts, {"flow"}, "path")
    if "flow" not in opts:
        raise ValueError("path needs flow=")
    flow = parse_quantity(opts["flow"], FLOW_UNITS, "flow")
    if flow <= 0:
        raise ValueError("flow must be positive")
    return Path(name, line, flow)


def parse_pump(rest, line):
    opts = _kv(rest)
    _check_keys(opts, {"curve"}, "pump")
    if "curve" not in opts:
        raise ValueError("pump needs curve=Q:H,Q:H,...")
    points = []
    for item in opts["curve"].split(","):
        q_text, sep, h_text = item.partition(":")
        if not sep:
            raise ValueError(f"curve point '{item}' should look like 2L/s:34m")
        points.append((parse_quantity(q_text, FLOW_UNITS, "pump flow"),
                       parse_quantity(h_text, LENGTH_UNITS, "pump head")))
    if len(points) < 2:
        raise ValueError("pump curve needs at least two points")
    for (q0, _), (q1, _) in zip(points, points[1:]):
        if q1 <= q0:
            raise ValueError("pump curve flows must strictly increase")
    for (_, h0), (_, h1) in zip(points, points[1:]):
        if h1 > h0:
            raise ValueError("pump head must not rise as flow rises")
    return Pump(line, points)


def parse_model(text):
    """Return (Model, errors). errors is a list of (line, message)."""
    temp_c = 20.0
    paths, errors, names = [], [], set()
    current = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        word, _, rest = body.partition(" ")
        word = word.lower()
        try:
            if word == "temp":
                temp_c = parse_temp(rest)
            elif word == "path":
                current = None  # a bad header must not let pipes attach to the old path
                current = parse_path_header(rest, lineno)
                if current.name in names:
                    raise ValueError(f"duplicate path '{current.name}'")
                names.add(current.name)
                paths.append(current)
            elif word == "pipe":
                if current is None:
                    raise ValueError("pipe line before any path")
                current.pipes.append(parse_pipe(rest, lineno))
            elif word == "pump":
                if current is None:
                    raise ValueError("pump line before any path")
                if current.pump is not None:
                    raise ValueError(f"path '{current.name}' already has a pump")
                current.pump = parse_pump(rest, lineno)
            else:
                raise ValueError(f"unknown directive '{word}'")
        except ValueError as exc:
            errors.append((lineno, str(exc)))
    for path in paths:
        if not path.pipes:
            errors.append((path.line, f"path '{path.name}' has no pipe lines"))
    return Model(temp_c, paths), errors


# ---------------------------------------------------------------- physics

def water_properties(temp_c):
    """Return (density kg/m^3, dynamic viscosity Pa.s) by linear interpolation."""
    rho = _interp(temp_c, _WATER_T, _WATER_RHO)
    mu = _interp(temp_c, _WATER_T, _WATER_MU) * 1e-3
    return rho, mu


def _interp(x, xs, ys):
    for i in range(len(xs) - 1):
        if x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def _colebrook(re_, rel):
    """Turbulent Darcy friction factor from Colebrook-White (Newton on 1/sqrt f)."""
    arg = rel / 3.7 + 5.74 / re_ ** 0.9
    x = 1.0 / math.sqrt(0.25 / math.log10(arg) ** 2)  # Swamee-Jain start
    for _ in range(100):
        a = rel / 3.7 + 2.51 * x / re_
        f_x = x + 2.0 * math.log10(a)
        df_x = 1.0 + (2.0 / math.log(10)) * (2.51 / re_) / a
        step = f_x / df_x
        x -= step
        if abs(step) < 1e-12:
            break
    return 1.0 / (x * x)


def friction_factor(re_, rel):
    """Laminar below Re 2000, blended through 2000-4000, Colebrook above."""
    if re_ < 2000.0:
        return 64.0 / re_
    turbulent_4000 = _colebrook(4000.0, rel)
    if re_ < 4000.0:
        t = (re_ - 2000.0) / 2000.0
        return (1.0 - t) * 0.032 + t * turbulent_4000
    return _colebrook(re_, rel)


@dataclass
class PipeResult:
    pipe: Pipe
    velocity: float
    re: float
    f: float
    friction_m: float
    minor_m: float
    loss_m: float
    dp_kpa: float


def analyse_pipe(pipe, q, rho, mu):
    if q <= 0:
        return PipeResult(pipe, 0.0, 0.0, None, 0.0, 0.0, 0.0, 0.0)
    area = math.pi * pipe.diameter ** 2 / 4.0
    v = q / area
    v2g = v * v / (2.0 * G)
    re_ = rho * v * pipe.diameter / mu
    minor = pipe.k * v2g
    if pipe.method == "hw":
        friction = 10.67 * pipe.length * q ** 1.852 / (pipe.c ** 1.852 * pipe.diameter ** 4.8704)
        f = None
    else:
        f = friction_factor(re_, pipe.roughness / pipe.diameter)
        friction = f * (pipe.length / pipe.diameter) * v2g
    loss = friction + minor
    return PipeResult(pipe, v, re_, f, friction, minor, loss, rho * G * loss / 1000.0)


@dataclass
class PathResult:
    path: Path
    flow: float
    pipes: list
    loss_m: float
    static_m: float
    tdh_m: float
    dp_kpa: float


def analyse_path(path, rho, mu, q=None):
    q = path.flow if q is None else q
    pipes = [analyse_pipe(p, q, rho, mu) for p in path.pipes]
    loss = sum(r.loss_m for r in pipes)
    static = sum(p.dz for p in path.pipes)
    dp = sum(r.dp_kpa for r in pipes)
    return PathResult(path, q, pipes, loss, static, loss + static, dp)


def pump_head(points, q):
    """Piecewise-linear pump head at flow q, or None outside the curve."""
    if q < points[0][0] or q > points[-1][0]:
        return None
    for (q0, h0), (q1, h1) in zip(points, points[1:]):
        if q <= q1:
            return h0 + (q - q0) / (q1 - q0) * (h1 - h0)
    return points[-1][1]


@dataclass
class Duty:
    status: str
    flow: float
    head: float
    meets_design: bool


def duty_point(path, rho, mu):
    """Where the pump curve meets the system curve (bisection on surplus head)."""
    points = path.pump.points
    lo, hi = points[0][0], points[-1][0]

    def surplus(q):
        return pump_head(points, q) - analyse_path(path, rho, mu, q).tdh_m

    if surplus(lo) < 0:
        return Duty("no intersection: pump cannot reach required head", None, None, False)
    if surplus(hi) >= 0:
        return Duty("beyond curve: pump still above system at its last point", hi,
                    pump_head(points, hi), hi >= path.flow)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if surplus(mid) >= 0:
            lo = mid
        else:
            hi = mid
    flow = 0.5 * (lo + hi)
    meets = flow >= path.flow * (1.0 - 1e-9)
    return Duty("ok", flow, pump_head(points, flow), meets)


# ---------------------------------------------------------------- loading

def load(path_arg):
    with open(path_arg, encoding="utf-8") as handle:
        return parse_model(handle.read())


def report_errors(filename, errors):
    for line, message in errors:
        print(f"{filename}:{line}: error: {message}", file=sys.stderr)
    print(f"{len(errors)} error(s) found", file=sys.stderr)


def props_for(model):
    return water_properties(model.temp_c)


def fmt_flow(q):
    return f"{q * 1000:.3f} L/s"


def build_report(model):
    rho, mu = props_for(model)
    results = []
    for path in model.paths:
        res = analyse_path(path, rho, mu)
        duty = duty_point(path, rho, mu) if path.pump else None
        results.append((res, duty))
    return results


def print_text_report(model, results):
    rho, mu = props_for(model)
    print(f"Water at {model.temp_c:.1f} C: density {rho:.2f} kg/m3, "
          f"viscosity {mu * 1000:.4f} mPa.s")
    for res, duty in results:
        print()
        print(f"Path {res.path.name}  design flow {fmt_flow(res.flow)}")
        print(f"  {'pipe':>4} {'line':>4} {'len m':>8} {'d mm':>7} {'v m/s':>7} "
              f"{'Re':>10} {'f':>7} {'loss m':>8} {'kPa':>7}")
        for i, pr in enumerate(res.pipes, 1):
            p = pr.pipe
            f_text = f"{pr.f:.4f}" if pr.f is not None else "hw"
            print(f"  {i:>4} {p.line:>4} {p.length:>8.2f} {p.diameter * 1000:>7.1f} "
                  f"{pr.velocity:>7.2f} {pr.re:>10.0f} {f_text:>7} {pr.loss_m:>8.3f} "
                  f"{pr.dp_kpa:>7.2f}")
        print(f"  losses {res.loss_m:.3f} m + static lift {res.static_m:.3f} m = "
              f"TDH {res.tdh_m:.3f} m  ({res.dp_kpa:.2f} kPa friction)")
        if duty is not None:
            if duty.flow is None:
                print(f"  pump duty: {duty.status}")
            else:
                verdict = "meets design flow" if duty.meets_design else "short of design flow"
                print(f"  pump duty: {fmt_flow(duty.flow)} at {duty.head:.2f} m "
                      f"({duty.status}; {verdict})")


def json_report(model, results):
    rho, mu = props_for(model)
    out = {"temp_c": model.temp_c, "density_kg_m3": rho, "viscosity_pa_s": mu, "paths": []}
    for res, duty in results:
        entry = {
            "name": res.path.name,
            "design_flow_m3_s": res.flow,
            "loss_m": res.loss_m,
            "static_m": res.static_m,
            "tdh_m": res.tdh_m,
            "dp_kpa": res.dp_kpa,
            "pipes": [
                {"line": pr.pipe.line, "velocity_m_s": pr.velocity, "re": pr.re,
                 "friction_factor": pr.f, "loss_m": pr.loss_m, "dp_kpa": pr.dp_kpa}
                for pr in res.pipes
            ],
        }
        if duty is not None:
            entry["pump"] = {"status": duty.status, "flow_m3_s": duty.flow,
                             "head_m": duty.head, "meets_design": duty.meets_design}
        out["paths"].append(entry)
    return out


# ---------------------------------------------------------------- commands

def cmd_report(args):
    model, errors = load(args.file)
    if errors:
        report_errors(args.file, errors)
        return 2
    results = build_report(model)
    if args.json:
        print(json.dumps(json_report(model, results), indent=2))
    else:
        print_text_report(model, results)
    return 0


def cmd_lint(args):
    model, errors = load(args.file)
    if errors:
        report_errors(args.file, errors)
        return 2
    pipes = sum(len(p.pipes) for p in model.paths)
    print(f"{args.file}: OK ({len(model.paths)} path(s), {pipes} pipe(s))")
    return 0


def cmd_check(args):
    model, errors = load(args.file)
    if errors:
        report_errors(args.file, errors)
        return 2
    rho, mu = props_for(model)
    violations = []
    for path in model.paths:
        res = analyse_path(path, rho, mu)
        for pr in res.pipes:
            p = pr.pipe
            limit = p.vmax if p.vmax is not None else args.vmax
            if limit is not None and pr.velocity > limit:
                violations.append((p.line, f"path {path.name}: velocity {pr.velocity:.2f} m/s "
                                           f"exceeds {limit:.2f} m/s"))
            if args.max_loss_per_100m is not None:
                per100 = pr.loss_m / p.length * 100.0
                if per100 > args.max_loss_per_100m:
                    violations.append((p.line, f"path {path.name}: loss {per100:.2f} m per 100 m "
                                               f"exceeds {args.max_loss_per_100m:.2f}"))
        if path.pump is not None:
            duty = duty_point(path, rho, mu)
            if not duty.meets_design:
                violations.append((path.pump.line, f"path {path.name}: pump does not deliver "
                                                   f"design flow ({duty.status})"))
    for line, message in violations:
        print(f"{args.file}:{line}: {message}")
    if violations:
        print(f"{len(violations)} violation(s)", file=sys.stderr)
        return 1
    print(f"{args.file}: all limits met")
    return 0


def cmd_size(args):
    model, errors = load(args.file)
    if errors:
        report_errors(args.file, errors)
        return 2
    rho, mu = props_for(model)
    paths = [p for p in model.paths if args.path is None or p.name == args.path]
    if not paths:
        print(f"no path named '{args.path}'", file=sys.stderr)
        return 2
    for path in paths:
        print(f"Path {path.name}  design flow {fmt_flow(path.flow)}  limit {args.vmax:.2f} m/s")
        for pipe in path.pipes:
            d_min = math.sqrt(4.0 * path.flow / (math.pi * args.vmax))
            chosen = next((mm for mm in STANDARD_ID_MM if mm / 1000.0 >= d_min), None)
            current = pipe.diameter * 1000.0
            if chosen is None:
                print(f"  line {pipe.line}: needs {d_min * 1000:.1f} mm, no standard size fits")
                continue
            sized = dataclasses.replace(pipe, diameter=chosen / 1000.0)
            pr = analyse_pipe(sized, path.flow, rho, mu)
            per100 = pr.loss_m / sized.length * 100.0
            print(f"  line {pipe.line}: now {current:.1f} mm -> minimum {d_min * 1000:.1f} mm, "
                  f"use {chosen} mm (v {pr.velocity:.2f} m/s, {per100:.2f} m per 100 m)")
    return 0


def cmd_sweep(args):
    model, errors = load(args.file)
    if errors:
        report_errors(args.file, errors)
        return 2
    rho, mu = props_for(model)
    matches = [p for p in model.paths if p.name == args.path]
    if not matches:
        print(f"no path named '{args.path}'", file=sys.stderr)
        return 2
    path = matches[0]
    print(f"Path {path.name}: flow sweep ({args.steps} steps)")
    print(f"  {'flow':>12} {'max v m/s':>10} {'losses m':>10} {'TDH m':>9} {'pump m':>8}")
    for i in range(args.steps):
        frac = i / (args.steps - 1) if args.steps > 1 else 0.0
        q = args.start + frac * (args.stop - args.start)
        if q <= 0:
            continue
        res = analyse_path(path, rho, mu, q)
        vmax = max((pr.velocity for pr in res.pipes), default=0.0)
        head = pump_head(path.pump.points, q) if path.pump else None
        head_text = f"{head:.2f}" if head is not None else "-"
        print(f"  {fmt_flow(q):>12} {vmax:>10.2f} {res.loss_m:>10.3f} {res.tdh_m:>9.3f} "
              f"{head_text:>8}")
    return 0


def cmd_props(args):
    rho, mu = water_properties(args.temp)
    print(f"temperature {args.temp:.1f} C")
    print(f"density            {rho:.2f} kg/m3")
    print(f"dynamic viscosity  {mu * 1000:.4f} mPa.s")
    print(f"kinematic          {mu / rho * 1e6:.4f} mm2/s")
    return 0


def cmd_calc(args):
    text = (f"temp {args.temp}\n"
            f"path calc flow={args.q}\n"
            f"pipe len={args.len} d={args.d} rough={args.rough} dz={args.dz} "
            f"k={args.k} method={args.method} c={args.c}\n")
    model, errors = parse_model(text)
    if errors:
        for line, message in errors:
            print(f"calc: error: {message}", file=sys.stderr)
        return 2
    print_text_report(model, build_report(model))
    return 0


def _quantity_arg(table, what):
    def convert(text):
        try:
            return parse_quantity(text, table, what)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return convert


def _temp_arg(text):
    try:
        return parse_temp(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser():
    parser = argparse.ArgumentParser(prog="headloss", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="show losses, velocities and pump duty per path")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("lint", help="check the file for syntax and value errors")
    p.add_argument("file")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("check", help="CI gate: velocity, loss and pump limits")
    p.add_argument("file")
    p.add_argument("--vmax", type=_quantity_arg(VELOCITY_UNITS, "velocity"),
                   help="default velocity limit for pipes without vmax=, e.g. 2.0m/s")
    p.add_argument("--max-loss-per-100m", type=float,
                   help="largest allowed loss in m of head per 100 m of pipe")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("size", help="smallest standard bore meeting a velocity limit")
    p.add_argument("file")
    p.add_argument("--path", help="only this path")
    p.add_argument("--vmax", type=_quantity_arg(VELOCITY_UNITS, "velocity"), required=True)
    p.set_defaults(func=cmd_size)

    p = sub.add_parser("sweep", help="tabulate losses across a range of flows")
    p.add_argument("file")
    p.add_argument("--path", required=True)
    p.add_argument("--from", dest="start", type=_quantity_arg(FLOW_UNITS, "flow"), required=True)
    p.add_argument("--to", dest="stop", type=_quantity_arg(FLOW_UNITS, "flow"), required=True)
    p.add_argument("--steps", type=int, default=6)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("props", help="water density and viscosity at a temperature")
    p.add_argument("--temp", type=_temp_arg, default=20.0, help="e.g. 20C or 68F")
    p.set_defaults(func=cmd_props)

    p = sub.add_parser("calc", help="one-off calculation for a single pipe")
    p.add_argument("--len", required=True, help="e.g. 35m")
    p.add_argument("--d", required=True, help="inside diameter, e.g. 50mm")
    p.add_argument("--q", required=True, help="flow, e.g. 2L/s")
    p.add_argument("--rough", default="steel")
    p.add_argument("--dz", default="0m", help="elevation rise, e.g. 3m")
    p.add_argument("--k", default="0", help="minor loss K or fittings, e.g. elbow90*2+gate_valve")
    p.add_argument("--method", default="dw", choices=["dw", "hw"])
    p.add_argument("--c", default="130", help="Hazen-Williams C")
    p.add_argument("--temp", default="20C")
    p.set_defaults(func=cmd_calc)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print(f"headloss: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
