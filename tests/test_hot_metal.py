from __future__ import annotations

import logging
import sys
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.hot_metal.reader import HotMetalReader


LOGGER = logging.getLogger("test_hot_metal")
HOT_METAL_CONFIG = load_yaml("src/config/hot_metal.yaml")["hot_metal"]


class HotMetalReaderTests(unittest.TestCase):
    def test_specification_suffixes_map_phosphorus_and_basicity_fields(self):
        headers = [
            "EML LAB SAMPLE ID",
            "DATE",
            "CAST NO(Ladle No.)",
            "H.M.T.",
            "RECD TIME",
            "REPO TIME",
            "%C",
            "%Mn",
            "%Si",
            "%S",
            "%P",
            "%Ti",
            "%Cr",
            "%Fe",
            "%SiO2",
            "%CaO",
            "%MgO",
            "%Al2O3",
            "%FeO",
            "%MnO",
            "%S",
            "%Na2O",
            "%K2O",
            "%TiO2",
            "BASICITY",
            "T.BASICITY",
        ]
        specifications = [
            "",
            "",
            "SPECIFICATION",
            "1470⁰C to 1500⁰C",
            "",
            "",
            "",
            "",
            "0.30% to 0.70%",
            "< 0.040 %",
            "< 0.100 %",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "0.95 to 1.05",
            "",
        ]

        values = [
            "HM-1",
            datetime(2026, 9, 1),
            "CAST-1",
            1490,
            "01:00",
            "01:10",
            4.5,
            0.4,
            0.5,
            0.03,
            0.141,
            0.05,
            0.02,
            94.0,
            32.0,
            34.0,
            8.0,
            15.0,
            1.0,
            0.4,
            0.2,
            0.1,
            0.1,
            0.5,
            1.0625,
            1.3,
        ]
        header_frame = pd.DataFrame(
            [[""] * len(headers) for _ in range(3)] + [headers, specifications]
        )
        data_frame = pd.DataFrame([values])

        class FakeExcelFile:
            sheet_names = ["SEP-26"]

            def parse(self, sheet, **kwargs):
                if kwargs.get("nrows") is not None:
                    return header_frame.copy()
                return data_frame.copy()

        with patch(
            "domains.hot_metal.reader.pd.ExcelFile",
            return_value=FakeExcelFile(),
        ):
            config = deepcopy(HOT_METAL_CONFIG)
            config["hot_metal_config"]["sheet_name"] = "SEP-26"
            config["hot_metal_config"]["sheets"] = {
                "SEP-26": {"columns": "A:Z", "header_row": [3, 4]}
            }
            raw = HotMetalReader(LOGGER).read_for_dates(
                "hot-metal.xlsx",
                ["01-Sep-2026"],
                config,
            )

        result = raw.rename(columns=config["hot_metal_fields"])

        self.assertIn("chem_pct_p", result.columns)
        self.assertIn("slag_basicity", result.columns)
        self.assertEqual(result.iloc[0]["chem_pct_p"], 0.141)
        self.assertEqual(result.iloc[0]["slag_basicity"], 1.0625)


if __name__ == "__main__":
    unittest.main()
