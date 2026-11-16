from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from infrastructure.database_targets import DatabaseTarget
from infrastructure.neon_client import NeonClient


Status = Literal["OK", "MISSING", "ERROR"]
_QUALIFIED_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)


@dataclass(frozen=True)
class AvailabilityResult:
    mode: str
    target_label: str
    table: str
    window_start: datetime
    window_end: datetime
    row_count: int | None
    latest_date_time: datetime | None
    status: Status
    error: str | None = None
    check_label: str | None = None
    window_hours: int | None = None


@dataclass(frozen=True)
class CheckSpec:
    mode: str
    table: str
    label: str
    window_hours: int
    due_hours: str | tuple[int, ...]


def checks_due(cfg: dict[str, Any], now: datetime) -> dict[str, list[str]]:
    """Return the table names due at ``now``, grouped by public mode."""
    availability_cfg = cfg.get("availability") or {}
    local_now = _local_now(now, availability_cfg)
    due: dict[str, list[str]] = {}
    for spec in _configured_check_specs(availability_cfg):
        if spec.due_hours == "all" or local_now.hour in spec.due_hours:
            due.setdefault(spec.mode, []).append(spec.table)
    return due


def modes_due(cfg: dict[str, Any], now: datetime) -> list[str]:
    """Return configured modes due at ``now`` in the availability timezone."""
    return list(checks_due(cfg, now))


def check_availability(
    cfg: dict[str, Any],
    targets: Iterable[DatabaseTarget],
    modes: Sequence[str],
    now: datetime,
    *,
    tables_by_mode: Mapping[str, Sequence[str]] | None = None,
    client_factory: Callable[[dict[str, Any]], NeonClient] | None = None,
) -> list[AvailabilityResult]:
    """Check all requested modes in every target and return structured results."""
    availability_cfg = cfg.get("availability") or {}
    client_factory = client_factory or NeonClient
    local_now = _local_now(now, availability_cfg)
    selected_modes = set(modes)
    selected_tables = {
        mode: set(tables)
        for mode, tables in (tables_by_mode or {}).items()
    }
    specs = [
        spec
        for spec in _configured_check_specs(availability_cfg)
        if spec.mode in selected_modes
        and (
            tables_by_mode is None
            or spec.table in selected_tables.get(spec.mode, set())
        )
    ]
    windows = {
        (spec.mode, spec.table): _window(local_now, spec.window_hours)
        for spec in specs
    }
    results: list[AvailabilityResult] = []

    for target in targets:
        client = None
        try:
            client = client_factory(target.config)
        except Exception as exc:
            for spec in specs:
                start_utc, end_utc = windows[(spec.mode, spec.table)]
                results.append(
                    _error_result(
                        spec.mode,
                        target.label,
                        spec.table,
                        start_utc,
                        end_utc,
                        exc,
                        check_label=spec.label,
                        window_hours=spec.window_hours,
                    )
                )
            continue

        try:
            for spec in specs:
                start_utc, end_utc = windows[(spec.mode, spec.table)]
                try:
                    row_count, latest = _query_table(
                        client, spec.table, start_utc, end_utc
                    )
                    results.append(
                        AvailabilityResult(
                            mode=spec.mode,
                            target_label=target.label,
                            table=spec.table,
                            window_start=start_utc,
                            window_end=end_utc,
                            row_count=row_count,
                            latest_date_time=latest,
                            status="OK" if row_count > 0 else "MISSING",
                            check_label=spec.label,
                            window_hours=spec.window_hours,
                        )
                    )
                except Exception as exc:
                    results.append(
                        _error_result(
                            spec.mode,
                            target.label,
                            spec.table,
                            start_utc,
                            end_utc,
                            exc,
                            check_label=spec.label,
                            window_hours=spec.window_hours,
                        )
                    )
        finally:
            try:
                client.close()
            except Exception:
                # The checks have already completed; a failed close must not
                # replace their availability results.
                pass

    return results


def _local_now(now: datetime, availability_cfg: dict[str, Any]) -> datetime:
    tz = ZoneInfo(str(availability_cfg.get("timezone") or "UTC"))
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _integer_hours(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {int(hour) for hour in value}


def _configured_check_specs(availability_cfg: dict[str, Any]) -> list[CheckSpec]:
    specs: list[CheckSpec] = []
    for mode, check_cfg in (availability_cfg.get("checks") or {}).items():
        for raw_table in check_cfg.get("tables") or []:
            if isinstance(raw_table, str):
                table_cfg = check_cfg
                table = raw_table
            else:
                table_cfg = {**check_cfg, **raw_table}
                table = str(raw_table.get("table") or "")
            raw_due_hours = table_cfg.get("due_hours")
            due_hours: str | tuple[int, ...]
            if raw_due_hours == "all":
                due_hours = "all"
            else:
                due_hours = tuple(sorted(_integer_hours(raw_due_hours)))
            specs.append(
                CheckSpec(
                    mode=mode,
                    table=table,
                    label=str(table_cfg.get("label") or mode.replace("_", " ").title()),
                    window_hours=int(table_cfg["window_hours"]),
                    due_hours=due_hours,
                )
            )
    return specs


def _window(local_now: datetime, window_hours: int) -> tuple[datetime, datetime]:
    end_utc = local_now.astimezone(timezone.utc)
    floor_hour = local_now.replace(minute=0, second=0, microsecond=0)
    start_local = floor_hour - timedelta(hours=window_hours)
    return start_local.astimezone(timezone.utc), end_utc


def _quote_table(table: str) -> str:
    table = str(table).strip()
    if not _QUALIFIED_IDENTIFIER.fullmatch(table):
        raise ValueError(f"Invalid configured table name: {table!r}")
    return ".".join(f'"{part}"' for part in table.split("."))


def _query_table(
    client: NeonClient,
    table: str,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[int, datetime | None]:
    table_sql = _quote_table(table)
    query = (
        "SELECT "
        f"(SELECT COUNT(*) FROM {table_sql} "
        "WHERE date_time >= %s AND date_time <= %s), "
        f"(SELECT MAX(date_time) FROM {table_sql} WHERE date_time <= %s)"
    )
    with client.conn.cursor() as cursor:
        cursor.execute(query, (start_utc, end_utc, end_utc))
        row_count, latest = cursor.fetchone()
    return int(row_count), latest


def _error_result(
    mode: str,
    target_label: str,
    table: str,
    start_utc: datetime,
    end_utc: datetime,
    exc: Exception,
    *,
    check_label: str,
    window_hours: int,
) -> AvailabilityResult:
    return AvailabilityResult(
        mode=mode,
        target_label=target_label,
        table=table,
        window_start=start_utc,
        window_end=end_utc,
        row_count=None,
        latest_date_time=None,
        status="ERROR",
        error=str(exc),
        check_label=check_label,
        window_hours=window_hours,
    )
