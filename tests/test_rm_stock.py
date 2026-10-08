from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domains.rm_stock.service import RMStockService


class RMStockMappingTests(unittest.TestCase):
    def setUp(self):
        self.service = RMStockService(logging.getLogger("test.rm_stock"))

    def test_crushed_rom_spacing_variant_maps_to_nmdc_rom(self):
        source_name = "NMDC  Limited Crushed ROM (10-40MM) (Kirandul)"

        self.assertEqual(self.service._map_material(source_name), "nmdc_rom_mt")
        self.assertEqual(self.service._map_db_material(source_name), "ore_5")

    def test_nmdc_lumps_is_not_merged_into_nmdc_rom_database_stock(self):
        source_name = "NMDC  Limited Lumps (10-150 MM) (Kirandul)"

        self.assertEqual(
            self.service._map_material(source_name),
            "nmdc_rom_lumps_kirandul_mt",
        )
        self.assertIsNone(self.service._map_db_material(source_name))

        source_rows = pd.DataFrame(
            [
                {
                    "date_time": pd.Timestamp("2026-09-23 10:32:18"),
                    "material_key": "nmdc_rom_mt",
                    "db_material_code": "ore_5",
                    "stock_mt": 9024.944,
                },
                {
                    "date_time": pd.Timestamp("2026-09-23 10:32:18"),
                    "material_key": "nmdc_rom_lumps_kirandul_mt",
                    "db_material_code": None,
                    "stock_mt": 24641.390,
                },
            ]
        )

        result, skipped_count = self.service._target_db_frame(
            source_rows,
            material_codes={"ore_5"},
            import_batch_id="test-batch",
            created_at=pd.Timestamp("2026-09-23", tz="UTC"),
        )

        self.assertEqual(skipped_count, 1)
        self.assertEqual(result["material_code"].tolist(), ["ore_5"])
        self.assertEqual(result["stock_mt"].tolist(), [9024.944])


if __name__ == "__main__":
    unittest.main()
