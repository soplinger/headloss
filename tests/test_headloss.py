import json
import math
from pathlib import Path

import pytest

import headloss as hl


EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "pump-house.txt"


def test_quantity_parsing_converts_to_si():
    assert hl.parse_quantity("50mm", hl.LENGTH_UNITS, "d") == pytest.approx(0.05)
    assert hl.parse_quantity("2.5L/s", hl.FLOW_UNITS, "flow") == pytest.approx(0.0025)
    assert hl.parse_quantity("3m3/h", hl.FLOW_UNITS, "flow") == pytest.approx(3 / 3600)
    assert hl.parse_quantity("1ft/s", hl.VELOCITY_UNITS, "v") == pytest.approx(0.3048)


def test_quantity_rejects_unknown_unit_and_bare_numbers():
    with pytest.raises(ValueError, match="unknown flow unit"):
        hl.parse_quantity("2bananas", hl.FLOW_UNITS, "flow")
    with pytest.raises(ValueError, match="not a number with a unit"):
        hl.parse_quantity("fifty", hl.LENGTH_UNITS, "length")


def test_temperature_accepts_fahrenheit_and_rejects_out_of_range():
    assert hl.parse_temp("68F") == pytest.approx(20.0)
    with pytest.raises(ValueError, match="outside"):
        hl.parse_temp("150C")


def test_water_properties_interpolate_between_table_points():
    rho, mu = hl.water_properties(15.0)
    assert 998.2 < rho < 999.7
    assert 0.00113 < mu < 0.00131
    assert hl.water_properties(20.0)[0] == pytest.approx(998.21)


def test_colebrook_matches_known_value():
    # Re 1e5, smooth pipe: Colebrook gives f ~ 0.0180 (Blasius-like region).
    f = hl._colebrook(1e5, 0.0)
    assert f == pytest.approx(0.01800, rel=0.01)


def test_laminar_friction_is_64_over_re_and_transition_is_continuous():
    assert hl.friction_factor(1000.0, 0.001) == pytest.approx(0.064)
    below = hl.friction_factor(1999.9, 0.001)
    mid = hl.friction_factor(3000.0, 0.001)
    above = hl.friction_factor(4000.1, 0.001)
    assert below == pytest.approx(0.032, rel=0.01)
    assert min(below, above) <= mid <= max(below, above)


def test_darcy_weisbach_loss_matches_hand_calculation():
    pipe = hl.Pipe(line=1, length=100.0, diameter=0.05, roughness=0.0, rough_label="smooth",
                   dz=0.0, k=0.0, method="dw", c=130.0)
    q = 0.0025
    rho, mu = hl.water_properties(20.0)
    result = hl.analyse_pipe(pipe, q, rho, mu)
    v = q / (math.pi * 0.05 ** 2 / 4)
    expected = hl.friction_factor(rho * v * 0.05 / mu, 0.0) * (100 / 0.05) * v * v / (2 * 9.80665)
    assert result.velocity == pytest.approx(v)
    assert result.loss_m == pytest.approx(expected)


def test_hazen_williams_formula():
    pipe = hl.Pipe(line=1, length=100.0, diameter=0.05, roughness=0.0, rough_label="",
                   dz=0.0, k=0.0, method="hw", c=130.0)
    rho, mu = hl.water_properties(20.0)
    result = hl.analyse_pipe(pipe, 0.0025, rho, mu)
    expected = 10.67 * 100 * 0.0025 ** 1.852 / (130 ** 1.852 * 0.05 ** 4.8704)
    assert result.loss_m == pytest.approx(expected)


def test_minor_loss_k_sums_fittings_and_multipliers():
    assert hl.parse_k("0.5+elbow90*2+gate_valve") == pytest.approx(0.5 + 1.8 + 0.2)
    with pytest.raises(ValueError, match="unknown fitting"):
        hl.parse_k("widget")


def test_parse_model_reports_line_numbered_errors():
    text = "path a flow=2L/s\npipe len=10m\npipe len=5m d=0m\nbogus line\n"
    model, errors = hl.parse_model(text)
    lines = dict(errors)
    assert lines[2] == "pipe needs d="
    assert "must be positive" in lines[3]
    assert "unknown directive" in lines[4]


def test_parse_model_rejects_pump_that_rises_with_flow():
    text = ("path a flow=1L/s\npipe len=10m d=25mm\n"
            "pump curve=0L/s:10m,2L/s:20m\n")
    _, errors = hl.parse_model(text)
    assert errors and "must not rise" in errors[0][1]


def test_pump_duty_point_meets_system_curve():
    model, errors = hl.parse_model(EXAMPLE.read_text())
    assert not errors
    path = model.paths[0]
    rho, mu = hl.water_properties(model.temp_c)
    duty = hl.duty_point(path, rho, mu)
    assert duty.status == "ok"
    assert hl.pump_head(path.pump.points, duty.flow) == pytest.approx(
        hl.analyse_path(path, rho, mu, duty.flow).tdh_m, abs=1e-4)
    assert duty.meets_design


def test_pump_that_cannot_reach_head_is_reported():
    text = ("path a flow=1L/s\npipe len=10m d=25mm dz=20m\n"
            "pump curve=0L/s:5m,1L/s:4m\n")
    model, _ = hl.parse_model(text)
    rho, mu = hl.water_properties(20.0)
    duty = hl.duty_point(model.paths[0], rho, mu)
    assert not duty.meets_design
    assert "no intersection" in duty.status


def test_sizing_picks_smallest_standard_bore_meeting_velocity():
    model, _ = hl.parse_model("path s flow=1L/s\npipe len=10m d=15mm\n")
    d_min = math.sqrt(4 * 0.001 / (math.pi * 1.5))
    chosen = next(mm for mm in hl.STANDARD_ID_MM if mm / 1000 >= d_min)
    assert d_min * 1000 == pytest.approx(29.14, abs=0.01)
    assert chosen == 32


def test_cli_check_exit_codes(tmp_path, capsys):
    ok = tmp_path / "ok.txt"
    ok.write_text("path a flow=0.5L/s\npipe len=10m d=32mm\n")
    assert hl.main(["check", str(ok), "--vmax", "2m/s"]) == 0

    tight = tmp_path / "tight.txt"
    tight.write_text("path a flow=3L/s\npipe len=10m d=15mm\n")
    assert hl.main(["check", str(tight), "--vmax", "2m/s"]) == 1
    assert f"{tight}:2:" in capsys.readouterr().out

    broken = tmp_path / "broken.txt"
    broken.write_text("path a flow=3lps\n")
    assert hl.main(["lint", str(broken)]) == 2


def test_cli_report_json_is_valid(capsys):
    assert hl.main(["report", str(EXAMPLE), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in data["paths"]] == ["well-to-tank", "garden"]
    assert data["paths"][0]["pump"]["meets_design"] is True


def test_cli_calc_single_pipe(capsys):
    assert hl.main(["calc", "--len", "35m", "--d", "50mm", "--q", "2L/s",
                    "--dz", "3m", "--k", "elbow90*2"]) == 0
    out = capsys.readouterr().out
    assert "Path calc" in out and "TDH" in out
