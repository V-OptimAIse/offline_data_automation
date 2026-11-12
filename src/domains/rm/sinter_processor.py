from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd


BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
SOURCE_MATERIAL_CODES = {"BF1": "sinter_3", "BF2": "sinter_3"}
ACTIVE_SOURCES = tuple(SOURCE_MATERIAL_CODES)
DEFAULT_SHIFT_SLOT = {"BF1": "1", "BF2": "2"}
STATE_VERSION = 3

CHEMISTRY_FIELDS = (
    "fe_t",
    "feo",
    "cao",
    "sio2",
    "al2o3",
    "mgo",
    "mno",
    "p",
    "tio2",
    "na2o",
    "k2o",
    "basicity",
)

CHEMISTRY_HEADERS = {
    "FET": "fe_t",
    "FEO": "feo",
    "CAO": "cao",
    "SIO2": "sio2",
    "AL2O3": "al2o3",
    "MGO": "mgo",
    "MNO": "mno",
    "P": "p",
    "TIO2": "tio2",
    "TALKALI": "na2o",
    "NA2O": "na2o",
    "UNNAMED11": "k2o",
    "K2O": "k2o",
    "BASICITY": "basicity",
}

TIMESTAMP_HEADERS = (
    "RESULTDATETIME",
    "RESULTTIMESTAMP",
    "SAMPLEDATETIME",
    "SAMPLETIMESTAMP",
    "LABDATETIME",
    "LABTIMESTAMP",
    "UPDATEDATETIME",
    "UPDATETIMESTAMP",
    "RESULTTIME",
    "SAMPLETIME",
    "LABTIME",
    "UPDATETIME",
    "TIME",
)

SHIFT_TIME = {
    "A-1": time(7),
    "A-2": time(11),
    "B-1": time(15),
    "B-2": time(19),
    "C-1": time(23),
    "C-2": time(3),
}
PREVIOUS_DAY_SHIFTS = {"C-1"}
INVALID_VALUES = {"", "-", "N/A", "NA", "NAN", "NONE", "NR", "NULL", "STOP"}


@dataclass(frozen=True)
class SinterEvent:
    source: str
    shift_anchor: datetime
    event_time: datetime
    event_time_source: str
    fingerprint: str
    chemistry: dict[str, float | None]


@dataclass(frozen=True)
class SinterBatch:
    rows: pd.DataFrame
    state: dict[str, Any]
    pending_keys: tuple[tuple[str, str], ...]


class RMSinterProcessor:
    """Build restart-safe BF1/BF2 Sinter events at half-shift anchors."""

    def __init__(
        self,
        logger,
        state_path: str | Path,
        now: Callable[[], datetime] | None = None,
    ):
        self.logger = logger
        self.state_path = Path(state_path)
        self._now = now or (lambda: datetime.now(BUSINESS_TZ))

    def prepare(
        self,
        source_frames: dict[str, list[pd.DataFrame]],
        run_dates: list[date],
        source_modified_at: dict[str, datetime] | None = None,
    ) -> SinterBatch:
        state = self._load_state()
        shifts = state.setdefault("shifts", {})
        removed_sources = 0
        for shift_state in shifts.values():
            if not isinstance(shift_state, dict):
                continue
            for source in tuple(shift_state):
                if source not in ACTIVE_SOURCES:
                    del shift_state[source]
                    removed_sources += 1
        if removed_sources:
            self.logger.info(
                f"RM Sinter ignored {removed_sources} inactive source state record(s); "
                f"enabled sources are {', '.join(ACTIVE_SOURCES)}"
            )
        source_modified_at = source_modified_at or {}
        detected_at = self._local_naive(self._now())

        events: list[SinterEvent] = []
        for source in ACTIVE_SOURCES:
            fallback = source_modified_at.get(source)
            fallback_source = (
                "source_modified" if fallback is not None else "first_detection"
            )
            fallback_time = self._local_naive(fallback or detected_at)
            for frame in source_frames.get(source, []):
                events.extend(
                    self._extract_frame_events(
                        frame=frame,
                        source=source,
                        run_dates=set(run_dates),
                        fallback_time=fallback_time,
                        fallback_source=fallback_source,
                    )
                )

        events_by_shift: dict[tuple[str, str], list[SinterEvent]] = {}
        for event in events:
            shift_key = self._format_datetime(event.shift_anchor)
            events_by_shift.setdefault((shift_key, event.source), []).append(event)

        for (shift_key, source), shift_events in events_by_shift.items():
            shift_state = shifts.setdefault(shift_key, {})
            # A reader can return more than one frame for the same sheet. Keep the
            # latest source event, but always store it at the half-shift anchor.
            # The Neon client interprets this naive value as Asia/Kolkata and writes
            # it as UTC.
            event = max(
                enumerate(shift_events),
                key=lambda item: (item[1].event_time, item[0]),
            )[1]
            assigned = event.shift_anchor
            assigned_text = self._format_datetime(assigned)
            current = shift_state.get(source)

            if (
                current is not None
                and current.get("fingerprint") == event.fingerprint
                and current.get("assigned_date_time") == assigned_text
            ):
                continue

            is_correction = current is not None
            shift_state[source] = {
                **self._state_values(event),
                "assigned_date_time": assigned_text,
                "status": "pending",
            }
            action = "correction" if is_correction else "event"
            self.logger.info(
                f"RM Sinter {action} accepted: {source} shift={shift_key} "
                f"assigned={assigned_text} timestamp_source={event.event_time_source}"
            )

        requested_shifts = {
            self._format_datetime(self.shift_anchor(run_date, shift))
            for run_date in run_dates
            for shift in SHIFT_TIME
        }
        pending_keys: list[tuple[str, str]] = []
        rows: list[dict[str, Any]] = []
        for shift_key in sorted(requested_shifts):
            for source in ACTIVE_SOURCES:
                record = shifts.get(shift_key, {}).get(source)
                if record is None or record.get("status") != "pending":
                    continue
                pending_keys.append((shift_key, source))
                rows.append(self._row_from_state(record, source))

        result = pd.DataFrame(
            rows,
            columns=["date_time", *CHEMISTRY_FIELDS, "material_code"],
        )
        if not result.empty:
            duplicate_keys = result.duplicated(
                subset=["material_code", "date_time"], keep=False
            )
            if duplicate_keys.any():
                collisions = result.loc[duplicate_keys, "date_time"].astype(str).tolist()
                raise ValueError(
                    "RM Sinter produced duplicate material/timestamp rows: "
                    f"{collisions}"
                )

        return SinterBatch(
            rows=result,
            state=state,
            pending_keys=tuple(pending_keys),
        )

    def stage(self, batch: SinterBatch) -> None:
        if batch.pending_keys:
            self._write_state(batch.state)

    def commit(self, batch: SinterBatch) -> None:
        if not batch.pending_keys:
            return

        state = deepcopy(batch.state)
        for shift_key, source in batch.pending_keys:
            record = state.get("shifts", {}).get(shift_key, {}).get(source)
            if record is not None:
                record["status"] = "accepted"
        self._write_state(state)

    @staticmethod
    def shift_anchor(run_date: date, shift: str) -> datetime:
        anchor_date = (
            run_date - timedelta(days=1)
            if shift in PREVIOUS_DAY_SHIFTS
            else run_date
        )
        return datetime.combine(anchor_date, SHIFT_TIME[shift])

    def _extract_frame_events(
        self,
        frame: pd.DataFrame,
        source: str,
        run_dates: set[date],
        fallback_time: datetime,
        fallback_source: str,
    ) -> list[SinterEvent]:
        if frame.empty:
            return []

        df = frame.copy()
        normalized_columns = {col: self._normalize_header(col) for col in df.columns}
        by_header = {normalized: col for col, normalized in normalized_columns.items()}
        date_col = by_header.get("DATE")
        shift_col = by_header.get("SHIFT")
        if date_col is None or shift_col is None:
            self.logger.warning(f"RM Sinter {source}: DATE/SHIFT columns missing")
            return []

        chemistry_columns = {
            target: by_header[header]
            for header, target in CHEMISTRY_HEADERS.items()
            if header in by_header
        }
        if not chemistry_columns:
            self.logger.warning(f"RM Sinter {source}: chemistry columns missing")
            return []

        candidates: dict[datetime, tuple[tuple[datetime, int], SinterEvent]] = {}
        for position, (_, row) in enumerate(df.iterrows()):
            run_date = self._parse_date(row.get(date_col))
            shift = self._parse_shift(row.get(shift_col), source)
            if run_date is None or run_date not in run_dates or shift is None:
                continue
            chemistry = {field: None for field in CHEMISTRY_FIELDS}
            for field, column in chemistry_columns.items():
                chemistry[field] = self._numeric_value(row.get(column))
            if not any(value is not None for value in chemistry.values()):
                continue

            anchor = self.shift_anchor(run_date, shift)
            event_time, event_source = self._row_event_time(
                row=row,
                by_header=by_header,
                run_date=run_date,
                shift=shift,
                fallback_time=fallback_time,
                fallback_source=fallback_source,
            )
            event = SinterEvent(
                source=source,
                shift_anchor=anchor,
                event_time=event_time,
                event_time_source=event_source,
                fingerprint=self._fingerprint(chemistry),
                chemistry=chemistry,
            )
            rank = (event_time, position)
            if anchor not in candidates or rank > candidates[anchor][0]:
                candidates[anchor] = (rank, event)

        return [item[1] for item in candidates.values()]

    def _row_event_time(
        self,
        row: pd.Series,
        by_header: dict[str, Any],
        run_date: date,
        shift: str,
        fallback_time: datetime,
        fallback_source: str,
    ) -> tuple[datetime, str]:
        for header in TIMESTAMP_HEADERS:
            column = by_header.get(header)
            if column is None:
                continue
            parsed = self._parse_source_timestamp(row.get(column), run_date, shift)
            if parsed is not None:
                return parsed, f"row:{column}"
        return fallback_time, fallback_source

    def _parse_source_timestamp(
        self,
        value: Any,
        run_date: date,
        shift: str,
    ) -> datetime | None:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return None

        if isinstance(value, pd.Timestamp):
            return self._local_naive(value.to_pydatetime())
        if isinstance(value, datetime):
            return self._local_naive(value)
        if isinstance(value, time):
            return self._combine_clock(run_date, shift, value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if 0 <= float(value) < 1:
                seconds = round(float(value) * 24 * 60 * 60)
                clock = (datetime.min + timedelta(seconds=seconds)).time()
                return self._combine_clock(run_date, shift, clock)

        text = str(value).strip()
        if not text or text.upper() in INVALID_VALUES:
            return None

        if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", text):
            parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
            if not pd.isna(parsed):
                return self._local_naive(parsed.to_pydatetime())

        clock_matches = re.findall(
            r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?::([0-5]\d))?",
            text,
        )
        if clock_matches:
            hour, minute, second = clock_matches[-1]
            return self._combine_clock(
                run_date,
                shift,
                time(int(hour), int(minute), int(second or 0)),
            )

        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
        if pd.isna(parsed):
            return None
        return self._local_naive(parsed.to_pydatetime())

    @staticmethod
    def _combine_clock(run_date: date, shift: str, clock: time) -> datetime:
        event_date = run_date
        if shift.startswith("C") and clock.hour >= 23:
            event_date -= timedelta(days=1)
        return datetime.combine(event_date, clock)

    @classmethod
    def _fingerprint(cls, chemistry: dict[str, float | None]) -> str:
        normalized = {
            field: cls._canonical_number(chemistry.get(field))
            for field in CHEMISTRY_FIELDS
        }
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_number(value: float | None) -> str | None:
        if value is None:
            return None
        try:
            decimal_value = Decimal(str(value)).normalize()
        except InvalidOperation:
            return None
        return format(decimal_value, "f")

    @staticmethod
    def _numeric_value(value: Any) -> float | None:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return None
        text = str(value).strip()
        if text.upper() in INVALID_VALUES:
            return None
        text = text.replace(",", "").rstrip("%").strip()
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _normalize_header(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]+", "", str(value).strip().upper())

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
        return None if pd.isna(parsed) else parsed.date()

    @staticmethod
    def _parse_shift(value: Any, source: str) -> str | None:
        text = str(value).strip().upper()
        match = re.match(r"^([ABC])(?:\s*[-_/]?\s*([12]))?", text)
        if match is None:
            return None
        group, slot = match.groups()
        candidate = f"{group}-{slot or DEFAULT_SHIFT_SLOT[source]}"
        return candidate if candidate in SHIFT_TIME else None

    @staticmethod
    def _local_naive(value: datetime) -> datetime:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(BUSINESS_TZ).tz_localize(None)
        return timestamp.to_pydatetime()

    @classmethod
    def _state_values(cls, event: SinterEvent) -> dict[str, Any]:
        return {
            "fingerprint": event.fingerprint,
            "event_date_time": cls._format_datetime(event.event_time),
            "event_time_source": event.event_time_source,
            "chemistry": event.chemistry,
        }

    @staticmethod
    def _row_from_state(record: dict[str, Any], source: str) -> dict[str, Any]:
        chemistry = record.get("chemistry", {})
        return {
            "date_time": pd.Timestamp(record["assigned_date_time"]),
            **{field: chemistry.get(field) for field in CHEMISTRY_FIELDS},
            "material_code": SOURCE_MATERIAL_CODES[source],
        }

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.isoformat(timespec="microseconds")

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": STATE_VERSION, "shifts": {}}
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read RM Sinter state: {self.state_path}") from exc
        if state.get("version") in {1, 2} and isinstance(
            state.get("shifts"), dict
        ):
            self.logger.info(
                "RM Sinter reset legacy state because shift timestamps or "
                "material-code mapping changed"
            )
            return {"version": STATE_VERSION, "shifts": {}}
        if state.get("version") != STATE_VERSION or not isinstance(
            state.get("shifts"), dict
        ):
            raise RuntimeError(f"Unsupported RM Sinter state format: {self.state_path}")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=self.state_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
