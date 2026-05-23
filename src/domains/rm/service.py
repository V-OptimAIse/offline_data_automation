# src/domains/rm/service.py
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from domains.rm.neon_mapper import RMNeonMapper
from domains.rm.reader import RMReader
from domains.rm.transformer import RMTransformer
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient


DEFAULT_OUTPUT_DIR = "output/rm"
DEFAULT_OUTPUT_FILENAME = "rm_processed_data.xlsx"
RAW_OUTPUT_FILENAME = "rm_combined_raw_1.xlsx"

DEFAULT_SHIFT_PRIORITY = {"C": 0, "A": 1, "B": 2}
DEFAULT_SHIFT_TIME = {"A": "07:00", "B": "15:00", "C": "23:00"}
INFLUX_REQUIRED_KEYS = ("url", "token", "org", "bucket")
NEON_NUMERIC_EXCLUDE = {"date", "date_time", "material_code"}


class RMService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = RMReader(logger)
        self.transformer = RMTransformer(logger)

    def process(
        self,
        rm_file: str,
        setting_cfg: dict[str, Any],
        run_dates: list[str],
    ) -> pd.DataFrame:
        rm_cfg = setting_cfg["rm"]
        output_dir, output_filename = self._output_settings(rm_cfg)
        run_date_list = self._parse_run_dates(run_dates, rm_cfg)

        self.logger.info("RM processing started")

        frames = self.reader.read(rm_file, rm_cfg["sheet_config"])
        parts = self._process_sheets(
            frames=frames,
            run_dates=run_date_list,
            invalid_markers=rm_cfg.get("invalid_markers"),
        )
        if not parts:
            self.logger.error("No RM data produced - exiting")
            return pd.DataFrame()

        combined = self._combine_parts(parts)
        combined = self._rename_for_output(combined, rm_cfg)
        self._write_raw_output(combined, output_dir)

        combined = self._build_final_frame(combined, rm_cfg)
        if combined.empty:
            return combined

        combined = self._select_output_columns(combined, rm_cfg)
        self._write_final_output(combined, output_dir, output_filename)

        self._write_to_influx(combined, setting_cfg, rm_cfg)

        neon_df = self._coerce_numeric_for_neon(combined.copy())
        self._push_to_database_targets(neon_df, setting_cfg, rm_cfg)

        self.logger.info("RM processing completed successfully")
        return neon_df

    def _output_settings(self, rm_cfg: dict[str, Any]) -> tuple[Path, str]:
        output_cfg = rm_cfg.get("output", {})
        output_dir = Path(output_cfg.get("dir", DEFAULT_OUTPUT_DIR))
        output_filename = output_cfg.get("filename", DEFAULT_OUTPUT_FILENAME)
        return output_dir, output_filename

    def _parse_run_dates(
        self,
        run_dates: list[str],
        rm_cfg: dict[str, Any],
    ) -> list[datetime.date]:
        run_date_fmt = rm_cfg.get("run_date_format", "%d-%b-%Y")
        return [datetime.strptime(date, run_date_fmt).date() for date in run_dates]

    def _process_sheets(
        self,
        frames: list[tuple[pd.DataFrame, str, str]],
        run_dates: list[datetime.date],
        invalid_markers: list[str] | None,
    ) -> list[pd.DataFrame]:
        parts = []
        for df, prefix, sheet in frames:
            part = self._process_sheet(df, prefix, sheet, run_dates, invalid_markers)
            if part is not None:
                parts.append(part)
        return parts

    def _process_sheet(
        self,
        df: pd.DataFrame,
        prefix: str,
        sheet: str,
        run_dates: list[datetime.date],
        invalid_markers: list[str] | None,
    ) -> pd.DataFrame | None:
        self.logger.info(f"-> {sheet}")

        df = self.transformer.normalize_columns(df)
        df = self.transformer.filter_by_date_and_shift(df, run_dates, sheet)
        if df is None or df.empty:
            self.logger.warning(f"   SKIPPED: {sheet} (no valid data)")
            return None

        df = self.transformer.filter_invalid_markers(
            df,
            invalid_markers=invalid_markers,
        )
        if df is None or df.empty:
            self.logger.warning(f"   SKIPPED: {sheet} (all rows filtered as invalid)")
            return None

        if "ONLINE/OFFLINE" in df.columns:
            df = self.transformer.split_online_offline_and_merge(df)

        if {"DATE", "SHIFT"}.issubset(df.columns):
            counts = df.groupby(["DATE", "SHIFT"]).size()
            if any(counts > 1):
                df = self.transformer.average_shift_blocks(df)

        df = df.copy()
        df["MERGE_KEY"] = df["DATE"].astype(str) + "_" + df["SHIFT"]
        df = df.rename(
            columns={col: f"{prefix}{col}" for col in df.columns if col != "MERGE_KEY"}
        )

        self.logger.info(f"   OK: {sheet}")
        return df

    def _combine_parts(self, parts: list[pd.DataFrame]) -> pd.DataFrame:
        combined = parts[0]
        for part in parts[1:]:
            combined = combined.merge(
                part,
                on="MERGE_KEY",
                how="outer",
                suffixes=("", "_dup"),
            )
        return combined.loc[:, ~combined.columns.str.endswith("_dup")]

    def _rename_for_output(
        self,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> pd.DataFrame:
        rename_map = rm_cfg.get("rename_fields", {})
        if not rename_map:
            return df

        self.logger.info("RM fields renamed using rm.yaml mapping")
        return df.rename(columns=rename_map)

    def _write_raw_output(self, df: pd.DataFrame, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        df.to_excel(output_dir / RAW_OUTPUT_FILENAME, index=False)

    def _build_final_frame(
        self,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> pd.DataFrame:
        shifts_cfg = rm_cfg.get("shifts", {})
        shift_priority = shifts_cfg.get("priority", DEFAULT_SHIFT_PRIORITY)
        shift_time = shifts_cfg.get("time", DEFAULT_SHIFT_TIME)

        df = df.copy()
        df["SHIFT"] = df["MERGE_KEY"].str.split("_").str[-1]
        df["SHIFT_ORDER"] = df["SHIFT"].map(shift_priority)
        df = df.sort_values("SHIFT_ORDER").reset_index(drop=True)

        date_col = next((col for col in df.columns if col.upper().endswith("_DATE")), None)
        if date_col is None:
            self.logger.error("No *_DATE column found after merge - cannot build datetime")
            return pd.DataFrame()

        df["Date"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.drop(
            columns=[col for col in df.columns if col.upper().endswith("_DATE")],
            errors="ignore",
        )
        df["Date"] = pd.to_datetime(
            df["Date"].dt.strftime("%Y-%m-%d") + " " + df["SHIFT"].map(shift_time)
        )
        df.loc[df["SHIFT"] == "C", "Date"] -= pd.Timedelta(days=1)

        df = df.drop(columns=["SHIFT", "SHIFT_ORDER", "MERGE_KEY"])
        return df.rename(columns={"Date": "date"})

    def _select_output_columns(
        self,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> pd.DataFrame:
        rename_map = rm_cfg.get("rename_fields", {})
        if not rename_map:
            return df

        allowed = [col for col in rename_map.values() if col in df.columns]
        return df[allowed]

    def _write_final_output(
        self,
        df: pd.DataFrame,
        output_dir: Path,
        output_filename: str,
    ) -> None:
        out_path = output_dir / output_filename
        df.to_excel(out_path, index=False)
        self.logger.info(f"RM output written -> {out_path}")

    def _coerce_numeric_for_neon(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in df.columns:
            if col not in NEON_NUMERIC_EXCLUDE:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _source_aliases(self, source_col: str, rm_cfg: dict[str, Any]) -> list[str]:
        aliases = [source_col]
        for sheet_key, sheet_cfg in rm_cfg.get("sheet_config", {}).items():
            col_prefix = sheet_cfg.get("col_prefix")
            if not col_prefix:
                continue

            sheet_prefix = f"{sheet_key}_"
            if source_col.startswith(sheet_prefix):
                aliases.append(f"{col_prefix}{source_col[len(sheet_prefix):]}")

        return list(dict.fromkeys(aliases))

    def _build_influx_rename_map(
        self,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> dict[str, str]:
        influx_fields = rm_cfg.get("influx", {}).get("rename_fields", {})
        source_to_output = rm_cfg.get("rename_fields", {})
        df_cols = set(df.columns)
        rename = {}

        for source_col, influx_col in influx_fields.items():
            for alias in self._source_aliases(source_col, rm_cfg):
                output_col = source_to_output.get(alias)
                if output_col in df_cols:
                    rename[output_col] = influx_col
                    break
                if alias in df_cols:
                    rename[alias] = influx_col
                    break
                if influx_col in df_cols:
                    rename[influx_col] = influx_col
                    break

        return rename

    def _to_influx_frame(
        self,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> pd.DataFrame:
        rename = self._build_influx_rename_map(df, rm_cfg)
        out = df.rename(columns=rename).copy()
        out = out.loc[:, ~out.columns.duplicated()]

        if "Date" in out.columns and "date" not in out.columns:
            out = out.rename(columns={"Date": "date"})
        if "date_time" in out.columns and "date" not in out.columns:
            out = out.rename(columns={"date_time": "date"})

        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce")

        for col in out.columns:
            if col != "date":
                out[col] = pd.to_numeric(out[col], errors="coerce")

        if rename:
            self.logger.info(
                f"RM fields renamed for InfluxDB using rm.yaml mapping ({len(rename)} columns)"
            )

        return out

    def _write_to_influx(
        self,
        df: pd.DataFrame,
        setting_cfg: dict[str, Any],
        rm_cfg: dict[str, Any],
    ) -> None:
        if not influx_write_enabled(setting_cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping RM Influx push")
            return

        influx_cfg = dict(setting_cfg.get("influxdb") or {})
        token = os.getenv("INFLUX_TOKEN")
        if token:
            influx_cfg["token"] = token.strip().strip("\"'")

        if not all(influx_cfg.get(key) for key in INFLUX_REQUIRED_KEYS):
            self.logger.warning("InfluxDB config missing or incomplete; skipping RM Influx push")
            return

        rm_influx = rm_cfg.get("influx", {})
        influx_cfg["bucket"] = rm_influx.get("bucket", influx_cfg["bucket"])
        measurement = rm_influx.get("measurement", "rm_data")
        influx_df = self._to_influx_frame(df, rm_cfg)

        if influx_df.empty or "date" not in influx_df.columns:
            self.logger.warning("RM Influx fields/date missing; skipping Influx push")
            return

        client = InfluxClient(influx_cfg)
        try:
            client.write_dataframe(df=influx_df, measurement=measurement)
            self.logger.info(f"RM pushed to InfluxDB measurement: {measurement}")
        except Exception:
            self.logger.exception("RM InfluxDB push failed")
        finally:
            client.close()

    def write_to_influx(self, df: pd.DataFrame, setting_cfg: dict[str, Any]) -> None:
        rm_cfg = setting_cfg.get("rm", setting_cfg)
        self._write_to_influx(df, setting_cfg, rm_cfg)

    def _push_to_database_targets(
        self,
        df: pd.DataFrame,
        setting_cfg: dict[str, Any],
        rm_cfg: dict[str, Any],
    ) -> None:
        def writer(client: NeonClient, target: DatabaseTarget) -> int:
            rows = self._sync_neon_tables(client, df, rm_cfg)
            self.logger.info(f"RM synced to {target.label}: {rows} rows")
            return rows

        write_to_database_targets(setting_cfg, self.logger, "RM", writer)

    def _sync_neon_tables(
        self,
        neon_client: NeonClient,
        df: pd.DataFrame,
        rm_cfg: dict[str, Any],
    ) -> int:
        rm_neon_cfg = rm_cfg.get("neon", {})
        category_map = rm_neon_cfg.get("category_map", {})
        schema = rm_neon_cfg.get("schema", "offline_feed")
        conflict_cols = rm_neon_cfg.get(
            "conflict_cols",
            ["material_code", "date_time"],
        )
        upsert_mode = rm_neon_cfg.get("upsert_mode", "update_insert")
        master_cfg = rm_neon_cfg.get("material_master", {})

        material_codes = neon_client.fetch_material_codes(
            schema=master_cfg.get("schema", "plant_master"),
            table=master_cfg.get("table", "materials"),
            code_column=master_cfg.get("code_column", "material_code"),
            active_column=master_cfg.get("active_column", "is_active"),
        )
        if not material_codes:
            self.logger.warning("No material codes loaded from plant_master.materials")

        table_names = {
            mapping["table"] for mapping in category_map.values() if "table" in mapping
        }
        table_columns = neon_client.fetch_table_columns(schema, table_names)
        missing_tables = sorted(
            table for table in table_names if not table_columns.get(table)
        )
        if missing_tables:
            raise RuntimeError(
                f"No columns found for RM target tables in schema '{schema}': {missing_tables}"
            )

        mapper = RMNeonMapper(
            material_codes=material_codes,
            category_map=category_map,
            schema=schema,
            table_columns=table_columns,
            logger=self.logger,
        )

        total_rows = 0
        for table_name, table_df in mapper.iter_table_dfs(df):
            try:
                rows = neon_client.insert_dataframe(
                    df=table_df,
                    table_name=table_name,
                    conflict_cols=conflict_cols,
                    upsert_mode=upsert_mode,
                )
                total_rows += rows
                self.logger.info(f"    {table_name}: {rows} rows synced")
            except Exception:
                self.logger.exception(f"    Failed for {table_name}")
                raise

        return total_rows
