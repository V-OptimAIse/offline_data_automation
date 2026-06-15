# src/domains/rm_hm/service.py

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any

import pandas as pd

from core.logging import log_file_read
from infrastructure.database_targets import (
    DatabaseTarget,
    write_to_database_targets,
)
from infrastructure.neon_client import NeonClient

OUTPUT_DIR = "output/rm_hm"
PROPERTY_COLS = [f"property_{i}" for i in range(1, 5)]


class RMHMService:
    """Processes raw-material strength sheets into generic material properties."""

    def __init__(self, logger, neon_cfg: dict | None = None, write_to_neon: bool = False):
        self.logger = logger
        self.neon_cfg = neon_cfg
        self.write_to_neon = write_to_neon

    @staticmethod
    def _key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())

    @staticmethod
    def _date_series(values: pd.Series) -> pd.Series:
        date_like = values.map(
            lambda v: isinstance(v, (pd.Timestamp, datetime, date))
            or bool(re.match(r"^\s*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s*$", str(v)))
            or bool(re.match(r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$", str(v)))
        )
        return pd.to_datetime(values.where(date_like), errors="coerce", dayfirst=True)

    def _sheet_name_map(self, xls: pd.ExcelFile) -> dict[str, str]:
        return {self._key(sheet): sheet for sheet in xls.sheet_names}

    def _header_row(self, raw: pd.DataFrame, fields: dict[str, str]) -> int | None:
        targets = {self._key(name) for name in fields}
        scores = raw.head(25).apply(
            lambda row: sum(self._key(value) in targets for value in row),
            axis=1,
        )
        return int(scores.idxmax()) if int(scores.max()) else None

    def _date_column(self, data: pd.DataFrame, headers: list[Any]) -> int | None:
        for idx, header in enumerate(headers):
            if self._key(header) in {"date", "dates", "dt", "datetime"}:
                return idx

        scores = {
            idx: int(self._date_series(data.iloc[:, idx]).notna().sum())
            for idx in data.columns
        }
        best_idx, best_score = max(scores.items(), key=lambda item: item[1])
        return int(best_idx) if best_score else None

    def _parse_sheet(
        self,
        xls: pd.ExcelFile,
        sheet: str,
        cfg: dict[str, Any],
        run_dates: set[date],
    ) -> pd.DataFrame:
        log_file_read(self.logger, xls.io, domain="RM_HM", sheet=sheet)
        raw = pd.read_excel(xls, sheet_name=sheet, header=None).dropna(how="all")
        if raw.empty:
            self.logger.warning(f"RM_HM {sheet}: sheet is empty")
            return pd.DataFrame()

        fields = cfg.get("fields") or {}
        header_idx = self._header_row(raw, fields)
        if header_idx is None:
            self.logger.warning(f"RM_HM {sheet}: property header row not found")
            return pd.DataFrame()

        headers = raw.loc[header_idx].tolist()
        data = raw.loc[header_idx + 1:].reset_index(drop=True)
        date_idx = self._date_column(data, headers)
        if date_idx is None:
            self.logger.warning(f"RM_HM {sheet}: date column not found")
            return pd.DataFrame()

        header_map = {self._key(header): idx for idx, header in enumerate(headers)}
        out = pd.DataFrame(
            {
                "date_time": self._date_series(data.iloc[:, date_idx]),
                "material_code": str(cfg["material_code"]).strip(),
            }
        )

        for source, target in fields.items():
            idx = header_map.get(self._key(source))
            if idx is None:
                self.logger.warning(f"RM_HM {sheet}: column {source!r} missing")
                out[target] = pd.NA
            else:
                out[target] = pd.to_numeric(data.iloc[:, idx], errors="coerce")

        out = out.dropna(subset=["date_time"]).sort_values("date_time")
        out[PROPERTY_COLS] = out.reindex(columns=PROPERTY_COLS).ffill()
        out = out[out["date_time"].dt.date.isin(run_dates)]
        out = out.dropna(subset=PROPERTY_COLS, how="all")
        return out[["date_time", "material_code", *PROPERTY_COLS]]

    def _push_to_database_targets(self, df: pd.DataFrame, setting_cfg: dict) -> None:
        rm_hm_cfg = setting_cfg.get("rm_hm", {})
        neon_cfg = rm_hm_cfg.get("neon", {})
        table_name = f"{neon_cfg.get('schema', 'offline_feed')}.{neon_cfg.get('table', 'raw_material_strength_analysis')}"
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
                    self.logger.warning(f"{target.label}: skipped {skipped} RM_HM rows with unknown material_code")

            rows = client.insert_dataframe(
                df=target_df,
                table_name=table_name,
                conflict_cols=conflict_cols,
                upsert_mode=neon_cfg.get("upsert_mode", "update_insert"),
            )
            self.logger.info(f"{target.label} {table_name}: {rows} rows synced")
            return rows

        db_cfg = dict(setting_cfg)
        if self.neon_cfg:
            db_cfg["neon_developer"] = self.neon_cfg
        write_to_database_targets(db_cfg, self.logger, "RM_HM", writer)

    def process(
        self,
        rm_hm_file: str,
        setting_cfg: dict,
        run_dates: list[str],
    ) -> pd.DataFrame | None:
        rm_hm_cfg = setting_cfg.get("rm_hm", {})
        sheet_cfgs = rm_hm_cfg.get("sheets", {})
        run_fmt = rm_hm_cfg.get("run_date_format", "%d-%b-%Y")
        requested_dates = {datetime.strptime(d, run_fmt).date() for d in run_dates}

        xls = pd.ExcelFile(rm_hm_file)
        normalized_sheets = self._sheet_name_map(xls)
        self.logger.info(f"Using RM_HM sheets: {', '.join(sheet_cfgs)}")

        parts = []
        for configured_sheet, cfg in sheet_cfgs.items():
            actual_sheet = normalized_sheets.get(self._key(configured_sheet))
            if not actual_sheet:
                self.logger.warning(f"RM_HM sheet missing: {configured_sheet!r}")
                continue
            part = self._parse_sheet(xls, actual_sheet, cfg, requested_dates)
            if not part.empty:
                parts.append(part)
                self.logger.info(f"RM_HM {actual_sheet}: {len(part)} row(s)")

        if not parts:
            self.logger.warning("No RM_HM data found for requested dates")
            return None

        filtered = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates(["date_time", "material_code"], keep="last")
            .sort_values(["date_time", "material_code"])
            .reset_index(drop=True)
        )

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, "combined_rm_hm_data.xlsx")
        filtered.to_excel(out_path, index=False)
        self.logger.info(f"RM & HM output written -> {out_path}")

        if self.write_to_neon:
            try:
                self._push_to_database_targets(filtered, setting_cfg)
            except Exception as exc:
                self.logger.error(f"Failed to write RM_HM data to DB targets: {exc}")

        return filtered
