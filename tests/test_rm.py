from __future__ import annotations

import logging
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.rm.neon_mapper import RMNeonMapper
from domains.rm.reader import RMReader
from domains.rm.service import RMService


LOGGER = logging.getLogger("test_rm")
RM_CONFIG = load_yaml("src/config/rm.yaml")["rm"]


class RMSinterReaderTests(unittest.TestCase):
    @patch("domains.rm.reader.pd.read_excel")
    @patch("domains.rm.reader.pd.ExcelFile")
    def test_bf01_reader_reads_only_the_configured_chemistry_sheet(
        self,
        excel_file,
        read_excel,
    ):
        excel_file.return_value = Mock(
            sheet_names=["BF-SKIP SINTER ", "BF LAM COKE"]
        )
        read_excel.return_value = pd.DataFrame(
            [{
                "DATE": datetime(2026, 9, 28),
                "SHIFT": "A-1",
                "BUNKER NO.": "BF-01 [ONLINE]",
                "%Fe(T)": 54.4,
            }]
        )
        cfg = {
            "SINTER_BF01": {
                "col_prefix": "SINTER_SP_01_",
                "columns": "B:Q",
                "header_row": 5,
                "sheet_name": "BF-SKIP SINTER",
                "status_column": "BUNKER NO.",
            }
        }
        frames = RMReader(LOGGER).read("11 BF-01 BUNKER.xlsx", cfg)

        self.assertEqual(len(frames), 1)
        frame, prefix, sheet = frames[0]
        self.assertEqual((prefix, sheet), ("SINTER_SP_01_", "BF-SKIP SINTER"))
        self.assertEqual(frame.loc[0, "ONLINE/OFFLINE"], "BF-01 [ONLINE]")
        read_excel.assert_called_once_with(
            excel_file.return_value,
            sheet_name="BF-SKIP SINTER ",
            usecols="B:Q",
            header=4,
        )

    def test_bf01_online_and_offline_rows_remain_separate(self):
        frame = pd.DataFrame(
            {
                "DATE": ["28-09-2026", "28-09-2026"],
                "SHIFT": ["A-1", "A-2"],
                "ONLINE/OFFLINE": ["BF-01 [ONLINE]", "BF-01 [OFFLINE]"],
                "%FE(T)": [54.4, 53.2],
            }
        )

        result = RMService(LOGGER)._process_sheet(
            frame,
            "SINTER_SP_01_",
            "BF-SKIP SINTER",
            [date(2026, 9, 28)],
            [],
        )

        self.assertEqual(result.loc[0, "SINTER_SP_01_%FE(T)_ON"], 54.4)
        self.assertEqual(result.loc[0, "SINTER_SP_01_%FE(T)_OFF"], 53.2)

    def test_online_sinter_is_not_written_by_the_generic_mapper(self):
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-09-28 07:00"]),
                "sinter sp 01 online fe_t": [53.2],
                "sinter sp 01 offline fe_t": [51.0],
                "sinter sp 02 online fe_t": [54.4],
                "sinter sp 02 offline fe_t": [52.1],
            }
        )
        mapper = RMNeonMapper(
            material_codes={"sinter_1", "sinter_2", "sinter_3", "sinter_4"},
            category_map=RM_CONFIG["neon"]["category_map"],
        )

        outputs = list(mapper.iter_table_dfs(frame))

        self.assertEqual(len(outputs), 2)
        self.assertEqual(
            {result["material_code"].iloc[0] for _, result in outputs},
            {"sinter_2", "sinter_4"},
        )
        self.assertTrue(all(table == "sinter_chemistry" for table, _ in outputs))


if __name__ == "__main__":
    unittest.main()
