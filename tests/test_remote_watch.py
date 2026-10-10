"""Pure unit tests for the network-free parts of the Synology change checker."""
import importlib.util
import json
import unittest
from pathlib import Path

FILE = Path(__file__).resolve().parents[1] / "watch_synology.py"
SPEC = importlib.util.spec_from_file_location("watch_synology", FILE)
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


def file(name, mtime, size=100, folder="/QC_LAB_DATA"):
    return {
        "name": name, "path": f"{folder}/{name}", "isdir": False,
        "additional": {"time": {"mtime": mtime}, "size": size},
    }


class WatchTests(unittest.TestCase):
    def test_identifiers_and_lock_files(self):
        self.assertTrue(watch.match_identifier(
            "12 BF-02 BUNKER 2027-28.xlsx", "BF-02 BUNKER"))
        self.assertTrue(watch.match_identifier(
            "06 BF 02 HOT METAL SLAG & GAS.xlsx", "BF-02- HOT METAL, SLAG"))
        self.assertFalse(watch.relevant_file(file("~$BF-02 BUNKER.xlsx", 1)))
        self.assertFalse(watch.relevant_file(file("BF-02 BUNKER.xlsx.tmp", 1)))

    def test_charge_folder_url_double_decoding(self):
        url = (
            "https://synology.example/index.cgi?launchApp=files&"
            "launchParam=openfile%3D%252FV-Optimaise%2520Data%252FBF2%2520AUTO%2520"
            "REPORTS%252FCHARGE%2520AND%2520DUMP%252FHOURLY%252F"
        )
        self.assertEqual(
            watch.charge_folder_from_url(url),
            "/V-Optimaise Data/BF2 AUTO REPORTS/CHARGE AND DUMP/HOURLY",
        )

    def test_latest_file_uses_server_mtime(self):
        old = file("12 BF-02 BUNKER 2026-27.xlsx", 100)
        new = file("12 BF-02 BUNKER 2027-28.xlsx", 200)
        self.assertEqual(
            watch.latest_matching([old, new], "BF-02 BUNKER"), new)
        self.assertEqual(json.loads(watch.file_signature(new))[1:], [200, 100])

    def test_stability_and_retries(self):
        key = "profile_2:rm"
        info = {"name": "bunker.xlsx", "fingerprint": "one", "modes": ("rm", "dust")}
        state = {"processed": {}, "observed": {}}
        self.assertEqual(watch.update_observations(state, {key: info}), {})
        self.assertEqual(watch.update_observations(state, {key: info}), {key: info})
        # Without a successful downstream run, the update remains pending.
        self.assertEqual(watch.update_observations(state, {key: info}), {key: info})
        state["processed"][key] = "one"
        self.assertEqual(watch.update_observations(state, {key: info}), {})
        newer = {**info, "fingerprint": "two"}
        self.assertEqual(watch.update_observations(state, {key: newer}), {})
        self.assertEqual(watch.update_observations(state, {key: newer}), {key: newer})

    def test_source_fans_out_to_all_relevant_modes(self):
        self.assertEqual(watch.FILE_TO_MODES["rm"], ("rm", "fines_analysis", "dust"))
        self.assertEqual(watch.FILE_TO_MODES["rm_sinter"], ("rm",))
        self.assertEqual(watch.FILE_TO_MODES["rm_stock_sinter"], ("rm_stock",))


if __name__ == "__main__":
    unittest.main()

class ClientAndMappingTests(unittest.TestCase):
    def test_fetches_one_listing_per_profile_plus_charge_and_fans_out(self):
        from unittest.mock import patch
        main_cfg = {
            "eml": {
                "login_url": "https://synology.example/",
                "hourly_url": (
                    "https://synology.example/index.cgi?launchParam=openfile%3D"
                    "%252FV-Optimaise%2520Data%252FCHARGE%2520AND%2520DUMP%252FHOURLY%252F"
                ),
                "profiles": {
                    "profile_1": {
                        "user": "first", "password": "secret",
                        "file_station_search_path": "/V-Optimaise Data/",
                        "modes": ["charge", "dpr", "rm_strength", "rm_stock", "ash"],
                    },
                    "profile_2": {
                        "user": "second", "password": "secret",
                        "file_station_search_path": "/QC_LAB_DATA",
                        "modes": ["rm", "fines_analysis", "hot_metal", "dust"],
                    },
                },
            },
            "portal_files": {
                "rm": "BF-02 BUNKER", "fines_analysis": "BF-02 BUNKER",
                "dust_basic": "BF-02 BUNKER", "dpr": "BF-02 DPR",
            },
        }
        paths = []

        class FakeClient:
            def __init__(self, base_url, user, password):
                self.user = user
            def login(self):
                pass
            def logout(self):
                pass
            def list_files(self, folder):
                paths.append((self.user, folder))
                if folder == "/QC_LAB_DATA":
                    return [file("12 BF-02 BUNKER 2026-27.xlsx", 500)]
                if folder == "/V-Optimaise Data":
                    return [file("08 BF-02 DPR.xlsx", 500, folder=folder)]
                return []

        with patch.object(watch, "DSMClient", FakeClient):
            located = watch.collect_files(main_cfg)
        self.assertEqual(len(paths), 3)
        self.assertEqual(
            located["profile_2:rm"]["modes"], ("rm", "fines_analysis", "dust")
        )
        self.assertEqual(located["profile_1:dpr"]["modes"], ("dpr",))
        self.assertIn(("first", "/V-Optimaise Data/CHARGE AND DUMP/HOURLY"), paths)

class TimerFlowTests(unittest.TestCase):
    def test_prime_does_not_run_process_and_success_acknowledges_change(self):
        from unittest.mock import Mock, patch
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            source = {"profile_2:rm": {
                "name": "BUNKER.xlsx", "path": "/QC_LAB_DATA/BUNKER.xlsx",
                "fingerprint": "v1", "modes": ("rm", "fines_analysis", "dust"),
            }}
            run = Mock(return_value=Mock(returncode=0))
            with patch.object(watch, "STATE_PATH", path), \
                 patch.object(watch, "load_config", return_value={}), \
                 patch.object(watch, "collect_files", return_value=source), \
                 patch.object(watch.subprocess, "run", run):
                self.assertEqual(watch.main(["--prime"]), 0)
                self.assertEqual(watch.main([]), 0)
                run.assert_not_called()
                source["profile_2:rm"] = {**source["profile_2:rm"], "fingerprint": "v2"}
                self.assertEqual(watch.main([]), 0)  # first check after a change
                run.assert_not_called()
                self.assertEqual(watch.main([]), 0)  # stable change -> run
                self.assertEqual(run.call_count, 1)
                argv = run.call_args.args[0]
                self.assertEqual(argv[2:4], ["--mode", "rm,fines_analysis,dust"])
                self.assertEqual(watch.load_state(path)["processed"]["profile_2:rm"], "v2")

    def test_failed_app_is_not_acknowledged(self):
        from unittest.mock import Mock, patch
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            source = {"profile_1:dpr": {
                "name": "BF-02 DPR.xlsx", "path": "/reports/BF-02 DPR.xlsx",
                "fingerprint": "v7", "modes": ("dpr",),
            }}
            with patch.object(watch, "STATE_PATH", path), \
                 patch.object(watch, "load_config", return_value={}), \
                 patch.object(watch, "collect_files", return_value=source), \
                 patch.object(watch.subprocess, "run", return_value=Mock(returncode=17)):
                self.assertEqual(watch.main([]), 0)
                self.assertEqual(watch.main([]), 17)
                self.assertNotIn("profile_1:dpr", watch.load_state(path)["processed"])
                self.assertEqual(watch.main([]), 17)
