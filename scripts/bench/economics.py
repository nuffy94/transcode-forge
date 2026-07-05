"""$/100GB economics model for Transcode Forge on Linode Dedicated CPU plans.

The model is deliberately simple:

    $/100GB = (plan $/hr [+ Object Storage $/hr]) / (GB-in per hour) * 100

``GB-in per hour`` comes from a benchmark report (scripts/bench/report.py,
``gb_in_per_hour`` for the group you're costing — decimal GB of *source*
bytes processed per encode-hour).

Plan prices are Linode list prices as of 2026-07
(https://www.linode.com/pricing/ — "Dedicated CPU" table). If Linode
changes pricing, update LINODE_DEDICATED_PLANS and
OBJECT_STORAGE_MONTHLY_USD below; everything else derives from them.

Usage:
    uv run python -m scripts.bench.economics --plan dedicated-8gb --gb-per-hour 25.4
    uv run python -m scripts.bench.economics --plan dedicated-16gb \
        --gb-per-hour 25.4 --object-storage
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

# Linode's hourly rates assume a 730-hour month (billing caps at the monthly price).
HOURS_PER_MONTH = 730

# Object Storage base tier (250GB / 1TB transfer) — flat monthly adder.
OBJECT_STORAGE_MONTHLY_USD = 5.00


@dataclass(frozen=True)
class PlanPreset:
    """A Linode Dedicated CPU plan at 2026-07 list price."""

    key: str
    label: str
    vcpus: int
    monthly_usd: float
    hourly_usd: float


LINODE_DEDICATED_PLANS: dict[str, PlanPreset] = {
    plan.key: plan
    for plan in (
        PlanPreset("dedicated-8gb", "Linode Dedicated 8GB (4 vCPU)", 4, 72.00, 0.108),
        PlanPreset("dedicated-16gb", "Linode Dedicated 16GB (8 vCPU)", 8, 144.00, 0.216),
        PlanPreset("dedicated-32gb", "Linode Dedicated 32GB (16 vCPU)", 16, 288.00, 0.432),
    )
}


def object_storage_hourly_usd() -> float:
    """The flat Object Storage adder amortized to an hourly rate."""
    return OBJECT_STORAGE_MONTHLY_USD / HOURS_PER_MONTH


def dollars_per_100gb(
    hourly_usd: float,
    gb_in_per_hour: float,
    *,
    object_storage: bool = False,
) -> float:
    """Cost to push 100 decimal GB of source video through the transcoder.

    Args:
        hourly_usd: compute cost per hour (a PlanPreset.hourly_usd).
        gb_in_per_hour: source GB processed per encode-hour, from a
            benchmark report's ``gb_in_per_hour``.
        object_storage: add the flat Object Storage monthly fee, amortized.

    Raises:
        ValueError: on non-positive throughput or negative cost.
    """
    if gb_in_per_hour <= 0:
        raise ValueError(f"gb_in_per_hour must be positive, got {gb_in_per_hour}")
    if hourly_usd < 0:
        raise ValueError(f"hourly_usd must be non-negative, got {hourly_usd}")
    total_hourly = hourly_usd + (object_storage_hourly_usd() if object_storage else 0.0)
    return total_hourly / gb_in_per_hour * 100.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.bench.economics",
        description="$/100GB transcoding cost on Linode Dedicated CPU plans.",
    )
    parser.add_argument(
        "--plan",
        required=True,
        choices=sorted(LINODE_DEDICATED_PLANS),
        help="Linode Dedicated CPU plan preset",
    )
    parser.add_argument(
        "--gb-per-hour",
        required=True,
        type=float,
        help="GB-in per hour (gb_in_per_hour from a scripts.bench.report group)",
    )
    parser.add_argument(
        "--object-storage",
        action="store_true",
        help=f"add the Object Storage flat fee (${OBJECT_STORAGE_MONTHLY_USD:.2f}/mo, amortized)",
    )
    args = parser.parse_args(argv)

    plan = LINODE_DEDICATED_PLANS[args.plan]
    try:
        cost = dollars_per_100gb(
            plan.hourly_usd, args.gb_per_hour, object_storage=args.object_storage
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    storage_note = " + Object Storage" if args.object_storage else ""
    print(f"{plan.label}{storage_note} @ {args.gb_per_hour:.2f} GB-in/hr")
    print(f"  plan rate:  ${plan.hourly_usd:.3f}/hr (${plan.monthly_usd:.2f}/mo)")
    if args.object_storage:
        print(
            f"  storage:    ${object_storage_hourly_usd():.4f}/hr "
            f"(${OBJECT_STORAGE_MONTHLY_USD:.2f}/mo / {HOURS_PER_MONTH}h)"
        )
    print(f"  $/100GB-in: ${cost:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
