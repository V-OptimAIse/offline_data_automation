# src/domains/rm_hm/service.py

import os
import pandas as pd
from datetime import datetime
from core.logging import log_file_read
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient

OUTPUT_DIR = "output/rm_hm"


class RMHMService:
    """
    Handles RM & HM combined sheet processing (e.g. SP-02).
    """

    def __init__(self, logger, neon_cfg: dict | None = None, write_to_neon: bool = False):
        self.logger = logger
        self.neon_cfg = neon_cfg
        self.write_to_neon = write_to_neon

    def _write_to_influx(self, df: pd.DataFrame, setting_cfg: dict) -> None:
        if not influx_write_enabled(setting_cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping RM_HM Influx push")
            return

        influx_cfg = dict(setting_cfg.get("influxdb") or {})
        token = os.getenv("INFLUX_TOKEN")
        if token:
            influx_cfg["token"] = token.strip().strip("\"'")

        if not all(influx_cfg.get(k) for k in ("url", "token", "org", "bucket")):
            self.logger.warning("InfluxDB config missing or incomplete; skipping RM_HM Influx push")
            return

        fields = setting_cfg.get("rm_hm_fields", {})
        cols = list(dict.fromkeys(fields.values()))
        influx_df = df.rename(columns={"date_time": "date"})
        influx_df = influx_df[[c for c in cols if c in influx_df.columns]].copy()

        if influx_df.empty or "date" not in influx_df.columns:
            self.logger.warning("RM_HM Influx fields/date missing; skipping Influx push")
            return

        measurement = setting_cfg.get("rm_hm", {}).get("influx", {}).get("measurement", "rm_hm")
        client = InfluxClient(influx_cfg)
        try:
            client.write_dataframe(df=influx_df, measurement=measurement)
            self.logger.info(f"RM_HM pushed to InfluxDB measurement: {measurement}")
        except Exception:
            self.logger.exception("RM_HM InfluxDB push failed")
        finally:
            client.close()

    def process(
        self,
        rm_hm_file: str,
        setting_cfg: dict,
        run_dates: list[str],
    ) -> pd.DataFrame | None:

        rm_hm_cfg = setting_cfg.get("rm_hm", {})
        field_map = setting_cfg.get("rm_hm_fields", {})

        sheet_name = rm_hm_cfg.get("sheet_name", "SP-02")

        self.logger.info(f"Using RM & HM sheet: {sheet_name}")

        # ----------------------------
        # READ SHEET
        # ----------------------------
        log_file_read(self.logger, rm_hm_file, domain="RM_HM", sheet=sheet_name)
        df = pd.read_excel(
            rm_hm_file,
            sheet_name=sheet_name,
            header=0,
        )

        if df.empty:
            self.logger.warning("RM & HM sheet is empty")
            return None

        # ----------------------------
        # NORMALIZE COLUMNS
        # ----------------------------
        df.columns = (
            df.columns.astype(str)
            .str.strip()
            .str.replace(r"\s+", "_", regex=True)
            .str.replace(r"[^0-9a-zA-Z_]", "", regex=True)
            .str.lower()
        )

        df = df.dropna(how="all")

        # ----------------------------
        # STANDARDIZE DATE COLUMN
        # ----------------------------
        date_col = None
        for col in df.columns:
            if col.lower() in {"date", "dates", "dt"}:
                date_col = col
                break

        if not date_col:
            self.logger.error(f"'date' column not found. Columns: {list(df.columns)}")
            return None

        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        df = df[df["date"].notna()]
        df = df.sort_values("date").reset_index(drop=True)

        # ----------------------------
        # ENSURE KEY COLUMNS EXIST
        # ----------------------------
        target_cols = ["ai", "ti", "rdi", "ri"]
        for col in target_cols:
            if col not in df.columns:
                self.logger.warning(f"Column '{col}' missing - creating empty column")
                df[col] = pd.NA

        # ----------------------------
        # FORWARD FILL
        # ----------------------------
        df[target_cols] = df[target_cols].ffill().infer_objects(copy=False)

        # ----------------------------
        # FILTER BY RUN DATES
        # ----------------------------
        run_dt_list = [
            datetime.strptime(d, "%d-%b-%Y").date()
            for d in run_dates
        ]

        filtered = df[df["date"].dt.date.isin(run_dt_list)].copy()

        if filtered.empty:
            self.logger.warning("No RM & HM data found for requested dates")
            return None

        filtered["date"] = pd.to_datetime(filtered["date"])

        # ----------------------------
        # RENAME + SELECT COLUMNS
        # ----------------------------
        cols_to_keep = []

        if field_map:
            filtered = filtered.rename(columns=field_map)

            cols_to_keep = list(field_map.values())

        # always include date
        if "date" in filtered.columns:
            cols_to_keep.append("date")

        # remove duplicates
        cols_to_keep = list(set(cols_to_keep))

        # keep only existing columns
        cols_to_keep = [c for c in cols_to_keep if c in filtered.columns]

        filtered = filtered[cols_to_keep]

        # ----------------------------
        # REQUIRED FOR NEON (date_time)
        # ----------------------------
        if "date" in filtered.columns:
            filtered = filtered.rename(columns={"date": "date_time"})

        # ----------------------------
        # WRITE EXCEL
        # ----------------------------
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, "combined_rm_hm_data.xlsx")
        filtered.to_excel(out_path, index=False)

        self.logger.info(f"RM & HM output written -> {out_path}")

        # ----------------------------
        # WRITE TO CONFIGURED DB TARGETS
        # ----------------------------
        if self.write_to_neon:
            try:
                db_cfg = dict(setting_cfg)
                if self.neon_cfg:
                    db_cfg["neon_developer"] = self.neon_cfg

                def writer(client: NeonClient, target: DatabaseTarget) -> int:
                    rows = client.insert_dataframe(
                        df=filtered,
                        table_name="offline_feed.raw_material_strength_analysis",
                        conflict_cols=["date_time"],
                        upsert_mode="update_insert",
                    )

                    self.logger.info(
                        f"Inserted {rows} rows to {target.label} "
                        "-> offline_feed.raw_material_strength_analysis"
                    )
                    return rows

                write_to_database_targets(
                    db_cfg,
                    self.logger,
                    "RM_HM",
                    writer,
                )
                self._write_to_influx(filtered, setting_cfg)

            except Exception as e:
                self.logger.error(f"Failed to write RM_HM data to DB targets: {e}")

        return filtered
