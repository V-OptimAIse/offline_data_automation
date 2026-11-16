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
from domains.availability.furnace_state import (
    advance_furnace_state,
    classify_charge_results,
    load_furnace_state,
    mark_shutdown_notified,
    save_furnace_state,
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


def _format_email_datetime(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "No earlier data found"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(tz).strftime("%d-%m-%Y %H:%M IST")


def _email_status(result: AvailabilityResult) -> str:
    if result.status == "OK":
        return f"Available ({result.row_count} rows)"
    if result.status == "MISSING":
        return "No data"
    return f"Check failed ({result.error or 'unknown error'})"


def _alert_message(
    results: Sequence[AvailabilityResult], tz: ZoneInfo
) -> tuple[str, str]:
    grouped: dict[tuple[str, str], list[AvailabilityResult]] = {}
    for result in results:
        grouped.setdefault((result.mode, result.table), []).append(result)
    affected = [
        group
        for group in grouped.values()
        if any(result.status in {"MISSING", "ERROR"} for result in group)
    ]
    labels = list(
        dict.fromkeys(
            (group[0].check_label or group[0].mode.replace("_", " ").title())
            for group in affected
        )
    )
    subject = f"[BF2 Offline Data] Availability alert: {', '.join(labels)}"
    body_lines = [
        "BF2 Offline Data Availability Alert",
        "",
    ]
    for index, group in enumerate(affected):
        first = group[0]
        label = first.check_label or first.mode.replace("_", " ").title()
        hours = first.window_hours
        interval = f"Last {hours} hour" + ("" if hours == 1 else "s")
        body_lines.extend(
            [
                label,
                f"Expected interval: {interval}",
                (
                    "Checked window: "
                    f"{_format_email_datetime(first.window_start, tz)} to "
                    f"{_format_email_datetime(first.window_end, tz)}"
                ),
                "",
                "Database status:",
            ]
        )
        body_lines.extend(
            f"- {result.target_label}: {_email_status(result)}"
            for result in group
        )
        body_lines.extend(["", "Latest available value:"])
        body_lines.extend(
            f"- {result.target_label}: "
            f"{_format_email_datetime(result.latest_date_time, tz)}"
            for result in group
        )
        if index < len(affected) - 1:
            body_lines.extend(["", "----------------------------------------", ""])
    body_lines.extend(
        [
            "",
            "This is an automated alert from Data Availability - Evonith.",
        ]
    )
    return subject, "\n".join(body_lines)


def _shutdown_confirmation_message(
    *,
    now: datetime,
    missing_threshold: int,
    recovery_threshold: int,
    tz: ZoneInfo,
) -> tuple[str, str]:
    subject = "[BF2 Offline Data] Blast furnace shutdown confirmed"
    body = "\n".join(
        [
            "BF2 Blast Furnace Status",
            "",
            "Status: SHUTDOWN CONFIRMED",
            f"Confirmed at: {_format_email_datetime(now, tz)}",
            (
                "Reason: Charge data was unavailable for "
                f"{missing_threshold} consecutive hourly checks."
            ),
            "",
            "Routine availability alerts are now paused for:",
            "- Charge",
            "- Hot Metal",
            "- RM Fuel",
            "- RM Sinter",
            "",
            "Charge monitoring will continue silently every hour.",
            (
                "Normal checks will resume after "
                f"{recovery_threshold} consecutive charge checks with data."
            ),
        ]
    )
    return subject, body


def _log_results(
    results: Sequence[AvailabilityResult],
    *,
    tz: ZoneInfo,
    logger: logging.Logger,
) -> None:
    for result in results:
        log = logger.info if result.status == "OK" else logger.warning
        log(_result_line(result, tz))


def _finish_run(
    cfg: dict[str, Any],
    results: Sequence[AvailabilityResult],
    *,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    availability_cfg = cfg.get("availability") or {}
    tz = ZoneInfo(str(availability_cfg.get("timezone") or "UTC"))
    _log_results(results, tz=tz, logger=logger)

    failures = [result for result in results if result.status in {"MISSING", "ERROR"}]
    if not failures:
        logger.info("All requested data availability checks passed; no mail sent")
        return 0
    if dry_run:
        logger.info("Dry run: alert mail suppressed | failures=%d", len(failures))
        return 0

    subject, body = _alert_message(results, tz)
    send_mail(availability_cfg.get("mail") or {}, subject, body)
    logger.info("Availability alert mail sent | failures=%d", len(failures))
    return 0


def run(
    cfg: dict[str, Any],
    *,
    modes: Sequence[str],
    now: datetime,
    dry_run: bool,
    logger: logging.Logger,
    tables_by_mode: dict[str, list[str]] | None = None,
) -> int:
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
    return _finish_run(cfg, results, dry_run=dry_run, logger=logger)


def run_scheduled(
    cfg: dict[str, Any],
    *,
    now: datetime,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """Run due checks with furnace shutdown detection and recovery gating."""
    availability_cfg = cfg.get("availability") or {}
    due_tables = checks_due(cfg, now)
    charge_tables = due_tables.get("charge") or []
    if not charge_tables:
        raise RuntimeError("Scheduled availability checks require a due charge check")

    targets = iter_database_targets(cfg)
    if not targets:
        raise RuntimeError("No configured PostgreSQL write targets are available to check")

    charge_results = check_availability(
        cfg,
        targets,
        ["charge"],
        now,
        tables_by_mode={"charge": charge_tables},
    )
    observation = classify_charge_results(charge_results)
    state_path = availability_cfg.get(
        "state_path", "output/state/availability_state.json"
    )
    detection_cfg = availability_cfg.get("shutdown_detection") or {}
    missing_threshold = int(detection_cfg.get("missing_charge_checks", 4))
    recovery_threshold = int(detection_cfg.get("recovery_charge_checks", 4))
    previous_state = load_furnace_state(state_path)
    current_state = advance_furnace_state(
        previous_state,
        observation,
        missing_threshold=missing_threshold,
        recovery_threshold=recovery_threshold,
        now=now,
    )

    logger.info(
        "Furnace availability state | previous=%s | current=%s | "
        "charge_observation=%s | missing_streak=%d | recovery_streak=%d | "
        "persisted=%s",
        previous_state.status,
        current_state.status,
        observation,
        current_state.consecutive_missing,
        current_state.consecutive_available,
        not dry_run,
    )

    results = list(charge_results)
    non_charge_tables = {
        mode: tables for mode, tables in due_tables.items() if mode != "charge"
    }
    if current_state.status == "SHUTDOWN":
        if non_charge_tables:
            logger.warning(
                "Furnace shutdown state active; suppressing scheduled checks | modes=%s",
                ",".join(non_charge_tables),
            )
    elif non_charge_tables:
        non_charge_results = check_availability(
            cfg,
            targets,
            list(non_charge_tables),
            now,
            tables_by_mode=non_charge_tables,
        )
        results.extend(non_charge_results)

    if current_state.status == "SHUTDOWN":
        tz = ZoneInfo(str(availability_cfg.get("timezone") or "UTC"))
        _log_results(results, tz=tz, logger=logger)
        if dry_run:
            if not current_state.shutdown_notified:
                logger.info("Dry run: shutdown confirmation mail suppressed")
            logger.info("Dry run: furnace state was not persisted")
            return 0

        save_furnace_state(state_path, current_state)
        if not current_state.shutdown_notified:
            subject, body = _shutdown_confirmation_message(
                now=now,
                missing_threshold=missing_threshold,
                recovery_threshold=recovery_threshold,
                tz=tz,
            )
            send_mail(availability_cfg.get("mail") or {}, subject, body)
            current_state = mark_shutdown_notified(current_state)
            save_furnace_state(state_path, current_state)
            logger.info("Blast furnace shutdown confirmation mail sent")
        else:
            logger.info(
                "Furnace shutdown state active; all routine availability alerts suppressed"
            )
        return 0

    if not dry_run:
        save_furnace_state(state_path, current_state)

    return _finish_run(cfg, results, dry_run=dry_run, logger=logger)


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
        if args.mode is None:
            return run_scheduled(
                cfg,
                now=now,
                dry_run=args.dry_run,
                logger=logger,
            )
        modes = _requested_modes(args.mode, cfg, now)
        return run(
            cfg,
            modes=modes,
            now=now,
            dry_run=args.dry_run,
            logger=logger,
        )
    except Exception as exc:
        logging.getLogger("availability").exception("Availability checker failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
