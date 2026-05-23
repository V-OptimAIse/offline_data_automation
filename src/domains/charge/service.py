# src/domains/charge/service.py
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import pandas as pd

from .reader import ChargeExcelReader
from .processor import RawChargeProcessor
from .hopper_repository import HopperSnapshotRepository
import yaml
from infrastructure.database_targets import (
    DatabaseTarget,
    iter_database_targets,
    write_to_database_targets,
)
from infrastructure.neon_client import NeonClient


@dataclass
class ChargeServiceConfig:
    output_dir: str
    neon_cfg: dict
    setting_cfg: dict | None = None
    charge_yaml_path: str = "src/config/charge.yaml"
    write_to_neon: bool = False


class ChargeService:
    def __init__(self, cfg: ChargeServiceConfig, logger):
        self.cfg = cfg
        self.logger = logger
        self.reader = ChargeExcelReader(logger=self.logger)
        self.processor = RawChargeProcessor()
        self.charge_cfg = self._load_charge_cfg()
        snapshot_cfg = cfg.neon_cfg
        if cfg.setting_cfg:
            targets = iter_database_targets(cfg.setting_cfg)
            if targets:
                snapshot_cfg = targets[0].config

        self.snapshot_repo = HopperSnapshotRepository(
            snapshot_cfg,
            hopper_cfg=self.charge_cfg.get("hopper_history"),
            material_cfg=self.charge_cfg.get("material_master"),
        )

    def run(self, charge_file: str, run_date_str: str) -> pd.DataFrame:
        target_date = datetime.strptime(run_date_str, "%d-%b-%Y")

        self.logger.info(f"Reading raw charge data for {run_date_str}")

        raw_df = self.reader.read_target_day_raw(
            file_path=charge_file,
            target_date=target_date,
        )

        self.logger.info(f"Raw rows found: {len(raw_df)}")

        snapshots = self.snapshot_repo.fetch_for_day(target_date)
        self.logger.info(f"Snapshots loaded: {len(snapshots)}")
        if not snapshots:
            raise ValueError(f"No hopper snapshots found before end of {run_date_str}")
        self.logger.info(f"First snapshot: {snapshots[0]['ts']}")
        self.logger.info(f"Last snapshot: {snapshots[-1]['ts']}")

        final_df = self.processor.process_wide_with_time(
            raw_df=raw_df,
            snapshots=snapshots
        )
        Path(self.cfg.output_dir).mkdir(parents=True, exist_ok=True)

        out_path = Path(self.cfg.output_dir) / f"raw_charge_data_{target_date:%Y_%m_%d}.xlsx"
        final_df.to_excel(out_path, index=False)

        self.logger.info(f"Raw charge Excel written: {out_path}")

        import_batch_id = str(uuid4())
        db_df = self.processor.to_charge_data_table(
            wide_df=final_df,
            material_column_overrides=self.charge_cfg.get("material_column_overrides"),
            import_batch_id=import_batch_id,
        )

        db_out_path = Path(self.cfg.output_dir) / f"charge_data_table_{target_date:%Y_%m_%d}.xlsx"
        db_df.to_excel(db_out_path, index=False)

        self.logger.info(f"Charge DB-format Excel written: {db_out_path}")

        if self.cfg.write_to_neon:
            target_cfg = self.charge_cfg.get("target", {})
            batch_cfg = self.charge_cfg.get("import_batch", {})
            schema = target_cfg.get("schema", "offline_feed")
            table = target_cfg.get("table", "charge_data")
            table_name = table if "." in table else f"{schema}.{table}"

            def writer(client: NeonClient, target: DatabaseTarget) -> int:
                client.create_import_batch(
                    import_batch_id=import_batch_id,
                    source_type=batch_cfg.get("source_type", "excel"),
                    domain=batch_cfg.get("domain", "charge"),
                    parser_name=batch_cfg.get("parser_name", "charge_report"),
                    source_filename=Path(charge_file).name,
                    source_path=str(Path(charge_file).resolve()),
                    file_sha256=self._file_sha256(charge_file),
                    row_count=len(db_df),
                    metadata={"run_date": run_date_str},
                    schema=batch_cfg.get("schema", "ingest"),
                    table=batch_cfg.get("table", "import_batches"),
                )

                try:
                    inserted = client.insert_dataframe(
                        df=db_df,
                        table_name=table_name,
                        conflict_cols=target_cfg.get("conflict_cols", ["date_time"]),
                        upsert_mode=target_cfg.get("upsert_mode", "update_insert"),
                    )
                except Exception as exc:
                    client.update_import_batch(
                        import_batch_id=import_batch_id,
                        status="failed",
                        error_count=len(db_df),
                        metadata={"error": str(exc)[:1000]},
                        schema=batch_cfg.get("schema", "ingest"),
                        table=batch_cfg.get("table", "import_batches"),
                    )
                    raise

                client.update_import_batch(
                    import_batch_id=import_batch_id,
                    status="succeeded",
                    row_count=inserted,
                    error_count=0,
                    schema=batch_cfg.get("schema", "ingest"),
                    table=batch_cfg.get("table", "import_batches"),
                )
                self.logger.info(
                    f"{target.label}: inserted/updated rows in {table_name}: {inserted}"
                )
                return inserted

            write_to_database_targets(
                self.cfg.setting_cfg or {"neon_developer": self.cfg.neon_cfg},
                self.logger,
                "charge",
                writer,
            )

        return final_df

    def _load_charge_cfg(self) -> dict:
        with open(self.cfg.charge_yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("charge", raw)

    @staticmethod
    def _file_sha256(path: str) -> str:
        digest = sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
