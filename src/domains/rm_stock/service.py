from domains.rm_stock.reader import RMStockReader
from domains.rm_stock.processor import RMStockProcessor
from hashlib import sha256
import pandas as pd
from pathlib import Path
from uuid import uuid4
import yaml
from infrastructure.database_targets import (
    DatabaseTarget,
    influx_write_enabled,
    write_to_database_targets,
)
from infrastructure.influx_client import InfluxClient
from infrastructure.neon_client import NeonClient


RM_STOCK_TABLE_SCHEMA = "offline_feed"
RM_STOCK_TABLE = "raw_material_stock"
RM_STOCK_TABLE_NAME = f"{RM_STOCK_TABLE_SCHEMA}.{RM_STOCK_TABLE}"
RM_STOCK_CONFLICT_COLS = ["material_code", "date_time"]
RM_STOCK_BATCH_SCHEMA = "ingest"
RM_STOCK_BATCH_TABLE = "import_batches"
RM_STOCK_INSERT_COLS = [
    "date_time",
    "material_code",
    "stock_mt",
    "import_batch_id",
    "created_at",
]


class RMStockService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = RMStockReader(logger)
        self.processor = RMStockProcessor()

        # Load mapping ONCE
        self.material_map = self._load_materials()
        self.db_material_map = self._load_db_materials()

    # -------------------------------------------------
    # LOAD MATERIAL MAPPING (YAML -> dict)
    # -------------------------------------------------
    def _load_materials(self):
        with open("src/config/rm_stock.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if not isinstance(data, dict):
            raise ValueError("rm_stock.yaml must be a dict (material -> key)")

        return {
            k.strip().lower(): v.strip()
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }

    # -------------------------------------------------
    # LOAD DB MATERIAL MAPPING (YAML -> dict)
    # -------------------------------------------------
    def _load_db_materials(self):
        with open("src/config/rm_stock.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        db_map = data.get("db_material_map") or {}
        if not isinstance(db_map, dict):
            raise ValueError("rm_stock.yaml db_material_map must be a dict")

        return {
            k.strip().lower(): v.strip()
            for k, v in db_map.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }

    # -------------------------------------------------
    # MAP RAW MATERIAL -> SHORT KEY
    # -------------------------------------------------
    def _map_material(self, material: str):
        material = material.lower().strip()

        for key, value in self.material_map.items():
            if key in material:   # flexible match
                return value

        return None

    def _map_db_material(self, material: str):
        material = material.lower().strip()

        for key, value in sorted(
            self.db_material_map.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if key in material:
                return value

        return None

    @staticmethod
    def _material_code_candidates(material_key):
        if pd.isna(material_key):
            return []

        material_code = str(material_key).strip()
        if not material_code:
            return []

        candidates = [material_code]
        if material_code.lower().endswith("_mt"):
            candidates.append(material_code[:-3])

        return list(dict.fromkeys(candidates))

    def _resolve_material_code(self, material_key, material_codes_by_lower):
        for candidate in self._material_code_candidates(material_key):
            matched = material_codes_by_lower.get(candidate.lower())
            if matched:
                return matched
        return None

    # -------------------------------------------------
    # MAIN PROCESS
    # -------------------------------------------------
    def process(self, file_path: str, cfg: dict, run_dates, sinter_file_path: str | None = None):
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)

        all_results = []
        stock_rows = []
        no_data_stock_rows = []

        for run_date in run_dates:
            self.logger.info(f"Processing RM STOCK for {run_date}")

            try:
                df, ts = self.reader.read(file_path, run_date)
            except Exception as e:
                self.logger.warning(f"{run_date} bulk stock skipped: {e}")
                df = pd.DataFrame(columns=["material", "physical_stock"])
                ts = pd.to_datetime(run_date, format="%d-%b-%Y")

            if sinter_file_path:
                try:
                    sinter_df = self.reader.read_sinter_stock(sinter_file_path, run_date)
                    if sinter_df.empty:
                        no_data_stock_rows.append(
                            {
                                "date_time": pd.to_datetime(ts),
                                "material_key": self._map_material("Sinter Stock at Yard"),
                                "db_material_code": self._map_db_material(
                                    "Sinter Stock at Yard"
                                ),
                            }
                        )
                    df = pd.concat(
                        [df, sinter_df],
                        ignore_index=True,
                    )
                except Exception as e:
                    self.logger.warning(f"{run_date} sinter stock skipped: {e}")

            # -----------------------------
            # CLEAN + MAP MATERIALS
            # -----------------------------
            df["material_key"] = df["material"].apply(self._map_material)
            df["db_material_code"] = df["material"].apply(self._map_db_material)

            # Drop unmatched
            df = df[df["material_key"].notna()].copy()

            blank_stock = df["physical_stock"].isna()
            if blank_stock.any():
                self.logger.info(
                    f"{run_date} RM STOCK skipped {int(blank_stock.sum())} "
                    "mapped row(s) with blank physical_stock"
                )
                df = df[~blank_stock].copy()

            if df.empty:
                self.logger.warning(f"No mapped data for {run_date}")
                continue

            # -----------------------------
            # ADD TIMESTAMP (processor)
            # -----------------------------
            df = self.processor.process(df, ts)

            # -----------------------------
            # AGGREGATE
            # -----------------------------
            db_part = (
                df.groupby(
                    ["material_key", "db_material_code"],
                    as_index=False,
                    dropna=False,
                )["physical_stock"]
                .sum()
                .rename(columns={"physical_stock": "stock_mt"})
            )
            db_part.insert(0, "date_time", pd.to_datetime(ts))
            stock_rows.append(
                db_part[["date_time", "material_key", "db_material_code", "stock_mt"]]
            )

            df = df.groupby("material_key", as_index=False)["physical_stock"].sum()

            # -----------------------------
            # PIVOT -> single row
            # -----------------------------
            df_final = (
                df.set_index("material_key")["physical_stock"]
                .to_frame()
                .T
            )

            df_final.insert(0, "date", pd.to_datetime(ts))

            all_results.append(df_final)

        # -------------------------------------------------
        # FINAL OUTPUT
        # -------------------------------------------------
        if not all_results:
            self.logger.warning("No RM STOCK data processed")
            if no_data_stock_rows:
                self._push_to_database_targets(
                    pd.DataFrame(
                        columns=[
                            "date_time",
                            "material_key",
                            "db_material_code",
                            "stock_mt",
                        ]
                    ),
                    cfg,
                    file_path,
                    run_dates,
                    sinter_file_path,
                    no_data_stock_rows=pd.DataFrame(no_data_stock_rows),
                )
            return

        final_df = pd.concat(all_results, ignore_index=True)

        output_file = output_dir / "rm_stock_output.xlsx"
        final_df.to_excel(output_file, index=False)

        self.logger.info(f"Excel written -> {output_file}")

        self._write_to_influx(final_df, cfg)
        stock_df = pd.concat(stock_rows, ignore_index=True)
        self._push_to_database_targets(
            stock_df,
            cfg,
            file_path,
            run_dates,
            sinter_file_path,
            no_data_stock_rows=pd.DataFrame(no_data_stock_rows),
        )

    def _target_db_frame(self, df, material_codes, import_batch_id, created_at):
        material_codes_by_lower = {
            str(code).lower(): code
            for code in (material_codes or set())
            if code
        }

        if not material_codes_by_lower:
            return pd.DataFrame(columns=RM_STOCK_INSERT_COLS), len(df)

        target_df = df.copy()
        target_df["material_code"] = target_df.apply(
            lambda row: self._resolve_material_code(
                row["db_material_code"]
                if "db_material_code" in row and pd.notna(row["db_material_code"])
                else row["material_key"],
                material_codes_by_lower,
            ),
            axis=1,
        )

        skipped = target_df["material_code"].isna()
        skipped_count = int(skipped.sum())
        target_df = target_df[~skipped].copy()
        if target_df.empty:
            return pd.DataFrame(columns=RM_STOCK_INSERT_COLS), skipped_count

        target_df["stock_mt"] = pd.to_numeric(target_df["stock_mt"], errors="coerce")
        target_df = target_df.dropna(subset=["stock_mt"]).copy()
        if target_df.empty:
            return pd.DataFrame(columns=RM_STOCK_INSERT_COLS), skipped_count

        target_df = (
            target_df.groupby(["date_time", "material_code"], as_index=False, dropna=False)[
                "stock_mt"
            ]
            .sum()
        )

        target_df["import_batch_id"] = import_batch_id
        target_df["created_at"] = created_at

        return target_df[RM_STOCK_INSERT_COLS], skipped_count

    def _target_delete_frame(self, df, material_codes):
        if df is None or df.empty:
            return pd.DataFrame(columns=RM_STOCK_CONFLICT_COLS)

        material_codes_by_lower = {
            str(code).lower(): code
            for code in (material_codes or set())
            if code
        }
        if not material_codes_by_lower:
            return pd.DataFrame(columns=RM_STOCK_CONFLICT_COLS)

        target_df = df.copy()
        target_df["material_code"] = target_df.apply(
            lambda row: self._resolve_material_code(
                row["db_material_code"]
                if "db_material_code" in row and pd.notna(row["db_material_code"])
                else row["material_key"],
                material_codes_by_lower,
            ),
            axis=1,
        )
        target_df["date_time"] = pd.to_datetime(target_df["date_time"], errors="coerce")
        target_df = target_df.dropna(subset=RM_STOCK_CONFLICT_COLS)

        return target_df[RM_STOCK_CONFLICT_COLS].drop_duplicates()

    # -------------------------------------------------
    # POSTGRES WRITER (NEON + PI)
    # -------------------------------------------------
    def _push_to_database_targets(
        self,
        df,
        cfg,
        file_path,
        run_dates,
        sinter_file_path=None,
        no_data_stock_rows=None,
    ):
        import_batch_id = str(uuid4())
        created_at = pd.Timestamp.now(tz="UTC")
        source_path = Path(file_path)
        source_hash = self._file_sha256(source_path)
        sinter_path = Path(sinter_file_path) if sinter_file_path else None
        metadata = {
            "run_dates": list(run_dates),
            "skipped_material_rows": 0,
            "no_data_rows_deleted": 0,
        }
        if sinter_path:
            metadata.update(
                {
                    "sinter_source_filename": sinter_path.name,
                    "sinter_source_path": str(sinter_path.resolve()),
                    "sinter_file_sha256": self._file_sha256(sinter_path),
                }
            )

        def writer(client: NeonClient, target: DatabaseTarget) -> int:
            material_codes = client.fetch_material_codes(
                schema="plant_master",
                table="materials",
                code_column="material_code",
                active_column="is_active",
            )
            if not material_codes:
                self.logger.warning(
                    f"{target.label}: no material codes loaded from "
                    "plant_master.materials; skipping RM STOCK DB rows"
                )

            target_df, skipped_count = self._target_db_frame(
                df,
                material_codes,
                import_batch_id,
                created_at,
            )
            delete_df = self._target_delete_frame(no_data_stock_rows, material_codes)
            batch_metadata = {
                **metadata,
                "skipped_material_rows": skipped_count,
                "no_data_rows_to_delete": len(delete_df),
                "no_data_rows_deleted": 0,
            }

            if skipped_count:
                self.logger.warning(
                    f"{target.label}: skipped {skipped_count} RM STOCK rows "
                    "with unknown material_code"
                )

            table_columns = client.fetch_table_columns(
                RM_STOCK_TABLE_SCHEMA,
                {RM_STOCK_TABLE},
            ).get(RM_STOCK_TABLE, set())
            if table_columns:
                insert_cols = [
                    col
                    for col in RM_STOCK_INSERT_COLS
                    if col in target_df.columns and col in table_columns
                ]
                target_df = target_df[insert_cols]
            else:
                self.logger.warning(
                    f"{target.label}: no column metadata loaded for "
                    f"{RM_STOCK_TABLE_NAME}; inserting configured columns"
                )

            client.create_import_batch(
                import_batch_id=import_batch_id,
                source_type="excel",
                domain="rm_stock",
                parser_name="rm_stock_report",
                source_filename=source_path.name,
                source_path=str(source_path.resolve()),
                file_sha256=source_hash,
                row_count=len(target_df),
                metadata=batch_metadata,
                schema=RM_STOCK_BATCH_SCHEMA,
                table=RM_STOCK_BATCH_TABLE,
            )

            deleted_rows = 0
            try:
                if not delete_df.empty:
                    deleted_rows = client.delete_dataframe_keys(
                        df=delete_df,
                        table_name=RM_STOCK_TABLE_NAME,
                        conflict_cols=RM_STOCK_CONFLICT_COLS,
                    )
                    self.logger.info(
                        f"{target.label} {RM_STOCK_TABLE_NAME}: "
                        f"{deleted_rows} no-data row(s) deleted"
                    )

                rows = client.insert_dataframe(
                    df=target_df,
                    table_name=RM_STOCK_TABLE_NAME,
                    conflict_cols=RM_STOCK_CONFLICT_COLS,
                    upsert_mode="update_insert",
                )
            except Exception as exc:
                client.update_import_batch(
                    import_batch_id=import_batch_id,
                    status="failed",
                    error_count=len(target_df),
                    metadata={"error": str(exc)[:1000]},
                    schema=RM_STOCK_BATCH_SCHEMA,
                    table=RM_STOCK_BATCH_TABLE,
                )
                raise

            client.update_import_batch(
                import_batch_id=import_batch_id,
                status="succeeded",
                row_count=rows,
                error_count=0,
                metadata={"no_data_rows_deleted": deleted_rows},
                schema=RM_STOCK_BATCH_SCHEMA,
                table=RM_STOCK_BATCH_TABLE,
            )
            self.logger.info(
                f"{target.label} {RM_STOCK_TABLE_NAME}: {rows} rows synced, "
                f"{skipped_count} rows skipped"
            )
            return rows

        write_to_database_targets(
            cfg,
            self.logger,
            "RM STOCK",
            writer,
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # -------------------------------------------------
    #  INFLUX WRITER
    # -------------------------------------------------
    def _write_to_influx(self, df, cfg):
        if not influx_write_enabled(cfg):
            self.logger.info("InfluxDB disabled by write_db; skipping RM STOCK Influx push")
            return

        influx_cfg = cfg.get("influxdb")

        if not influx_cfg:
            self.logger.warning("No InfluxDB config found. Skipping write.")
            return

        client = InfluxClient(influx_cfg)

        try:
            client.write_dataframe(
                df=df,
                measurement="rm_stock",
                tag_keys=[],  
            )
            self.logger.info("Data successfully written to InfluxDB")

        except Exception as e:
            self.logger.error(f"Influx write failed: {e}")

        finally:
            client.close()

