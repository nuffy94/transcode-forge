"""$/100GB economics model (scripts/bench/economics.py)."""

import pytest

from scripts.bench.economics import (
    HOURS_PER_MONTH,
    LINODE_DEDICATED_PLANS,
    OBJECT_STORAGE_MONTHLY_USD,
    dollars_per_100gb,
    main,
    object_storage_hourly_usd,
)


def test_plan_presets_match_2026_07_list_prices() -> None:
    plan = LINODE_DEDICATED_PLANS["dedicated-8gb"]
    assert (plan.vcpus, plan.monthly_usd, plan.hourly_usd) == (4, 72.00, 0.108)
    assert LINODE_DEDICATED_PLANS["dedicated-16gb"].hourly_usd == 0.216
    assert LINODE_DEDICATED_PLANS["dedicated-32gb"].vcpus == 16


def test_dollars_per_100gb_arithmetic() -> None:
    # $0.108/hr at 20 GB-in/hr -> $0.108 per 20GB -> $0.54 per 100GB.
    assert dollars_per_100gb(0.108, 20.0) == pytest.approx(0.54)
    # Half the throughput doubles the cost.
    assert dollars_per_100gb(0.108, 10.0) == pytest.approx(1.08)


def test_object_storage_adder() -> None:
    assert object_storage_hourly_usd() == pytest.approx(OBJECT_STORAGE_MONTHLY_USD / 730)
    assert HOURS_PER_MONTH == 730
    expected = (0.108 + 5.0 / 730) / 20.0 * 100.0
    assert dollars_per_100gb(0.108, 20.0, object_storage=True) == pytest.approx(expected)


def test_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="gb_in_per_hour"):
        dollars_per_100gb(0.108, 0.0)
    with pytest.raises(ValueError, match="gb_in_per_hour"):
        dollars_per_100gb(0.108, -5.0)
    with pytest.raises(ValueError, match="hourly_usd"):
        dollars_per_100gb(-0.01, 20.0)


def test_cli_prints_cost(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--plan", "dedicated-8gb", "--gb-per-hour", "20"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Linode Dedicated 8GB (4 vCPU)" in out
    assert "$/100GB-in: $0.54" in out


def test_cli_object_storage_flag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--plan", "dedicated-8gb", "--gb-per-hour", "20", "--object-storage"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "+ Object Storage" in out
    expected = (0.108 + 5.0 / 730) / 20.0 * 100.0
    assert f"$/100GB-in: ${expected:.2f}" in out


def test_cli_rejects_zero_throughput(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--plan", "dedicated-8gb", "--gb-per-hour", "0"])
    assert rc == 2
    assert "gb_in_per_hour" in capsys.readouterr().err
