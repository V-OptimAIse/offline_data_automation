from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.rm.service import RMService
from domains.rm.sinter_processor import RMSinterProcessor


LOGGER = logging.getLogger("test_rm_sinter")
RM_CONFIG = load_yaml("src/config/rm.yaml")["rm"]
RUN_DATE = date(2026, 10, 8)


def _frame(
    source: str,
    *,
    shift: str = "A-1",
    fe_t: object = 54.4,
    sample_time: str | None = None,
    online: bool = True,
) -> pd.DataFrame:
    row = {
        "DATE": datetime(2026, 10, 8),
        "SHIFT": shift,
        "BUNKER NO.": f"{source} [{'ONLINE' if online else 'OFFLINE'}]",
        "%Fe(T)": fe_t,
        "%FeO": 9.1,
        "%SiO2": 5.5,
        "%Al2O3": 2.2,
        "%CaO": 11.5,
        "%MgO": 2.3,
        "% T. ALKALI": 0.023,
        "Unnamed: 11": 0.047,
        "%P ": 0.052,
        "%MnO": 0.22,
        "%TiO2": 0.15,
        "BASICITY": 2.09,
    }
    if sample_time is not None:
        row["TIME"] = sample_time
    return pd.DataFrame([row])


class RMSinterEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent,
        )
        self.state_path = Path(self.temp_dir.name) / "sinter_state.json"
        self.processor = RMSinterProcessor(
            LOGGER,
            self.state_path,
            now=lambda: datetime(2026, 10, 8, 12, 0),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _prepare(self, sources, modified=None, run_dates=None):
        return self.processor.prepare(
            source_frames=sources,
            run_dates=run_dates or [RUN_DATE],
            source_modified_at=modified or {},
        )

    def _accept(self, batch):
        self.processor.stage(batch)
        self.processor.commit(batch)

    def test_bf1_first_uses_anchor_and_bf2_later_uses_real_time(self):
        first = self._prepare(
            {"BF1": [_frame("BF1")]},
            {"BF1": datetime(2026, 10, 8, 8, 15)},
        )
        self.assertEqual(first.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(first.rows.loc[0, "material_code"], "sinter_3")
        self._accept(first)

        second = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.8, sample_time="09:30HRS")]}
        )
        self.assertEqual(second.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 09:30"))
        self.assertEqual(set(second.rows["material_code"]), {"sinter_3"})

    def test_bf2_first_is_source_order_independent(self):
        first = self._prepare(
            {"BF2": [_frame("BF2", sample_time="08:20HRS")]}
        )
        self.assertEqual(first.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self._accept(first)

        second = self._prepare(
            {"BF1": [_frame("BF1", fe_t=54.9)]},
            {"BF1": datetime(2026, 10, 8, 11, 15)},
        )
        self.assertEqual(second.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 11:15"))

    def test_both_new_are_sorted_by_event_time_not_source_or_input_order(self):
        batch = self._prepare(
            {
                "BF1": [_frame("BF1", fe_t=54.9)],
                "BF2": [_frame("BF2", fe_t=54.2, sample_time="08:20HRS")],
            },
            {"BF1": datetime(2026, 10, 8, 11, 15)},
        )

        by_fe = batch.rows.set_index("fe_t")["date_time"].to_dict()
        self.assertEqual(by_fe[54.2], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(by_fe[54.9], pd.Timestamp("2026-10-08 11:15"))
        self.assertEqual(set(batch.rows["material_code"]), {"sinter_3"})

    def test_indistinguishable_times_use_documented_deterministic_tie_breaker(self):
        with self.assertLogs("test_rm_sinter", level="WARNING") as captured:
            batch = self._prepare(
                {
                    "BF2": [_frame("BF2", fe_t=54.2)],
                    "BF1": [_frame("BF1", fe_t=54.1)],
                }
            )

        by_fe = batch.rows.set_index("fe_t")["date_time"].to_dict()
        self.assertEqual(by_fe[54.1], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(by_fe[54.2], pd.Timestamp("2026-10-08 12:00"))
        self.assertTrue(any("tie-breaker" in message for message in captured.output))

    def test_unchanged_normalized_fingerprint_produces_no_row(self):
        first = self._prepare(
            {"BF1": [_frame("BF1", fe_t="54.4000")]},
            {"BF1": datetime(2026, 10, 8, 8, 15)},
        )
        self._accept(first)

        unchanged = self._prepare(
            {"BF1": [_frame("BF1", fe_t=54.4)]},
            {"BF1": datetime(2026, 10, 8, 12, 30)},
        )
        self.assertTrue(unchanged.rows.empty)

    def test_same_source_correction_updates_its_assigned_row(self):
        bf1 = self._prepare(
            {"BF1": [_frame("BF1", fe_t=54.1)]},
            {"BF1": datetime(2026, 10, 8, 8, 15)},
        )
        self._accept(bf1)
        bf2 = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.2, sample_time="09:30HRS")]}
        )
        self._accept(bf2)

        correction = self._prepare(
            {"BF1": [_frame("BF1", fe_t=55.0)]},
            {"BF1": datetime(2026, 10, 8, 12, 0)},
        )
        self.assertEqual(len(correction.rows), 1)
        self.assertEqual(correction.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(correction.rows.loc[0, "fe_t"], 55.0)
        shift_state = next(iter(correction.state["shifts"].values()))
        self.assertEqual(set(shift_state), {"BF1", "BF2"})

    def test_shift_anchors_follow_existing_rm_convention(self):
        expected = {
            "A-1": pd.Timestamp("2026-10-08 07:00"),
            "B-1": pd.Timestamp("2026-10-08 15:00"),
            "C-1": pd.Timestamp("2026-10-07 23:00"),
        }
        for shift, timestamp in expected.items():
            with self.subTest(shift=shift):
                state_path = Path(self.temp_dir.name) / f"{shift[0]}.json"
                processor = RMSinterProcessor(LOGGER, state_path)
                batch = processor.prepare(
                    {"BF2": [_frame("BF2", shift=shift, sample_time="18:30HRS")]},
                    [RUN_DATE],
                )
                self.assertEqual(batch.rows.loc[0, "date_time"], timestamp)

    def test_second_c_shift_sample_after_midnight_uses_next_calendar_day(self):
        first = self._prepare(
            {"BF1": [_frame("BF1", shift="C-1", fe_t=54.1)]},
            {"BF1": datetime(2026, 10, 7, 23, 15)},
        )
        self.assertEqual(first.rows.loc[0, "date_time"], pd.Timestamp("2026-10-07 23:00"))
        self._accept(first)

        second = self._prepare(
            {"BF2": [_frame("BF2", shift="C-2", fe_t=54.2, sample_time="00:30HRS")]}
        )
        self.assertEqual(second.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 00:30"))

    def test_bf1_and_bf2_online_both_map_to_sinter_3(self):
        batch = self._prepare(
            {
                "BF1": [_frame("BF1", fe_t=54.1)],
                "BF2": [_frame("BF2", fe_t=54.2, sample_time="09:30HRS")],
            },
            {"BF1": datetime(2026, 10, 8, 8, 15)},
        )
        self.assertEqual(set(batch.rows["material_code"]), {"sinter_3"})
        self.assertEqual(len(batch.rows), 2)

    def test_bf1_offline_is_ignored_by_dedicated_path_and_mapping_is_preserved(self):
        batch = self._prepare({"BF1": [_frame("BF1", online=False)]})
        self.assertTrue(batch.rows.empty)
        self.assertEqual(
            RM_CONFIG["neon"]["category_map"]["sinter sp 01 offline"]["material_code"],
            "sinter_2",
        )
        self.assertEqual(
            RM_CONFIG["neon"]["category_map"]["sinter sp 02 offline"]["material_code"],
            "sinter_4",
        )

    def test_staged_but_uncommitted_event_retries_with_same_assignment(self):
        first = self._prepare(
            {"BF1": [_frame("BF1")]},
            {"BF1": datetime(2026, 10, 8, 8, 15)},
        )
        self.processor.stage(first)

        restarted = RMSinterProcessor(
            LOGGER,
            self.state_path,
            now=lambda: datetime(2026, 10, 8, 13, 0),
        )
        retry = restarted.prepare(
            {"BF1": [_frame("BF1")]},
            [RUN_DATE],
            {"BF1": datetime(2026, 10, 8, 13, 0)},
        )
        self.assertEqual(len(retry.rows), 1)
        self.assertEqual(retry.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))

    def test_bf2_file_change_without_chemistry_change_produces_no_row(self):
        first = self._prepare(
            {"BF2": [_frame("BF2", sample_time="09:30HRS")]},
            {"BF2": datetime(2026, 10, 8, 10, 0)},
        )
        self._accept(first)

        unchanged = self._prepare(
            {"BF2": [_frame("BF2", sample_time="09:30HRS")]},
            {"BF2": datetime(2026, 10, 8, 14, 0)},
        )
        self.assertTrue(unchanged.rows.empty)


class RMSinterNeonTests(unittest.TestCase):
    def test_service_writes_dedicated_and_offline_rows_with_existing_conflict_key(self):
        client = Mock()
        client.fetch_material_codes.return_value = {"sinter_2", "sinter_3"}
        target_columns = {"date_time", "material_code", "fe_t"}
        client.fetch_table_columns.side_effect = lambda schema, tables: {
            table: set(target_columns) for table in tables
        }
        client.insert_dataframe.side_effect = lambda **kwargs: len(kwargs["df"])

        generic = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-10-08 07:00"]),
                "sinter sp 01 online fe_t": [54.1],
                "sinter sp 01 offline fe_t": [53.0],
            }
        )
        dedicated = pd.DataFrame(
            {
                "date_time": pd.to_datetime(["2026-10-08 09:30"]),
                "fe_t": [54.8],
                "material_code": ["sinter_3"],
            }
        )

        total, dedicated_count = RMService(LOGGER)._sync_neon_tables(
            client,
            generic,
            RM_CONFIG,
            sinter_df=dedicated,
        )

        self.assertEqual((total, dedicated_count), (2, 1))
        written_materials = {
            call.kwargs["df"]["material_code"].iloc[0]
            for call in client.insert_dataframe.call_args_list
        }
        self.assertEqual(written_materials, {"sinter_2", "sinter_3"})
        for call in client.insert_dataframe.call_args_list:
            self.assertEqual(
                call.kwargs["conflict_cols"],
                ["material_code", "date_time"],
            )


if __name__ == "__main__":
    unittest.main()
