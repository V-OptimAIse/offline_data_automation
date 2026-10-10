from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.rm_strength.service import RMStrengthService


LOGGER = logging.getLogger("test_rm_strength")


def _write_coke_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "COKE"
    sheet["B2"] = "Date"
    sheet["Q2"] = "%M-40"
    sheet["R2"] = "%M-10"
    sheet["Z2"] = "%CRI"
    sheet["AA2"] = "%CSR"
    sheet["B3"] = datetime(2026, 4, 2)
    sheet["Q3"] = 81.76
    sheet["R3"] = 6.48
    sheet["Z3"] = 25.66
    sheet["AA3"] = 65.69
    sheet["B4"] = datetime(2026, 4, 3)
    sheet["Q4"] = "*"
    sheet["R4"] = "*"
    sheet["Z4"] = "*"
    sheet["AA4"] = "*"
    workbook.save(path)


def _write_sinter_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "APR-26"
    sheet["A2"] = "Date"
    sheet["D2"] = "REPORT  TIME"
    sheet["AG2"] = "TI"
    sheet["AH2"] = "AI"
    sheet["AI2"] = "RDI"
    sheet["AJ2"] = "RI"
    sheet["AK2"] = "RDI"
    sheet["AL2"] = "RI"

    sheet["A4"] = datetime(2026, 4, 2)
    sheet["D4"] = 1.4
    sheet["AG4"] = 81.0
    sheet["AH4"] = 5.0
    sheet["AI4"] = 30.0
    sheet["AJ4"] = 66.0
    sheet["AK4"] = 999.0
    sheet["AL4"] = 888.0

    sheet["A5"] = datetime(2026, 4, 2)
    sheet["D5"] = 5.4
    sheet["AG5"] = 82.0
    sheet["AH5"] = 4.5
    sheet["AK5"] = 777.0
    sheet["AL5"] = 666.0
    workbook.save(path)


class RMStrengthServiceTests(unittest.TestCase):
    def test_reads_exact_source_columns_and_preserves_missing_values(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            coke_path = root / "01 COKE OVEN 2026-27.xlsx"
            sinter_path = root / "02 SP-02 PRODUCT 2026-27.xlsx"
            _write_coke_workbook(coke_path)
            _write_sinter_workbook(sinter_path)

            cfg = load_yaml("src/config/rm_strength.yaml")
            cfg["rm_strength"]["output"]["dir"] = str(root / "output")
            result = RMStrengthService(LOGGER).process(
                coke_file=str(coke_path),
                sinter_file=str(sinter_path),
                setting_cfg=cfg,
                run_dates=["02-Apr-2026"],
            )

            self.assertIsNotNone(result)
            self.assertEqual(len(result), 3)

            coke = result[result["material_code"] == "coke_1"].iloc[0]
            self.assertEqual(coke["date_time"], pd.Timestamp("2026-04-02 00:00:00"))
            self.assertEqual(
                coke[["property_1", "property_2", "property_3", "property_4"]].tolist(),
                [81.76, 6.48, 25.66, 65.69],
            )

            sinter = result[result["material_code"] == "sinter_3"].reset_index(drop=True)
            self.assertEqual(sinter.loc[0, "date_time"], pd.Timestamp("2026-04-02 01:40:00"))
            self.assertEqual(
                sinter.loc[0, ["property_1", "property_2", "property_3", "property_4"]].tolist(),
                [5.0, 81.0, 30.0, 66.0],
            )
            self.assertEqual(sinter.loc[1, "date_time"], pd.Timestamp("2026-04-02 05:40:00"))
            self.assertEqual(sinter.loc[1, "property_1"], 4.5)
            self.assertEqual(sinter.loc[1, "property_2"], 82.0)
            self.assertTrue(pd.isna(sinter.loc[1, "property_3"]))
            self.assertTrue(pd.isna(sinter.loc[1, "property_4"]))
            self.assertTrue(
                (root / "output" / "combined_rm_strength_data.xlsx").is_file()
            )

    def test_invalid_or_empty_strength_values_are_not_synthesized(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            coke_path = root / "01 COKE OVEN 2026-27.xlsx"
            _write_coke_workbook(coke_path)

            cfg = load_yaml("src/config/rm_strength.yaml")
            cfg["rm_strength"]["output"]["dir"] = str(root / "output")
            result = RMStrengthService(LOGGER).process(
                coke_file=str(coke_path),
                sinter_file=None,
                setting_cfg=cfg,
                run_dates=["03-Apr-2026"],
            )

            self.assertIsNone(result)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
