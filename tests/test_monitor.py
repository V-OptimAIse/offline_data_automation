from __future__ import annotations

import logging
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infrastructure.synology_api_client import RemoteFile
from monitor import SynologyMonitor, load_monitor_state


LOGGER = logging.getLogger("test_monitor")
TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)
P1 = "/V-Optimaise Data"
P2 = "/QC_LAB_DATA"
CHARGE = "/V-Optimaise Data/BF2 AUTO REPORTS/CHARGE AND DUMP/HOURLY"


def remote(name, mtime=100, size=10, folder=P2):
    return RemoteFile(name, f"{folder}/{name}", size, mtime, False)


class FakeClient:
    def __init__(self, folders):
        self.folders = folders
        self.list_calls = []
        self.download_calls = []
        self.login_calls = 0
        self.logout_calls = 0

    def login(self):
        self.login_calls += 1

    def logout(self):
        self.logout_calls += 1

    def list_files(self, folder):
        self.list_calls.append(folder)
        return list(self.folders.get(folder, []))

    def download_file(self, remote_path, local_path, expected_size=None, remote_mtime=None):
        self.download_calls.append(remote_path)
        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * int(expected_size or 1))
        if remote_mtime is not None:
            path.touch()
            import os
            os.utime(path, (remote_mtime, remote_mtime))
        return str(path.resolve())


def config(root: Path):
    return {
        "download": {"download_dir": str(root / "downloads")},
        "monitor": {
            "state_path": str(root / "state.json"),
            "stability_delay_seconds": 0,
            "poll_interval_seconds": 60,
        },
        "eml": {
            "base_url": "https://synology.example",
            "charge_folder_path": CHARGE,
            "profiles": {
                "profile_1": {
                    "user": "one",
                    "password": "password",
                    "file_station_search_path": f"{P1}/",
                    "modes": ["charge", "dpr", "rm_strength", "rm_stock", "ash"],
                },
                "profile_2": {
                    "user": "two",
                    "password": "password",
                    "file_station_search_path": P2,
                    "modes": ["rm", "fines_analysis", "hot_metal", "dust"],
                },
            },
        },
        "portal_files": {
            "rm": "BF-02 BUNKER",
            "rm_sinter": "BF-01 BUNKER",
            "fines_analysis": "BF-02 BUNKER",
            "dpr": "BF-02 DPR",
            "hot_metal": "BF-02- HOT METAL, SLAG",
            "rm_strength_coke": "01 COKE OVEN",
            "rm_strength_sinter": "02 SP-02 PRODUCT",
            "rm_stock": "RM BULK STOCK",
            "rm_stock_sinter": "DPR SP#2",
            "dust_basic": "BF-02 BUNKER",
            "dust_chemical": "GCP DUST CATCHER",
            "ash": "ASH ANALYSIS",
        },
    }


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".monitor-test-data"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir()
        self.cfg = config(self.root)
        self.processor = Mock()
        self.p1 = FakeClient({P1: [], CHARGE: []})
        self.p2 = FakeClient({P2: []})
        self.monitor = SynologyMonitor(
            self.cfg,
            {"profile_1": self.p1, "profile_2": self.p2},
            LOGGER,
            sleep=lambda _seconds: None,
            processor=self.processor,
            now=lambda: NOW,
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def state_path(self):
        return Path(self.cfg["monitor"]["state_path"])

    def test_initial_snapshot_creates_baseline_without_processing(self):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", folder=P1)]
        self.monitor.run_cycle()
        state = load_monitor_state(self.state_path)
        self.assertIn("dpr", state["sources"])
        self.assertEqual(state["sources"]["dpr"]["status"], "completed")
        self.processor.assert_not_called()
        self.assertEqual(self.p1.download_calls, [])
        self.assertEqual(self.p1.list_calls.count(P1), 1)
        self.assertEqual(self.p1.list_calls.count(CHARGE), 1)
        self.assertEqual(self.p2.list_calls.count(P2), 1)

    def test_unchanged_revision_does_nothing(self):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", folder=P1)]
        self.monitor.run_cycle()
        self.monitor.run_cycle()
        self.processor.assert_not_called()

    def _baseline_dpr_then(self, changed):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 100, 10, P1)]
        self.monitor.run_cycle()
        self.p1.folders[P1] = [changed]
        self.monitor.run_cycle()

    def test_mtime_change_triggers_and_commits(self):
        self._baseline_dpr_then(remote("08 BF-02 DPR.xlsx", 101, 10, P1))
        self.assertEqual(self.processor.call_args.args[0], ["dpr"])
        self.assertEqual(
            self.processor.call_args.args[1],
            ["08-Oct-2026", "09-Oct-2026", "10-Oct-2026"],
        )
        self.assertEqual(load_monitor_state(self.state_path)["sources"]["dpr"]["mtime"], 101)

    def test_size_change_triggers(self):
        self._baseline_dpr_then(remote("08 BF-02 DPR.xlsx", 100, 11, P1))
        self.assertEqual(self.processor.call_args.args[0], ["dpr"])

    def test_remote_path_change_triggers(self):
        self._baseline_dpr_then(remote("09 BF-02 DPR Oct.xlsx", 100, 10, P1))
        self.assertEqual(self.processor.call_args.args[0], ["dpr"])

    def test_revision_that_changes_during_stability_delay_waits_for_next_cycle(self):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 100, 10, P1)]
        self.monitor.run_cycle()
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 200, 10, P1)]

        def mutate_during_delay(_seconds):
            self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 201, 11, P1)]

        self.monitor.monitor_cfg["stability_delay_seconds"] = 1
        self.monitor.sleep = mutate_during_delay
        self.monitor.run_cycle()
        self.processor.assert_not_called()
        self.assertEqual(
            load_monitor_state(self.state_path)["sources"]["dpr"]["mtime"], 100
        )
        self.assertEqual(self.p1.list_calls.count(P1), 3)

    def test_missing_source_that_later_appears_triggers(self):
        self.monitor.run_cycle()
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 100, 10, P1)]
        self.monitor.run_cycle()
        self.assertEqual(self.processor.call_args.args[0], ["dpr"])

    def test_failed_processing_does_not_commit_revision(self):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 100, 10, P1)]
        self.monitor.run_cycle()
        self.processor.side_effect = RuntimeError("database unavailable")
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 200, 10, P1)]
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.monitor.run_cycle()
        self.assertEqual(load_monitor_state(self.state_path)["sources"]["dpr"]["mtime"], 100)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            self.monitor.run_cycle()
        self.assertEqual(self.processor.call_count, 2)

    def _profile2_dependencies(self, *, bf02_mtime=100, bf01_mtime=100, dust_mtime=100):
        return [
            remote("12 BF-02 BUNKER 2026-27.xlsx", bf02_mtime),
            remote("11 BF-01 BUNKER 2026-27.xlsx", bf01_mtime),
            remote("15 GCP DUST CATCHER ANALYSIS.xlsx", dust_mtime),
        ]

    def test_bf02_downloads_once_and_triggers_three_modes(self):
        self.p2.folders[P2] = self._profile2_dependencies()
        self.monitor.run_cycle()
        self.p2.folders[P2] = self._profile2_dependencies(bf02_mtime=200)
        self.monitor.run_cycle()
        self.assertEqual(
            self.processor.call_args.args[0], ["rm", "fines_analysis", "dust"]
        )
        bf02_path = f"{P2}/12 BF-02 BUNKER 2026-27.xlsx"
        self.assertEqual(self.p2.download_calls.count(bf02_path), 1)

    def test_bf01_triggers_rm_only(self):
        self.p2.folders[P2] = self._profile2_dependencies()
        self.monitor.run_cycle()
        self.p2.folders[P2] = self._profile2_dependencies(bf01_mtime=200)
        self.monitor.run_cycle()
        self.assertEqual(self.processor.call_args.args[0], ["rm"])

    def test_gcp_dust_catcher_triggers_dust_only(self):
        self.p2.folders[P2] = self._profile2_dependencies()
        self.monitor.run_cycle()
        self.p2.folders[P2] = self._profile2_dependencies(dust_mtime=200)
        self.monitor.run_cycle()
        self.assertEqual(self.processor.call_args.args[0], ["dust"])

    def test_charge_today_uses_nested_folder_and_date_specific_state(self):
        name = "CHARGE_AND_DUMP_REPORT_10_10_2026.xlsx"
        self.p1.folders[CHARGE] = [remote(name, 100, 10, CHARGE)]
        self.monitor.run_cycle()
        self.p1.folders[CHARGE] = [remote(name, 200, 10, CHARGE)]
        self.monitor.run_cycle()
        self.assertEqual(self.processor.call_args.args[0], ["charge"])
        self.assertEqual(self.processor.call_args.args[1], ["10-Oct-2026"])
        state = load_monitor_state(self.state_path)
        self.assertEqual(state["sources"]["charge:2026-10-10"]["mtime"], 200)

    def test_charge_and_non_charge_use_separate_date_windows(self):
        charge_name = "CHARGE_AND_DUMP_REPORT_10_10_2026.xlsx"
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 100, 10, P1)]
        self.p1.folders[CHARGE] = [remote(charge_name, 100, 10, CHARGE)]
        self.monitor.run_cycle()

        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", 200, 10, P1)]
        self.p1.folders[CHARGE] = [remote(charge_name, 200, 10, CHARGE)]
        self.monitor.run_cycle()

        self.assertEqual(self.processor.call_count, 2)
        self.assertEqual(self.processor.call_args_list[0].args[0], ["dpr"])
        self.assertEqual(
            self.processor.call_args_list[0].args[1],
            ["08-Oct-2026", "09-Oct-2026", "10-Oct-2026"],
        )
        self.assertEqual(self.processor.call_args_list[1].args[0], ["charge"])
        self.assertEqual(self.processor.call_args_list[1].args[1], ["10-Oct-2026"])

    def test_ash_downloads_both_financial_year_files_when_window_crosses_april(self):
        boundary_now = datetime(2026, 4, 1, 12, 0, tzinfo=TZ)
        self.p1.folders[P1] = [
            remote("20 ASH ANALYSIS 25-26.xlsx", 100, 10, P1),
            remote("21 ASH ANALYSIS 26-27.xlsx", 100, 10, P1),
        ]
        boundary_monitor = SynologyMonitor(
            self.cfg,
            {"profile_1": self.p1, "profile_2": self.p2},
            LOGGER,
            process_initial=True,
            sleep=lambda _seconds: None,
            processor=self.processor,
            now=lambda: boundary_now,
        )

        boundary_monitor.run_cycle()

        self.assertEqual(self.processor.call_args.args[0], ["ash"])
        self.assertEqual(
            self.processor.call_args.args[1],
            ["30-Mar-2026", "31-Mar-2026", "01-Apr-2026"],
        )
        ash_paths = self.processor.call_args.args[4].by_mode["ash"].paths
        self.assertEqual(len(ash_paths), 2)

    def test_dry_run_does_not_download_process_or_commit(self):
        self.p1.folders[P1] = [remote("08 BF-02 DPR.xlsx", folder=P1)]
        dry_monitor = SynologyMonitor(
            self.cfg,
            {"profile_1": self.p1, "profile_2": self.p2},
            LOGGER,
            dry_run=True,
            sleep=lambda _seconds: None,
            processor=self.processor,
            now=lambda: NOW,
        )
        dry_monitor.run_cycle()
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.p1.download_calls, [])
        self.processor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
