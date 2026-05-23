# src/domains/dpr/service.py

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Tuple

import pandas as pd

from domains.dpr.reader import DPRReader
from domains.dpr.config_updater import DPRConfigUpdater
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient
output_dir = "output/dpr"

@dataclass
class DPRService:
    logger: any

    def __post_init__(self):
        self.reader = DPRReader(self.logger)
        self.updater = DPRConfigUpdater(self.logger)

    def _to_influx_frame(self, df: pd.DataFrame, dpr_cfg: Dict[str, Any]) -> pd.DataFrame:
        influx_fields = dpr_cfg.get("dpr_fields", {})
        source_to_neon = dpr_cfg.get("fields", {})
        rename = {}
        for source, influx_col in influx_fields.items():
            neon_col = source_to_neon.get(source)
            if neon_col in df.columns:
                rename[neon_col] = influx_col
            elif influx_col in df.columns:
                rename[influx_col] = influx_col
            elif source in df.columns:
                rename[source] = influx_col

        if not rename:
            return pd.DataFrame()

        out = df[list(dict.fromkeys(rename))].rename(columns=rename)
        return out.loc[:, ~out.columns.duplicated()].copy()

    def _write_to_influx(
        self,
        df: pd.DataFrame,
        dpr_cfg: Dict[str, Any],
        setting_cfg: Dict[str, Any],
    ) -> None:
        if not influx_write_enabled(setting_cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping DPR Influx push")
            return

        influx_cfg = dict(setting_cfg.get("influxdb") or {})
        token = os.getenv("INFLUX_TOKEN")
        if token:
            influx_cfg["token"] = token.strip().strip("\"'")

        required = ("url", "token", "org", "bucket")
        if not all(influx_cfg.get(k) for k in required):
            self.logger.warning("InfluxDB config missing or incomplete; skipping DPR Influx push")
            return

        dpr_influx = dpr_cfg.get("influx", {})
        influx_cfg["bucket"] = dpr_influx.get("bucket", influx_cfg["bucket"])
        measurement = dpr_influx.get("measurement", "dpr")
        influx_df = self._to_influx_frame(df, dpr_cfg)

        if influx_df.empty or "date" not in influx_df.columns:
            self.logger.warning("DPR Influx fields/date missing; skipping Influx push")
            return

        client = InfluxClient(influx_cfg)
        try:
            client.write_dataframe(df=influx_df, measurement=measurement)
            self.logger.info(f"DPR pushed to InfluxDB measurement: {measurement}")
        except Exception:
            self.logger.exception("DPR InfluxDB push failed")
            raise
        finally:
            client.close()

    def process(
        self,
        dpr_file: str,
        setting_cfg: Dict[str, Any],
        run_dates: List[str],
    ) -> pd.DataFrame:

        dpr_cfg = setting_cfg["dpr"]
        field_mapping = dpr_cfg.get("fields", {})

        output_cfg = dpr_cfg.get("output", {})
        output_dir = output_cfg.get("dir", "output/dpr")
        output_filename = output_cfg.get("filename", "combined_dpr_data.xlsx")

        run_date_fmt = dpr_cfg.get("run_date_format", "%d-%b-%Y")
        required_cols = [v for v in field_mapping.values() if v != "date_time"]

        updated_months: set[Tuple[int, int]] = set()
        all_frames: List[pd.DataFrame] = []

        for run_date in run_dates:
            self.logger.info(f"Processing DPR for {run_date}")
            run_dt = datetime.strptime(run_date, run_date_fmt)
            month_key = (run_dt.year, run_dt.month)

            if month_key not in updated_months:
                dpr_cfg = self.updater.update_rows_in_config(dpr_file, dpr_cfg, run_date)
                updated_months.add(month_key)

            df = self.reader.read_for_date(dpr_file, dpr_cfg, run_date)

            if df is None or df.empty:
                self.logger.warning(f"No DPR data for {run_date}")
                continue

            df = df.rename(columns=field_mapping)

            if df.columns.duplicated().any():
                self.logger.warning(
                    f"Duplicate columns for {run_date} - keeping first occurrence"
                )
                df = df.loc[:, ~df.columns.duplicated()]

            if "date_time" not in df.columns:
                raise ValueError(f"Missing 'date_time' column after rename for {run_date}")

            for col in required_cols:
                if col not in df.columns:
                    df[col] = None
                    self.logger.warning(
                        f"'{col}' missing for {run_date} -> filled with NULL"
                    )

            all_frames.append(df[["date_time"] + required_cols])

        if not all_frames:
            self.logger.warning("No DPR data produced")
            return pd.DataFrame()

        combined = pd.concat(all_frames, ignore_index=True)
        for col in combined.columns:
            if col != "date_time":
                combined[col] = pd.to_numeric(combined[col], errors="coerce")

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, output_filename)
        combined.to_excel(out_path, index=False)
        self.logger.info(f"DPR output written -> {out_path}")

        dpr_neon = dpr_cfg.get("neon", {})
        table = dpr_neon.get("table", "dpr_data")
        schema = dpr_neon.get("schema")
        if schema and "." not in table:
            table = f"{schema}.{table}"

        conflict_cols = dpr_neon.get("conflict_cols", ["date_time"])
        upsert_mode = dpr_neon.get("upsert_mode", "update_insert")

        def writer(neon: NeonClient, target: DatabaseTarget) -> int:
            rows = neon.insert_dataframe(
                df=combined,
                table_name=table,
                conflict_cols=conflict_cols,
                upsert_mode=upsert_mode,
            )
            self.logger.info(
                f"DPR pushed to {target.label} table {table}: {rows} rows upserted"
            )
            return rows

        write_to_database_targets(setting_cfg, self.logger, "DPR", writer)
        self._write_to_influx(combined, dpr_cfg, setting_cfg)

        return combined
