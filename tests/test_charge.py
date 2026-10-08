from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.charge.processor import RawChargeProcessor


CHARGE_CONFIG = load_yaml("src/config/charge.yaml")["charge"]


class RawChargeProcessorTests(unittest.TestCase):
    def test_all_online_coke_hoppers_are_summed_into_coke_1(self):
        wide = pd.DataFrame(
            [
                {
                    "Date": "2026-09-21 00:07:35.887",
                    "charge_no": 0,
                    "source_row_number": 8,
                    "hopper_1_material_code": "ONLINE",
                    "hopper_1_value": 1475,
                    "hopper_10_material_code": "coke_online",
                    "hopper_10_value": 2358,
                }
            ]
        )

        result = RawChargeProcessor().to_charge_data_table(
            wide,
            material_column_overrides=CHARGE_CONFIG["material_column_overrides"],
            import_batch_id="test-batch",
        )

        self.assertAlmostEqual(result.iloc[0]["coke_1_mt"], 3.833)
        self.assertEqual(result.iloc[0]["coke_2_mt"], 0.0)


if __name__ == "__main__":
    unittest.main()
