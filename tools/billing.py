#!/usr/bin/env python3
"""Stage 1 usage metering: per-user playtime, solo/group split, and a dry-run bill.

This is a pure calculator over a *presence ledger* -- an append-only JSONL file of occupancy
samples produced by ``tools/presence_meter.py``. Each ledger line records who was present on
one instance at one instant::

    {"ts": "2026-07-18T20:00:00Z", "instance": "enshrouded-primary", "present": ["alice@ex", "bob@ex"]}

No money is moved here. Given a nominal per-instance run-cost and the group-size multiplier
schedule (``billing.yaml``), it reports how many hours each user played and when, how much was
solo vs. grouped, and the hypothetical cost-share each person would owe -- so the group can
test-fly the model on the current free server before any cloud cost or payments exist.

Model (see docs/cloud-hosting-cost-analysis.md): for each sample interval of duration ``d`` with
``n`` players present, each present user accrues ``rate_per_second * d * m(n) / n``. Solo play
(``n == 1``) carries a premium ``m(1) > 1``; larger groups are subsidized ``m(n) < 1``. Charges
therefore do not sum to the raw server cost per interval -- the difference is the shared kitty,
reported here so the group can see it nets out over time.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def multiplier(n: int, schedule: dict[int, float], default: float) -> float:
    """Return m(n): the group-size multiplier for ``n`` present players."""
    if n <= 0:
        return 0.0
    return float(schedule.get(n, default))


def current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Return the [start, end) UTC datetimes for a ``YYYY-MM`` calendar month."""
    year, mon = (int(part) for part in month.split("-", 1))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC) if mon == 12 else datetime(year, mon + 1, 1, tzinfo=UTC)
    return start, end


def available_months(samples: list[dict[str, Any]]) -> list[str]:
    """Sorted (ascending) list of ``YYYY-MM`` months that have at least one sample."""
    return sorted({sample["ts_dt"].astimezone(UTC).strftime("%Y-%m") for sample in samples})


def filter_by_month(samples: list[dict[str, Any]], month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    return [sample for sample in samples if start <= sample["ts_dt"] < end]


def load_ledger(path: Path, instance: str | None = None) -> list[dict[str, Any]]:
    """Read a presence-ledger JSONL file into validated, time-parsed sample records."""
    samples: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return samples
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or "ts" not in record or "instance" not in record:
            continue
        if instance is not None and record.get("instance") != instance:
            continue
        present = record.get("present")
        if not isinstance(present, list):
            present = []
        try:
            ts_dt = parse_ts(str(record["ts"]))
        except ValueError:
            continue
        samples.append({
            "ts_dt": ts_dt,
            "instance": record["instance"],
            "present": sorted({str(p) for p in present if isinstance(p, str) and p}),
        })
    return samples


def normalize_schedule(raw: Any) -> dict[int, float]:
    schedule: dict[int, float] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                schedule[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return schedule


def compute_report(
    samples: list[dict[str, Any]],
    *,
    rate_per_hour: float,
    schedule: dict[int, float],
    default_multiplier: float,
    sample_interval_s: float,
    max_gap_s: float,
) -> dict[str, Any]:
    """Turn presence samples for a single instance into a per-user playtime + bill report."""
    ordered = sorted(samples, key=lambda s: s["ts_dt"])
    rate_per_sec = rate_per_hour / 3600.0
    users: dict[str, dict[str, float]] = defaultdict(lambda: {"seconds": 0.0, "solo_seconds": 0.0, "group_seconds": 0.0, "charge": 0.0})
    completed_sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    open_sessions: dict[str, dict[str, Any]] = {}
    total_charged = 0.0
    actual_cost = 0.0
    server_up_s = 0.0

    for index, sample in enumerate(ordered):
        ts = sample["ts_dt"]
        if index + 1 < len(ordered):
            delta = (ordered[index + 1]["ts_dt"] - ts).total_seconds()
            duration = min(delta, max_gap_s) if delta > 0 else sample_interval_s
        else:
            duration = sample_interval_s
        present = sample["present"]
        n = len(present)
        if n >= 1:
            server_up_s += duration
            actual_cost += rate_per_sec * duration
        m = multiplier(n, schedule, default_multiplier)
        for login in present:
            charge = rate_per_sec * duration * m / n if n > 0 else 0.0
            user = users[login]
            user["seconds"] += duration
            user["charge"] += charge
            if n == 1:
                user["solo_seconds"] += duration
            else:
                user["group_seconds"] += duration
            total_charged += charge

        # Close sessions for anyone who just dropped out, then open/extend for the present.
        for login in [login for login in open_sessions if login not in present]:
            completed_sessions[login].append(open_sessions.pop(login))
        for login in present:
            session = open_sessions.get(login)
            if session is None:
                open_sessions[login] = {"start": ts, "end": ts + timedelta(seconds=duration), "max_group": n}
            else:
                session["end"] = ts + timedelta(seconds=duration)
                session["max_group"] = max(session["max_group"], n)

    for login, session in open_sessions.items():
        completed_sessions[login].append(session)

    user_report: dict[str, Any] = {}
    for login, totals in sorted(users.items()):
        seconds = totals["seconds"]
        sessions = [
            {
                "start": iso(session["start"]),
                "end": iso(session["end"]),
                "hours": round((session["end"] - session["start"]).total_seconds() / 3600.0, 3),
                "max_group": session["max_group"],
            }
            for session in sorted(completed_sessions[login], key=lambda s: s["start"])
        ]
        user_report[login] = {
            "hours": round(seconds / 3600.0, 3),
            "solo_hours": round(totals["solo_seconds"] / 3600.0, 3),
            "group_hours": round(totals["group_seconds"] / 3600.0, 3),
            "solo_pct": round(100.0 * totals["solo_seconds"] / seconds, 1) if seconds else 0.0,
            "charge": round(totals["charge"], 2),
            "sessions": sessions,
        }

    period = {"start": iso(ordered[0]["ts_dt"]), "end": iso(ordered[-1]["ts_dt"])} if ordered else {"start": None, "end": None}
    total_play_s = sum(u["seconds"] for u in users.values())
    total_solo_s = sum(u["solo_seconds"] for u in users.values())
    return {
        "currency": None,  # filled by the CLI from config
        "run_cost_per_hour": rate_per_hour,
        "period": period,
        "users": user_report,
        "totals": {
            "player_count": len(user_report),
            "server_up_hours": round(server_up_s / 3600.0, 3),
            "actual_cost": round(actual_cost, 2),
            "charged": round(total_charged, 2),
            "kitty": round(total_charged - actual_cost, 2),
            "player_hours": round(total_play_s / 3600.0, 3),
            "solo_share_pct": round(100.0 * total_solo_s / total_play_s, 1) if total_play_s else 0.0,
        },
    }


def _hours(value: float) -> str:
    return f"{value:.2f}h"


def render_text(report: dict[str, Any], instance: str) -> str:
    currency = report.get("currency") or ""
    lines = [
        f"Usage report for {instance}  ({report.get('month', '')})",
        f"  period: {report['period']['start']} -> {report['period']['end']}",
        f"  server up: {_hours(report['totals']['server_up_hours'])}   "
        f"players: {report['totals']['player_count']}   "
        f"solo share of playtime: {report['totals']['solo_share_pct']}%",
        "",
        f"  {'user':<28}{'hours':>8}{'solo':>8}{'group':>8}{'solo%':>7}{'bill':>10}",
        f"  {'-' * 28}{'-' * 8}{'-' * 8}{'-' * 8}{'-' * 7}{'-' * 10}",
    ]
    for login, data in report["users"].items():
        lines.append(
            f"  {login:<28}{data['hours']:>8.2f}{data['solo_hours']:>8.2f}"
            f"{data['group_hours']:>8.2f}{data['solo_pct']:>6.0f}%{currency + ' ' + format(data['charge'], '.2f'):>10}"
        )
    totals = report["totals"]
    lines += [
        f"  {'-' * 69}",
        f"  actual cost: {currency} {totals['actual_cost']:.2f}   "
        f"charged: {currency} {totals['charged']:.2f}   "
        f"kitty: {currency} {totals['kitty']:.2f}",
        "",
        "  (Dry run -- no money is charged. The kitty is the surplus from solo premiums that",
        "   funds group discounts and fixed costs; it should net out over time.)",
    ]
    return "\n".join(lines)


def build_report(ledger_path: Path, config: dict[str, Any], instance: str, month: str | None = None) -> dict[str, Any]:
    """Build the report for one instance, scoped to a ``YYYY-MM`` month (default: current).

    ``available_months`` is computed from the whole ledger (before month filtering) so a caller
    can offer a month selector; the current month is always included so "this month to date" is
    selectable even before any play has happened.
    """
    all_samples = load_ledger(ledger_path, instance=instance)
    months = sorted(set(available_months(all_samples)) | {current_month()})
    selected = month if month else current_month()
    samples = filter_by_month(all_samples, selected)
    instances = config.get("instances", {})
    instance_cfg = instances.get(instance, {}) if isinstance(instances, dict) else {}
    rate = float(instance_cfg.get("run_cost_per_hour", 0.0))
    report = compute_report(
        samples,
        rate_per_hour=rate,
        schedule=normalize_schedule(config.get("multiplier_schedule")),
        default_multiplier=float(config.get("default_multiplier", 1.0)),
        sample_interval_s=float(config.get("sample_interval_seconds", 60)),
        max_gap_s=float(config.get("max_gap_seconds", 150)),
    )
    report["currency"] = config.get("currency", "USD")
    report["instance"] = instance
    report["month"] = selected
    report["available_months"] = months
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute per-user playtime and a dry-run cost-share bill.")
    parser.add_argument("--ledger", type=Path, default=Path("/var/lib/game-server-interface/presence.jsonl"))
    parser.add_argument("--config", type=Path, default=Path("/etc/game-server-interface/billing.yaml"))
    parser.add_argument("--instance", required=True, help="instance id, e.g. enshrouded-primary")
    parser.add_argument("--month", help="YYYY-MM to report (default: current month to date)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON instead of text")
    args = parser.parse_args()

    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("billing config must be a mapping")
        report = build_report(args.ledger, config, args.instance, args.month)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"billing report failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report, args.instance))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
