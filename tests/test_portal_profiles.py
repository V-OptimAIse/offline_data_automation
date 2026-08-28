from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from core.portal_profiles import (
    PortalProfileConfigError,
    resolve_portal_profile_jobs,
)
from domains.download.service import DownloadOutcome, DownloadResult


def _eml_config() -> dict:
    return {
        "profiles": {
            "profile_1": {
                "user": "first-user",
                "password": "first-password",
                "file_station_search_path": "/V-Optimaise Data/",
                "modes": ["charge", "dpr", "rm_hm", "rm_stock", "ash"],
            },
            "profile_2": {
                "user": "second-user",
                "password": "second-password",
                "file_station_search_path": "/QC_LAB_DATA",
                "modes": ["rm", "fines_analysis", "hot_metal", "dust"],
            },
        }
    }


class PortalProfileRoutingTests(unittest.TestCase):
    def test_base_config_assigns_each_mode_to_the_required_profile(self):
        profiles = load_yaml("src/config/base.yaml")["eml"]["profiles"]

        self.assertEqual(
            set(profiles["profile_1"]["modes"]),
            {"charge", "dpr", "rm_hm", "rm_stock", "ash"},
        )
        self.assertEqual(
            set(profiles["profile_2"]["modes"]),
            {"rm", "fines_analysis", "hot_metal", "dust"},
        )
        self.assertEqual(
            profiles["profile_1"]["file_station_search_path"],
            "/V-Optimaise Data/",
        )
        self.assertEqual(
            profiles["profile_2"]["file_station_search_path"],
            "/QC_LAB_DATA",
        )

    def test_single_profile_modes_share_one_login_job(self):
        jobs = resolve_portal_profile_jobs(
            ["charge", "dpr", "ash"],
            _eml_config(),
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].name, "profile_1")
        self.assertEqual(jobs[0].file_station_search_path, "/V-Optimaise Data/")
        self.assertEqual(jobs[0].modes, ("charge", "dpr", "ash"))

    def test_mixed_modes_are_grouped_into_one_job_per_profile(self):
        jobs = resolve_portal_profile_jobs(
            ["rm", "charge", "hot_metal", "rm_stock"],
            _eml_config(),
        )

        self.assertEqual([job.name for job in jobs], ["profile_2", "profile_1"])
        self.assertEqual(jobs[0].file_station_search_path, "/QC_LAB_DATA")
        self.assertEqual(jobs[1].file_station_search_path, "/V-Optimaise Data/")
        self.assertEqual(jobs[0].modes, ("rm", "hot_metal"))
        self.assertEqual(jobs[1].modes, ("charge", "rm_stock"))

    def test_credentials_are_only_required_for_requested_profiles(self):
        eml_config = _eml_config()
        eml_config["profiles"]["profile_2"]["user"] = ""
        eml_config["profiles"]["profile_2"]["password"] = ""

        jobs = resolve_portal_profile_jobs(["dpr"], eml_config)

        self.assertEqual([job.name for job in jobs], ["profile_1"])

    def test_missing_credentials_identify_required_env_variables(self):
        eml_config = _eml_config()
        eml_config["profiles"]["profile_2"]["user"] = ""
        eml_config["profiles"]["profile_2"]["password"] = ""

        with self.assertRaisesRegex(
            PortalProfileConfigError,
            "EML_PROFILE_2_USER, EML_PROFILE_2_PASSWORD",
        ):
            resolve_portal_profile_jobs(["rm"], eml_config)

    def test_duplicate_mode_assignment_is_rejected(self):
        eml_config = _eml_config()
        eml_config["profiles"]["profile_2"]["modes"].append("dpr")

        with self.assertRaisesRegex(PortalProfileConfigError, "assigned to both"):
            resolve_portal_profile_jobs(["dpr"], eml_config)

    def test_missing_profile_search_path_is_rejected(self):
        eml_config = _eml_config()
        eml_config["profiles"]["profile_2"]["file_station_search_path"] = ""

        with self.assertRaisesRegex(
            PortalProfileConfigError,
            "Missing file_station_search_path.*profile_2",
        ):
            resolve_portal_profile_jobs(["rm"], eml_config)


class PortalProfileDownloadTests(unittest.TestCase):
    @patch("app.PortalDownloader")
    @patch("app.SeleniumClient")
    def test_mixed_profile_downloads_use_correct_logins_and_merge_results(
        self,
        selenium_client_cls,
        downloader_cls,
    ):
        from app import _download_for_profiles

        profile_2_client = Mock()
        profile_1_client = Mock()
        selenium_client_cls.side_effect = [profile_2_client, profile_1_client]

        profile_2_downloader = Mock()
        profile_1_downloader = Mock()
        downloader_cls.side_effect = [profile_2_downloader, profile_1_downloader]
        profile_2_downloader.download.return_value = DownloadResult(
            by_mode={"rm": DownloadOutcome(status="downloaded", paths=("rm.xlsx",))}
        )
        profile_1_downloader.download.return_value = DownloadResult(
            by_mode={
                "charge": DownloadOutcome(
                    status="downloaded",
                    paths=("charge.xlsx",),
                )
            }
        )

        cfg = {
            "download": {
                "default_timeout": 180,
                "download_dir": "downloads",
                "metadata_path": "metadata.json",
            },
            "eml": {
                **_eml_config(),
                "login_url": "https://portal.example/login",
                "file_station_url": "https://portal.example/files",
                "hourly_url": "https://portal.example/hourly",
            },
            "portal_files": {},
        }

        result = _download_for_profiles(
            modes=["rm", "charge"],
            run_dates=["28-Aug-2026"],
            is_today_mode=True,
            cfg=cfg,
            logger=Mock(),
        )

        profile_2_client.login.assert_called_once_with(
            login_url="https://portal.example/login",
            user="second-user",
            password="second-password",
        )
        profile_1_client.login.assert_called_once_with(
            login_url="https://portal.example/login",
            user="first-user",
            password="first-password",
        )
        profile_2_downloader.download.assert_called_once_with(
            modes=["rm"],
            run_dates=["28-Aug-2026"],
            is_today_mode=True,
        )
        profile_1_downloader.download.assert_called_once_with(
            modes=["charge"],
            run_dates=["28-Aug-2026"],
            is_today_mode=True,
        )
        profile_2_download_config = downloader_cls.call_args_list[0].args[1]
        profile_1_download_config = downloader_cls.call_args_list[1].args[1]
        self.assertEqual(
            profile_2_download_config.file_station_search_path,
            "/QC_LAB_DATA",
        )
        self.assertEqual(
            profile_1_download_config.file_station_search_path,
            "/V-Optimaise Data/",
        )
        self.assertEqual(set(result.by_mode), {"rm", "charge"})
        profile_2_client.stop.assert_called_once_with()
        profile_1_client.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
