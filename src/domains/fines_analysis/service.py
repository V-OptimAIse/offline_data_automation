from __future__ import annotations

import os
from datetime import datetime
from typing import List

import pandas as pd

from domains.fines_analysis.reader import FinesAnalysisReader
from domains.fines_analysis.transformer import FinesAnalysisTransformer
from infrastructure.database_targets import DatabaseTarget, write_to_database_targets
from infrastructure.neon_client import NeonClient


class FinesAnalysisService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = FinesAnalysisReader(logger)
        self.transformer = FinesAnalysisTransformer(logger)

    def process(
        self,
        rm_file: str,
        setting_cfg: dict,
        run_dates: List[str],
    ) -> pd.DataFrame:
        fines_cfg = setting_cfg["fines_analysis"]
        run_date_fmt = fines_cfg.get("run_date_format", "%d-%b-%Y")
        date_list = [datetime.strptime(d, run_date_fmt).date() for d in run_dates]

        shifts_cfg = fines_cfg.get("shifts", {})
        shift_time = shifts_cfg.get("time", {"A": "07:00", "B": "15:00", "C": "23:00"})
        invalid_markers = fines_cfg.get("invalid_markers", [])

        output_cfg = fines_cfg.get("output", {})
        output_dir = output_cfg.get("dir", "output/fines_analysis")
        output_filename = output_cfg.get("filename", "material_fines_analysis.xlsx")

        self.logger.info("Fines analysis processing started")

        frames = self.reader.read(rm_file, fines_cfg["sheet_config"])
        parts = []

        for df, sheet_cfg, sheet in frames:
            self.logger.info(f"-> {sheet}")
            sheet_cfg = {**sheet_cfg, "invalid_markers": invalid_markers}
            part = self.transformer.transform(df, sheet_cfg, date_list, shift_time)
            if part.empty:
                self.logger.warning(f"   SKIPPED: {sheet} (no fines data)")
                continue

            parts.append(part)
            self.logger.info(f"   OK: {sheet} ({len(part)} rows)")

        if not parts:
            self.logger.error("No fines analysis data produced")
            return pd.DataFrame()

        combined = pd.concat(parts, ignore_index=True)
        ordered_cols = ["date_time", "material_code"] + [
            col for col in self.transformer.OUTPUT_SIZE_COLUMNS if col in combined.columns
        ]
        combined = combined.reindex(columns=ordered_cols)
        combined = combined.sort_values(["date_time", "material_code"]).reset_index(drop=True)

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, output_filename)
        combined.to_excel(out_path, index=False)
        self.logger.info(f"Fines analysis output written -> {out_path}")

        self._push_to_database_targets(combined, setting_cfg, fines_cfg)
        self.logger.info("Fines analysis processing completed successfully")

        return combined

    def _push_to_database_targets(
        self,
        df: pd.DataFrame,
        setting_cfg: dict,
        fines_cfg: dict,
    ) -> None:
        fines_neon_cfg = fines_cfg.get("neon", {})
        schema = fines_neon_cfg.get("schema", "offline_feed")
        table = fines_neon_cfg.get("table", "material_fines_analysis")
        table_name = f"{schema}.{table}"
        conflict_cols = fines_neon_cfg.get("conflict_cols", ["material_code", "date_time"])
        upsert_mode = fines_neon_cfg.get("upsert_mode", "update_insert")
        master_cfg = fines_neon_cfg.get("material_master", {})

        def writer(neon_client: NeonClient, target: DatabaseTarget) -> int:
            target_df = df.copy()
            material_codes = neon_client.fetch_material_codes(
                schema=master_cfg.get("schema", "plant_master"),
                table=master_cfg.get("table", "materials"),
                code_column=master_cfg.get("code_column", "material_code"),
                active_column=master_cfg.get("active_column", "is_active"),
            )
            if material_codes:
                before = len(target_df)
                target_df = target_df[
                    target_df["material_code"].str.lower().isin(
                        {code.lower() for code in material_codes}
                    )
                ]
                skipped = before - len(target_df)
                if skipped:
                    self.logger.warning(
                        f"{target.label}: skipped {skipped} fines rows with unknown material_code"
                    )
            else:
                self.logger.warning(
                    f"{target.label}: no material codes loaded from plant_master.materials"
                )

            table_columns = neon_client.fetch_table_columns(schema, {table}).get(table, set())
            if table_columns:
                insert_cols = [col for col in target_df.columns if col in table_columns]
                target_df = target_df[insert_cols]
            else:
                self.logger.warning(
                    f"{target.label}: no column metadata loaded for {table_name}; "
                    "inserting configured columns"
                )

            rows = neon_client.insert_dataframe(
                df=target_df,
                table_name=table_name,
                conflict_cols=conflict_cols,
                upsert_mode=upsert_mode,
            )
            self.logger.info(f"{target.label} {table_name}: {rows} rows synced")
            return rows

        write_to_database_targets(
            setting_cfg,
            self.logger,
            "fines analysis",
            writer,
        )
