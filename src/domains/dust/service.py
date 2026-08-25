from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from domains.dust.reader import DustReader
from domains.dust.transformer import DustTransformer
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient


INFLUX_REQUIRED_KEYS = ("url", "token", "org", "bucket")


class DustService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = DustReader(logger)
        self.transformer = DustTransformer(logger)

    def process(
        self,
        basic_file: str | None,
        chemical_file: str | None,
        setting_cfg: dict[str, Any],
        run_dates: list[str],
    ) -> dict[str, pd.DataFrame]:
        dust_cfg = setting_cfg["dust"]
        record_columns = dust_cfg["record_columns"]
        date_format = dust_cfg.get("run_date_format", "%d-%b-%Y")
        parsed_dates = [
            datetime.strptime(value, date_format).date() for value in run_dates
        ]
        frames = {
            "basic": pd.DataFrame(),
            "chemical": pd.DataFrame(),
        }

        self.logger.info("Dust analysis processing started")

        if basic_file:
            raw_basic = self.reader.read_basic(
                basic_file,
                dust_cfg["basic"],
                record_columns,
            )
            frames["basic"] = self.transformer.transform(
                raw_basic,
                "basic",
                parsed_dates,
                record_columns,
            )
            self.logger.info(f"Dust basic rows prepared: {len(frames['basic'])}")

        if chemical_file:
            raw_chemical = self.reader.read_chemical(
                chemical_file,
                dust_cfg["chemical"],
                record_columns,
            )
            frames["chemical"] = self.transformer.transform(
                raw_chemical,
                "chemical",
                parsed_dates,
                record_columns,
            )
            self.logger.info(f"Dust chemical rows prepared: {len(frames['chemical'])}")

        non_empty_frames = {
            analysis_type: frame
            for analysis_type, frame in frames.items()
            if not frame.empty
        }
        if not non_empty_frames:
            self.logger.warning("No dust analysis data found for the requested date(s)")
            return frames

        self._write_outputs(non_empty_frames, dust_cfg)
        self._write_to_influx(non_empty_frames, setting_cfg, dust_cfg)
        self._push_to_database_targets(non_empty_frames, setting_cfg, dust_cfg)

        self.logger.info("Dust analysis processing completed successfully")
        return frames

    def _write_outputs(
        self,
        frames: dict[str, pd.DataFrame],
        dust_cfg: dict[str, Any],
    ) -> None:
        output_cfg = dust_cfg.get("output", {})
        output_dir = Path(output_cfg.get("dir", "output/dust"))
        output_dir.mkdir(parents=True, exist_ok=True)

        for analysis_type, frame in frames.items():
            filename = output_cfg.get(
                f"{analysis_type}_filename",
                f"dust_{analysis_type}_analysis.xlsx",
            )
            output_path = output_dir / filename
            frame.to_excel(output_path, index=False)
            self.logger.info(f"Dust {analysis_type} output written -> {output_path}")

    def _write_to_influx(
        self,
        frames: dict[str, pd.DataFrame],
        setting_cfg: dict[str, Any],
        dust_cfg: dict[str, Any],
    ) -> None:
        if not influx_write_enabled(setting_cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping dust Influx push")
            return

        influx_cfg = dict(setting_cfg.get("influxdb") or {})
        token = os.getenv("INFLUX_TOKEN")
        if token:
            influx_cfg["token"] = token.strip().strip("\"'")

        if not all(influx_cfg.get(key) for key in INFLUX_REQUIRED_KEYS):
            self.logger.warning(
                "InfluxDB config missing or incomplete; skipping dust Influx push"
            )
            return

        dust_influx_cfg = dust_cfg.get("influx", {})
        record_columns = dust_cfg["record_columns"]
        date_field = record_columns["date"]
        material_field = record_columns["material"]
        influx_cfg["bucket"] = dust_influx_cfg.get("bucket", influx_cfg["bucket"])
        influx_cfg["timestamp_col"] = "date"
        measurements = dust_influx_cfg.get("measurements", {})

        client = InfluxClient(influx_cfg)
        try:
            for analysis_type, frame in frames.items():
                influx_frame = frame.rename(columns={date_field: "date"}).copy()
                measurement = measurements.get(
                    analysis_type,
                    f"dust_{analysis_type}_analysis",
                )
                rows = client.write_dataframe(
                    df=influx_frame,
                    measurement=measurement,
                    tag_keys=[material_field],
                )
                self.logger.info(
                    f"Dust {analysis_type} pushed to InfluxDB measurement "
                    f"{measurement}: {rows} points"
                )
        except Exception:
            self.logger.exception("Dust InfluxDB push failed")
        finally:
            client.close()

    def _push_to_database_targets(
        self,
        frames: dict[str, pd.DataFrame],
        setting_cfg: dict[str, Any],
        dust_cfg: dict[str, Any],
    ) -> None:
        postgres_cfg = dust_cfg.get("postgres", {})
        record_columns = dust_cfg["record_columns"]
        date_field = record_columns["date"]
        material_field = record_columns["material"]
        schema = postgres_cfg.get("schema", "offline_feed")
        tables = postgres_cfg.get("tables", {})
        conflict_cols = postgres_cfg.get(
            "conflict_cols",
            [material_field, date_field],
        )
        upsert_mode = postgres_cfg.get("upsert_mode", "update_insert")
        master_cfg = postgres_cfg.get("material_master", {})

        def writer(client: NeonClient, target: DatabaseTarget) -> dict[str, int]:
            material_codes = client.fetch_material_codes(
                schema=master_cfg.get("schema", "plant_master"),
                table=master_cfg.get("table", "materials"),
                code_column=master_cfg.get("code_column", "material_code"),
                active_column=master_cfg.get("active_column", "is_active"),
            )
            if not material_codes:
                raise RuntimeError(
                    f"{target.label}: no active material codes loaded from material master"
                )

            table_names = {
                tables[analysis_type]
                for analysis_type in frames
                if analysis_type in tables
            }
            table_columns = client.fetch_table_columns(schema, table_names)
            results: dict[str, int] = {}
            known_codes = {code.casefold() for code in material_codes}

            for analysis_type, frame in frames.items():
                table = tables.get(analysis_type)
                if not table:
                    raise ValueError(
                        f"Dust {analysis_type} PostgreSQL table is not configured"
                    )

                columns = table_columns.get(table, set())
                if not columns:
                    raise RuntimeError(
                        f"{target.label}: table not found or has no columns: {schema}.{table}"
                    )

                target_frame = frame[
                    frame[material_field].str.casefold().isin(known_codes)
                ].copy()
                skipped = len(frame) - len(target_frame)
                if skipped:
                    self.logger.warning(
                        f"{target.label}: skipped {skipped} dust {analysis_type} row(s) "
                        f"with unknown {material_field}"
                    )

                payload_columns = [
                    column for column in target_frame.columns if column in columns
                ]
                target_frame = target_frame[payload_columns]
                if "updated_at" in columns:
                    target_frame["updated_at"] = datetime.now(timezone.utc)

                rows = client.insert_dataframe(
                    df=target_frame,
                    table_name=f"{schema}.{table}",
                    conflict_cols=conflict_cols,
                    upsert_mode=upsert_mode,
                    null_non_positive_values=False,
                )
                results[analysis_type] = rows
                self.logger.info(
                    f"{target.label} {schema}.{table}: {rows} rows synced"
                )

            return results

        write_to_database_targets(
            setting_cfg,
            self.logger,
            "dust analysis",
            writer,
        )
