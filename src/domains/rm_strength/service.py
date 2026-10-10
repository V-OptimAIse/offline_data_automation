from __future__ import annotations

import re
from datetime import date, datetime, time
from numbers import Real
from pathlib import Path
from typing import Any

import pandas as pd

from core.logging import log_file_read
from infrastructure.database_targets import (
    DatabaseTarget,
    write_to_database_targets,
)
from infrastructure.neon_client import NeonClient

PROPERTY_COLS = [f"property_{number}" for number in range(1, 5)]
OUTPUT_COLS = ["date_time", "material_code", *PROPERTY_COLS]


class RMStrengthService:
    """Read the two profile-1 raw-material strength workbooks."""

    def __init__(self, logger, neon_cfg: dict | None = None, write_to_neon: bool = False):
        self.logger = logger
        self.neon_cfg = neon_cfg
        self.write_to_neon = write_to_neon

    @staticmethod
    def _excel_column_index(column: str) -> int:
        value = str(column).strip().upper()
        if not re.fullmatch(r"[A-Z]+", value):
            raise ValueError(f"Invalid Excel column: {column!r}")

        index = 0
        for character in value:
            index = index * 26 + ord(character) - ord("A") + 1
        return index - 1

    @staticmethod
    def _header_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())

    @staticmethod
    def _date_series(values: pd.Series) -> pd.Series:
        return pd.to_datetime(
            values,
            errors="coerce",
            dayfirst=True,
            format="mixed",
        )

    def _read_selected_columns(
        self,
        xls: pd.ExcelFile,
        sheet_name: str,
        source_cfg: dict[str, Any],
    ) -> pd.DataFrame:
        fields = source_cfg.get("fields") or {}
        columns = [source_cfg["date_column"]]
        if source_cfg.get("time_column"):
            columns.append(source_cfg["time_column"])
        columns.extend(fields)
        columns = sorted(
            dict.fromkeys(str(column).strip().upper() for column in columns),
            key=self._excel_column_index,
        )

        log_file_read(self.logger, xls.io, domain="RM_STRENGTH", sheet=sheet_name)
        data = pd.read_excel(
            xls,
            sheet_name=sheet_name,
            header=int(source_cfg.get("header_row", 1)) - 1,
            usecols=[self._excel_column_index(column) for column in columns],
        )
        observed_headers = dict(zip(columns, data.columns))
        data.columns = columns

        expected_headers = {
            str(source_cfg["date_column"]).upper(): source_cfg.get("date_name"),
            str(source_cfg.get("time_column", "")).upper(): source_cfg.get("time_name"),
            **{
                str(column).upper(): field_cfg.get("name")
                for column, field_cfg in fields.items()
            },
        }
        mismatches = [
            f"{column} expected {expected!r}, found {observed_headers.get(column)!r}"
            for column, expected in expected_headers.items()
            if column
            and expected
            and self._header_key(observed_headers.get(column))
            != self._header_key(expected)
        ]
        if mismatches:
            raise ValueError(
                f"RM strength layout mismatch in sheet {sheet_name!r}: "
                + "; ".join(mismatches)
            )
        return data

    @staticmethod
    def _clock_delta(value: Any) -> pd.Timedelta | pd.NaT:
        """Convert the source's HH.MM clock notation (1.40 = 01:40)."""
        if value is None:
            return pd.NaT
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            return pd.Timedelta(
                hours=value.hour,
                minutes=value.minute,
                seconds=value.second,
            )
        if isinstance(value, time):
            return pd.Timedelta(
                hours=value.hour,
                minutes=value.minute,
                seconds=value.second,
            )
        try:
            if pd.isna(value):
                return pd.NaT
        except (TypeError, ValueError):
            return pd.NaT

        if isinstance(value, Real) and not isinstance(value, bool):
            numeric_value = float(value)
            hours = int(numeric_value)
            minutes = int(round((numeric_value - hours) * 100))
            seconds = 0
        else:
            text = str(value).strip()
            colon_match = re.fullmatch(
                r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text
            )
            if colon_match:
                hours, minutes, seconds = (
                    int(part) if part is not None else 0
                    for part in colon_match.groups()
                )
            else:
                decimal_match = re.fullmatch(r"(\d{1,2})(?:\.(\d+))?", text)
                if not decimal_match:
                    return pd.NaT
                hours = int(decimal_match.group(1))
                fraction = decimal_match.group(2) or "00"
                minutes = int(fraction.ljust(2, "0")[:2])
                seconds = 0

        if hours > 23 or minutes > 59 or seconds > 59:
            return pd.NaT
        return pd.Timedelta(hours=hours, minutes=minutes, seconds=seconds)

    def _raw_output(
        self,
        data: pd.DataFrame,
        source_cfg: dict[str, Any],
        date_time: pd.Series,
        requested_dates: set[date],
    ) -> pd.DataFrame:
        out = pd.DataFrame(index=data.index)
        out["date_time"] = date_time
        out["material_code"] = str(source_cfg["material_code"]).strip()
        for property_column in PROPERTY_COLS:
            out[property_column] = pd.NA
        for source_column, field_cfg in (source_cfg.get("fields") or {}).items():
            out[field_cfg["target"]] = pd.to_numeric(
                data[str(source_column).upper()],
                errors="coerce",
            )

        out = out.dropna(subset=["date_time"])
        out = out[out["date_time"].dt.date.isin(requested_dates)]
        out = out.dropna(subset=PROPERTY_COLS, how="all")
        return out[OUTPUT_COLS]

    def _read_coke(
        self,
        file_path: str,
        source_cfg: dict[str, Any],
        requested_dates: set[date],
    ) -> pd.DataFrame:
        with pd.ExcelFile(file_path) as xls:
            sheet_name = str(source_cfg.get("sheet_name", "COKE"))
            if sheet_name not in xls.sheet_names:
                self.logger.warning(f"RM_STRENGTH coke sheet missing: {sheet_name!r}")
                return pd.DataFrame(columns=OUTPUT_COLS)
            data = self._read_selected_columns(xls, sheet_name, source_cfg)

        dates = self._date_series(
            data[str(source_cfg["date_column"]).upper()]
        ).dt.normalize()
        default_time = self._clock_delta(source_cfg.get("default_time", "00:00"))
        if pd.isna(default_time):
            raise ValueError("RM strength coke default_time must be a valid clock time")
        return self._raw_output(data, source_cfg, dates + default_time, requested_dates)

    def _read_sinter(
        self,
        file_path: str,
        source_cfg: dict[str, Any],
        requested_dates: set[date],
    ) -> pd.DataFrame:
        parts: list[pd.DataFrame] = []
        with pd.ExcelFile(file_path) as xls:
            sheet_map = {sheet.strip().upper(): sheet for sheet in xls.sheet_names}
            sheet_format = str(source_cfg.get("sheet_name_format", "%b-%y"))
            requested_sheets = {
                requested_date.strftime(sheet_format).upper()
                for requested_date in requested_dates
            }

            for requested_sheet in sorted(requested_sheets):
                sheet_name = sheet_map.get(requested_sheet)
                if sheet_name is None:
                    self.logger.warning(
                        f"RM_STRENGTH sinter sheet missing: {requested_sheet!r}"
                    )
                    continue

                data = self._read_selected_columns(xls, sheet_name, source_cfg)
                dates = self._date_series(
                    data[str(source_cfg["date_column"]).upper()]
                ).dt.normalize()
                time_column = str(source_cfg["time_column"]).upper()
                clock_deltas = data[time_column].map(self._clock_delta)
                part = self._raw_output(
                    data,
                    source_cfg,
                    dates + pd.to_timedelta(clock_deltas),
                    requested_dates,
                )
                if not part.empty:
                    parts.append(part)

        if not parts:
            return pd.DataFrame(columns=OUTPUT_COLS)
        return pd.concat(parts, ignore_index=True)

    def _push_to_database_targets(self, df: pd.DataFrame, setting_cfg: dict) -> None:
        strength_cfg = setting_cfg.get("rm_strength", {})
        neon_cfg = strength_cfg.get("neon", {})
        table_name = (
            f"{neon_cfg.get('schema', 'offline_feed')}."
            f"{neon_cfg.get('table', 'raw_material_strength_analysis')}"
        )
        conflict_cols = neon_cfg.get("conflict_cols", ["material_code", "date_time"])
        master_cfg = neon_cfg.get("material_master", {})

        def writer(client: NeonClient, target: DatabaseTarget) -> int:
            target_df = df.copy()
            material_codes = client.fetch_material_codes(
                schema=master_cfg.get("schema", "plant_master"),
                table=master_cfg.get("table", "material_property_mapping"),
                code_column=master_cfg.get("code_column", "material_code"),
                active_column=master_cfg.get("active_column"),
            )
            if material_codes:
                before = len(target_df)
                target_df = target_df[
                    target_df["material_code"].str.lower().isin(
                        {code.lower() for code in material_codes}
                    )
                ]
                if skipped := before - len(target_df):
                    self.logger.warning(
                        f"{target.label}: skipped {skipped} RM_STRENGTH rows "
                        "with unknown material_code"
                    )

            rows = client.insert_dataframe(
                df=target_df,
                table_name=table_name,
                conflict_cols=conflict_cols,
                upsert_mode=neon_cfg.get("upsert_mode", "update_insert"),
                null_non_positive_values=False,
            )
            self.logger.info(f"{target.label} {table_name}: {rows} rows synced")
            return rows

        db_cfg = dict(setting_cfg)
        if self.neon_cfg:
            db_cfg["neon_developer"] = self.neon_cfg
        write_to_database_targets(db_cfg, self.logger, "RM_STRENGTH", writer)

    def process(
        self,
        coke_file: str | None,
        sinter_file: str | None,
        setting_cfg: dict,
        run_dates: list[str],
    ) -> pd.DataFrame | None:
        strength_cfg = setting_cfg.get("rm_strength", {})
        source_cfgs = strength_cfg.get("sources", {})
        run_format = strength_cfg.get("run_date_format", "%d-%b-%Y")
        requested_dates = {
            datetime.strptime(run_date, run_format).date() for run_date in run_dates
        }

        parts: list[pd.DataFrame] = []
        if coke_file:
            coke = self._read_coke(coke_file, source_cfgs["coke"], requested_dates)
            if not coke.empty:
                parts.append(coke)
                self.logger.info(f"RM_STRENGTH coke: {len(coke)} raw row(s)")
        if sinter_file:
            sinter = self._read_sinter(
                sinter_file,
                source_cfgs["sinter"],
                requested_dates,
            )
            if not sinter.empty:
                parts.append(sinter)
                self.logger.info(f"RM_STRENGTH sinter: {len(sinter)} raw row(s)")

        if not parts:
            self.logger.warning("No RM_STRENGTH data found for requested dates")
            return None

        combined = pd.concat(parts, ignore_index=True)
        key_columns = ["date_time", "material_code"]
        duplicate_count = int(combined.duplicated(key_columns, keep="last").sum())
        if duplicate_count:
            self.logger.warning(
                f"RM_STRENGTH: {duplicate_count} duplicate database-key row(s); "
                "raw output keeps every observation and DB upsert keeps the last per key"
            )
        combined = combined.sort_values(key_columns, kind="stable").reset_index(drop=True)

        output_cfg = strength_cfg.get("output", {})
        output_dir = Path(output_cfg.get("dir", "output/rm_strength"))
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / output_cfg.get(
            "filename", "combined_rm_strength_data.xlsx"
        )
        combined.to_excel(output_path, index=False)
        self.logger.info(f"RM strength output written -> {output_path}")

        if self.write_to_neon:
            try:
                self._push_to_database_targets(combined, setting_cfg)
            except Exception as exc:
                self.logger.error(f"Failed to write RM_STRENGTH data to DB targets: {exc}")

        return combined
