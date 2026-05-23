from domains.rm_stock.reader import RMStockReader
from domains.rm_stock.processor import RMStockProcessor
import pandas as pd
from pathlib import Path
import yaml
from infrastructure.database_targets import influx_write_enabled
from infrastructure.influx_client import InfluxClient

class RMStockService:
    def __init__(self, logger):
        self.logger = logger
        self.reader = RMStockReader(logger)
        self.processor = RMStockProcessor()

        # Load mapping ONCE
        self.material_map = self._load_materials()

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
            if k and v
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

    # -------------------------------------------------
    # MAIN PROCESS
    # -------------------------------------------------
    def process(self, file_path: str, cfg: dict, run_dates):
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)

        all_results = []

        for run_date in run_dates:
            self.logger.info(f"Processing RM STOCK for {run_date}")

            try:
                df, ts = self.reader.read(file_path, run_date)
            except Exception as e:
                self.logger.error(f"{run_date} failed: {e}")
                continue

            # -----------------------------
            # CLEAN + MAP MATERIALS
            # -----------------------------
            df["material_key"] = df["material"].apply(self._map_material)

            # Drop unmatched
            df = df[df["material_key"].notna()]

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
            return

        final_df = pd.concat(all_results, ignore_index=True)

        output_file = output_dir / "rm_stock_output.xlsx"
        final_df.to_excel(output_file, index=False)

        self.logger.info(f"Excel written -> {output_file}")

        self._write_to_influx(final_df, cfg)

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

