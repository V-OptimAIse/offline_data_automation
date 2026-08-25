from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from domains.ash.reader import AshReader
from domains.ash.transformer import AshTransformer
from infrastructure.database_targets import DatabaseTarget, write_to_database_targets
from infrastructure.neon_client import NeonClient


class AshService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = AshReader(logger)
        self.transformer = AshTransformer(logger)

    def process(
        self,
        file_paths: str | list[str],
        setting_cfg: dict[str, Any],
        run_dates: list[str],
    ) -> pd.DataFrame:
        ash_cfg = setting_cfg["ash"]
        date_format = ash_cfg.get("run_date_format", "%d-%b-%Y")
        parsed_dates = [
            datetime.strptime(value, date_format).date() for value in run_dates
        ]

        self.logger.info("Ash analysis processing started")
        paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
        raw_frames = [self.reader.read(file_path, ash_cfg) for file_path in paths]
        raw = pd.concat(raw_frames, ignore_index=True) if raw_frames else pd.DataFrame()
        result = self.transformer.transform(raw, ash_cfg, parsed_dates)
        self.logger.info(f"Ash rows prepared: {len(result)}")

        if result.empty:
            self.logger.warning("No ash analysis data found for the requested date(s)")
            return result

        self._write_output(result, ash_cfg)
        self._push_to_database_targets(result, setting_cfg, ash_cfg)
        self.logger.info("Ash analysis processing completed successfully")
        return result

    def _write_output(self, df: pd.DataFrame, ash_cfg: dict[str, Any]) -> None:
        output_cfg = ash_cfg.get("output", {})
        output_dir = Path(output_cfg.get("dir", "output/ash"))
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / output_cfg.get(
            "filename",
            "ash_chemical_analysis.xlsx",
        )
        df.to_excel(output_path, index=False)
        self.logger.info(f"Ash output written -> {output_path}")

    def _push_to_database_targets(
        self,
        df: pd.DataFrame,
        setting_cfg: dict[str, Any],
        ash_cfg: dict[str, Any],
    ) -> None:
        postgres_cfg = ash_cfg.get("postgres", {})
        schema = postgres_cfg.get("schema", "offline_feed")
        table = postgres_cfg.get("table", "ash_chemical_analysis")
        table_name = f"{schema}.{table}"
        conflict_cols = list(
            postgres_cfg.get("conflict_cols", ["material_type", "date"])
        )
        upsert_mode = postgres_cfg.get("upsert_mode", "update_insert")

        def writer(client: NeonClient, target: DatabaseTarget) -> int:
            table_columns = client.fetch_table_columns(schema, {table}).get(table, set())
            if not table_columns:
                raise RuntimeError(
                    f"{target.label}: table not found or has no columns: {table_name}"
                )

            payload_columns = [column for column in df.columns if column in table_columns]
            target_df = df[payload_columns].copy()
            missing_keys = [
                column for column in conflict_cols if column not in target_df.columns
            ]
            if missing_keys:
                raise RuntimeError(
                    f"{target.label}: {table_name} is missing conflict column(s): "
                    f"{missing_keys}"
                )

            if "updated_at" in table_columns:
                target_df["updated_at"] = datetime.now(timezone.utc)

            rows = client.insert_dataframe(
                df=target_df,
                table_name=table_name,
                conflict_cols=conflict_cols,
                upsert_mode=upsert_mode,
                null_non_positive_values=False,
            )
            self.logger.info(f"{target.label} {table_name}: {rows} rows synced")
            return rows

        write_to_database_targets(
            setting_cfg,
            self.logger,
            "ash analysis",
            writer,
        )
