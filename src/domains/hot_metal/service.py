# src/domains/hot_metal/service.py

import os
import pandas as pd
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient

from domains.hot_metal.reader import HotMetalReader
from domains.hot_metal.config_updater import HotMetalConfigUpdater
import pytz

ist = pytz.timezone("Asia/Kolkata")

OUTPUT_DIR = "output/hot_metal"
HOT_METAL_TABLE = "offline_feed.hot_metal_slag_analysis"
HOT_METAL_PRIMARY_KEY = "cast_no_ladle_spec"
HOT_METAL_TEXT_COLUMNS = ("lab_sample_id", "cast_no_ladle_spec")


class HotMetalService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = HotMetalReader(logger)
        self.updater = HotMetalConfigUpdater(logger)

    def _clean_text_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        for col in HOT_METAL_TEXT_COLUMNS:
            if col not in df.columns:
                continue

            values = df[col].astype("string").str.strip()
            empty_like = values.isna() | values.str.lower().isin(
                {"", "nan", "nat", "none"}
            )
            df[col] = values.mask(empty_like, pd.NA)

        return df

    def _prepare_db_dataframe(self, df: pd.DataFrame, run_date: str) -> pd.DataFrame:
        db_df = df.rename(columns={"date": "date_time"}).copy()

        if HOT_METAL_PRIMARY_KEY not in db_df.columns:
            raise ValueError(
                f"HOT_METAL primary key column missing: {HOT_METAL_PRIMARY_KEY}"
            )

        missing_key = db_df[HOT_METAL_PRIMARY_KEY].isna()
        if missing_key.any():
            self.logger.warning(
                f"HOT_METAL {run_date}: dropping {int(missing_key.sum())} row(s) "
                f"without {HOT_METAL_PRIMARY_KEY}"
            )
            db_df = db_df.loc[~missing_key].copy()

        duplicate_key = db_df.duplicated(subset=[HOT_METAL_PRIMARY_KEY], keep=False)
        if duplicate_key.any():
            duplicate_values = sorted(
                db_df.loc[duplicate_key, HOT_METAL_PRIMARY_KEY]
                .dropna()
                .astype(str)
                .unique()
            )
            self.logger.warning(
                f"HOT_METAL {run_date}: duplicate {HOT_METAL_PRIMARY_KEY} value(s) "
                f"{duplicate_values}; keeping the last row for each key"
            )
            db_df = db_df.drop_duplicates(subset=[HOT_METAL_PRIMARY_KEY], keep="last")

        return db_df

    def _write_to_influx(self, df: pd.DataFrame, setting_cfg: dict, hm_cfg: dict) -> None:
        if not influx_write_enabled(setting_cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping HOT_METAL Influx push")
            return

        influx_cfg = dict(setting_cfg.get("influxdb") or {})
        token = os.getenv("INFLUX_TOKEN")
        if token:
            influx_cfg["token"] = token.strip().strip("\"'")

        if not all(influx_cfg.get(k) for k in ("url", "token", "org", "bucket")):
            self.logger.warning("InfluxDB config missing or incomplete; skipping HOT_METAL Influx push")
            return

        cols = list(dict.fromkeys(hm_cfg.get("hot_metal_fields", {}).values()))
        influx_df = df[[c for c in cols if c in df.columns]].copy()
        if influx_df.empty or "date" not in influx_df.columns:
            self.logger.warning("HOT_METAL Influx fields/date missing; skipping Influx push")
            return

        measurement = hm_cfg.get("influx", {}).get("measurement", "hot_metal")
        client = InfluxClient(influx_cfg)
        try:
            client.write_dataframe(
                df=influx_df,
                measurement=measurement,
                tag_keys=["lab_sample_id", "cast_no_ladle_spec"],
            )
            self.logger.info(f"HOT_METAL pushed to InfluxDB measurement: {measurement}")
        except Exception:
            self.logger.exception("HOT_METAL InfluxDB push failed")
        finally:
            client.close()

    def process(self, hm_file: str, setting_cfg: dict, run_dates):
        hm_cfg = setting_cfg["hot_metal"]
        field_map = hm_cfg.get("hot_metal_fields", {})

        for run_date in run_dates:
            self.logger.info(f"Processing HOT_METAL for {run_date}")

            # Update config
            hm_cfg = self.updater.update_from_excel(hm_file, hm_cfg, run_date)

            # Read data
            df = self.reader.read_for_dates(hm_file, [run_date], hm_cfg)

            if df is None or df.empty:
                self.logger.warning(f"No HOT_METAL data for {run_date}")
                continue

            # Drop raw DATE column BEFORE renaming to avoid duplicate `date`
            if "DATE" in df.columns:
                df = df.drop(columns=["DATE"])

            # Rename fields (DATE -> date happens here safely)
            df = df.rename(columns=field_map)
            df = df.loc[:, ~df.columns.duplicated()]
            allowed_cols = list(field_map.values())
            df = df[[col for col in allowed_cols if col in df.columns]]

            # df["date"] = pd.to_datetime(df["date"])  

            df = self._clean_text_columns(df)

            # Write Excel
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            out_path = os.path.join(OUTPUT_DIR, "combined_hot_data.xlsx")
            df.to_excel(out_path, index=False)
            self.logger.info(f"HOT_METAL output written -> {out_path}")
            df["date"] = pd.to_datetime(df["date"])
            df["date"] = df["date"].dt.tz_localize(ist)
            # --- CLEAN NUMERIC COLUMNS ---
            exclude_cols = ["lab_sample_id", "cast_no_ladle_spec", "date"]

            for col in df.columns:
                if col in exclude_cols:
                    continue

                # Replace junk values
                df[col] = df[col].replace(
                    ["*", "NA", "na", "--", ""],
                    None
                )

                # Convert to numeric safely
                df[col] = pd.to_numeric(df[col], errors="coerce")

            db_df = self._prepare_db_dataframe(df, run_date)

            def writer(neon: NeonClient, target: DatabaseTarget) -> int:
                rows = neon.insert_dataframe(
                    df=db_df,
                    table_name=HOT_METAL_TABLE,
                    conflict_cols=[HOT_METAL_PRIMARY_KEY],
                    upsert_mode="update_insert",
                )
                self.logger.info(
                    f"HOT_METAL {run_date}: {rows} rows synced "
                    f"to {target.label} -> {HOT_METAL_TABLE}"
                )
                return rows

            write_to_database_targets(
                setting_cfg,
                self.logger,
                "HOT_METAL",
                writer,
            )
            self._write_to_influx(df, setting_cfg, hm_cfg)
