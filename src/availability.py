from __future__ import annotations

import argparse
from datetime import datetime
import logging
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from core.config_loader import load_config
from core.logging import configure_logging
from domains.availability.checker import (
    AvailabilityResult,
    check_availability,
    checks_due,
    modes_due,
)
from infrastructure.database_targets import iter_database_targets
from infrastructure.mailer import send_mail


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check recent offline data availability")
    parser.add_argument(
        "--mode",
        help="Comma-separated modes to run now, ignoring configured due hours",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log check results without sending alert mail",
    )
    return parser.parse_args(argv)


def _requested_modes(raw_modes: str | None, cfg: dict[str, Any], now: datetime) -> list[str]:
    checks = ((cfg.get("availability") or {}).get("checks") or {})
    if raw_modes is None:
        return modes_due(cfg, now)

    modes = list(
        dict.fromkeys(
            mode.strip().lower() for mode in raw_modes.split(",") if mode.strip()
        )
    )
    invalid = [mode for mode in modes if mode not in checks]
    if invalid:
        raise ValueError(f"Unsupported availability mode(s): {invalid}")
    if not modes:
        raise ValueError("--mode must contain at least one mode")
    return modes


def _due_tables(cfg: dict[str, Any], now: datetime) -> dict[str, list[str]]:
    return checks_due(cfg, now)


def _format_datetime(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "none"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(tz).isoformat(timespec="seconds")


def _result_line(result: AvailabilityResult, tz: ZoneInfo) -> str:
    count = "unknown" if result.row_count is None else str(result.row_count)
    line = (
        f"status={result.status} mode={result.mode} db={result.target_label} "
        f"table={result.table} rows={count} "
        f"window_ist=[{_format_datetime(result.window_start, tz)}, "
        f"{_format_datetime(result.window_end, tz)}] "
        f"latest_ist={_format_datetime(result.latest_date_time, tz)}"
    )
    if result.error:
        line += f" error={result.error}"
    return line


def _alert_message(
    failures: Sequence[AvailabilityResult], tz: ZoneInfo
) -> tuple[str, str]:
    pairs = list(dict.fromkeys((result.mode, result.target_label) for result in failures))
    summary = ", ".join(f"{mode} ({label})" for mode, label in pairs)
    subject = f"[BF2 Offline Data] Data NOT available: {summary}"
    body_lines = [
        "Recent offline data is missing or could not be checked:",
        "",
    ]
    body_lines.extend(_result_line(result, tz) for result in failures)
    return subject, "\n".join(body_lines)


def run(
    cfg: dict[str, Any],
    *,
    modes: Sequence[str],
    now: datetime,
    dry_run: bool,
    logger: logging.Logger,
    tables_by_mode: dict[str, list[str]] | None = None,
) -> int:
    availability_cfg = cfg.get("availability") or {}
    tz = ZoneInfo(str(availability_cfg.get("timezone") or "UTC"))
    if not modes:
        logger.info("No availability modes are due at this hour")
        return 0

    targets = iter_database_targets(cfg)
    if not targets:
        raise RuntimeError("No configured PostgreSQL write targets are available to check")

    logger.info(
        "Running availability checks | modes=%s | targets=%s | dry_run=%s",
        ",".join(modes),
        ",".join(target.label for target in targets),
        dry_run,
    )
    results = check_availability(
        cfg,
        targets,
        modes,
        now,
        tables_by_mode=tables_by_mode,
    )
    for result in results:
        log = logger.info if result.status == "OK" else logger.warning
        log(_result_line(result, tz))

    failures = [result for result in results if result.status in {"MISSING", "ERROR"}]
    if not failures:
        logger.info("All requested data availability checks passed; no mail sent")
        return 0

    if dry_run:
        logger.info("Dry run: alert mail suppressed | failures=%d", len(failures))
        return 0

    subject, body = _alert_message(failures, tz)
    send_mail(availability_cfg.get("mail") or {}, subject, body)
    logger.info("Availability alert mail sent | failures=%d", len(failures))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config()
        logging_cfg = cfg.get("logging") or {}
        logger = configure_logging(
            level=logging_cfg.get("level", "INFO"),
            log_dir=logging_cfg.get("dir", "output/logs"),
        )
        availability_cfg = cfg.get("availability") or {}
        tz = ZoneInfo(str(availability_cfg.get("timezone") or "UTC"))
        now = datetime.now(tz)
        modes = _requested_modes(args.mode, cfg, now)
        tables_by_mode = None if args.mode is not None else _due_tables(cfg, now)
        return run(
            cfg,
            modes=modes,
            now=now,
            dry_run=args.dry_run,
            logger=logger,
            tables_by_mode=tables_by_mode,
        )
    except Exception as exc:
        logging.getLogger("availability").exception("Availability checker failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
