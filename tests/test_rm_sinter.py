from __future__ import annotations

import json
import logging
import shutil
import sys
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
        self.temp_dir = Path(__file__).resolve().parent / ".rm-sinter-test-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()
        self.state_path = self.temp_dir / "sinter_state.json"
        self.processor = RMSinterProcessor(
            LOGGER,
            self.state_path,
            now=lambda: datetime(2026, 10, 8, 12, 0),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _prepare(self, sources, modified=None, run_dates=None):
        return self.processor.prepare(
            source_frames=sources,
            run_dates=run_dates or [RUN_DATE],
            source_modified_at=modified or {},
        )

    def _accept(self, batch):
        self.processor.stage(batch)
        self.processor.commit(batch)

    def test_bf2_uses_shift_anchor_instead_of_sample_time(self):
        batch = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.8, sample_time="09:30HRS")]}
        )

        self.assertEqual(len(batch.rows), 1)
        self.assertEqual(batch.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(batch.rows.loc[0, "material_code"], "sinter_3")
        self.assertEqual(
            batch.pending_keys,
            (("2026-10-08T07:00:00.000000", "BF2"),),
        )

    def test_bf1_input_is_ignored(self):
        batch = self._prepare(
            {
                "BF1": [_frame("BF1", fe_t=54.1)],
                "BF2": [_frame("BF2", fe_t=54.2, sample_time="08:20HRS")],
            },
            {"BF1": datetime(2026, 10, 8, 11, 15)},
        )

        self.assertEqual(len(batch.rows), 1)
        self.assertEqual(batch.rows.loc[0, "fe_t"], 54.2)
        self.assertEqual(batch.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))

    def test_stale_bf1_state_cannot_create_a_second_shift_row(self):
        shift_key = "2026-10-08T07:00:00.000000"
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "shifts": {
                        shift_key: {
                            "BF1": {
                                "assigned_date_time": "2026-10-08T11:15:00.000000",
                                "status": "pending",
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        batch = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.2, sample_time="09:30HRS")]}
        )

        self.assertEqual(len(batch.rows), 1)
        self.assertEqual(batch.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(set(batch.state["shifts"][shift_key]), {"BF2"})

    def test_multiple_bf2_frames_for_a_shift_produce_one_canonical_row(self):
        batch = self._prepare(
            {
                "BF2": [
                    _frame("BF2", fe_t=54.1, sample_time="08:20HRS"),
                    _frame("BF2", fe_t=54.2, sample_time="09:30HRS"),
                ]
            }
        )

        self.assertEqual(len(batch.rows), 1)
        self.assertEqual(batch.rows.loc[0, "fe_t"], 54.2)
        self.assertEqual(batch.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))

    def test_unchanged_normalized_fingerprint_produces_no_row(self):
        first = self._prepare(
            {"BF2": [_frame("BF2", fe_t="54.4000")]},
        )
        self._accept(first)

        unchanged = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.4)]},
        )
        self.assertTrue(unchanged.rows.empty)

    def test_bf2_correction_updates_the_same_shift_row(self):
        first = self._prepare(
            {"BF2": [_frame("BF2", fe_t=54.1, sample_time="08:20HRS")]},
        )
        self._accept(first)

        correction = self._prepare(
            {"BF2": [_frame("BF2", fe_t=55.0, sample_time="11:45HRS")]},
        )
        self.assertEqual(len(correction.rows), 1)
        self.assertEqual(correction.rows.loc[0, "date_time"], pd.Timestamp("2026-10-08 07:00"))
        self.assertEqual(correction.rows.loc[0, "fe_t"], 55.0)
        shift_state = next(iter(correction.state["shifts"].values()))
        self.assertEqual(set(shift_state), {"BF2"})

    def test_shift_anchors_are_the_required_utc_times_after_db_conversion(self):
        expected = {
            "A-1": (
                pd.Timestamp("2026-10-08 07:00"),
                pd.Timestamp("2026-10-08 01:30", tz="UTC"),
            ),
            "B-1": (
                pd.Timestamp("2026-10-08 15:00"),
                pd.Timestamp("2026-10-08 09:30", tz="UTC"),
            ),
            "C-1": (
                pd.Timestamp("2026-10-07 23:00"),
                pd.Timestamp("2026-10-07 17:30", tz="UTC"),
            ),
        }
        for shift, (local_timestamp, utc_timestamp) in expected.items():
            with self.subTest(shift=shift):
                state_path = self.temp_dir / f"{shift[0]}.json"
                processor = RMSinterProcessor(LOGGER, state_path)
                batch = processor.prepare(
                    {"BF2": [_frame("BF2", shift=shift, sample_time="18:30HRS")]},
                    [RUN_DATE],
                )
                actual = batch.rows.loc[0, "date_time"]
                self.assertEqual(actual, local_timestamp)
                self.assertEqual(
                    actual.tz_localize("Asia/Kolkata").tz_convert("UTC"),
                    utc_timestamp,
                )

    def test_c_shift_sample_time_does_not_replace_the_shift_anchor(self):
        first = self._prepare(
            {"BF2": [_frame("BF2", shift="C-1", fe_t=54.1, sample_time="00:30HRS")]},
        )
        self.assertEqual(first.rows.loc[0, "date_time"], pd.Timestamp("2026-10-07 23:00"))

    def test_bf2_offline_is_ignored_and_offline_mapping_is_preserved(self):
        batch = self._prepare({"BF2": [_frame("BF2", online=False)]})
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
            {"BF2": [_frame("BF2", sample_time="08:20HRS")]},
        )
        self.processor.stage(first)

        restarted = RMSinterProcessor(
            LOGGER,
            self.state_path,
            now=lambda: datetime(2026, 10, 8, 13, 0),
        )
        retry = restarted.prepare(
            {"BF2": [_frame("BF2", sample_time="08:20HRS")]},
            [RUN_DATE],
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
    def test_database_boundary_rejects_non_shift_sinter_timestamp(self):
        invalid = pd.DataFrame(
            {
                "date_time": pd.to_datetime(["2026-10-08 09:30"]),
                "material_code": ["sinter_3"],
            }
        )

        with self.assertRaisesRegex(ValueError, "01:30, 09:30, or 17:30 UTC"):
            RMService._validate_sinter_shift_timestamps(invalid)

    def test_database_boundary_accepts_required_utc_sinter_timestamps(self):
        valid = pd.DataFrame(
            {
                "date_time": pd.to_datetime(
                    [
                        "2026-10-08 01:30Z",
                        "2026-10-08 09:30Z",
                        "2026-10-08 17:30Z",
                    ]
                ),
                "material_code": ["sinter_3"] * 3,
            }
        )

        RMService._validate_sinter_shift_timestamps(valid)

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
                "date_time": pd.to_datetime(["2026-10-08 07:00"]),
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
