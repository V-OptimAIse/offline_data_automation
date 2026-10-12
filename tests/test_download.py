from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.download.service import (
    DownloadConfig,
    SynologyDownloader,
    ash_expected_name,
    format_portal_filename,
    is_excel_lock_file,
    latest_matching_file,
    name_contains_identifier,
    normalize_name,
)
from infrastructure.synology_api_client import RemoteFile


LOGGER = logging.getLogger("test_download")


def remote(name: str, mtime: int, size: int = 100, folder: str = "/reports"):
    return RemoteFile(name, f"{folder}/{name}", size, mtime, False)


class FilenameMatchingTests(unittest.TestCase):
    def test_html_encoded_ampersand_and_punctuation_match(self):
        self.assertEqual(normalize_name("RM &amp; HM.xlsx"), "rm and hm.xlsx")
        self.assertTrue(name_contains_identifier("RM&HM 2027-28.xlsx", "RM & HM"))
        self.assertTrue(
            name_contains_identifier(
                "06 BF 02 HOT METAL SLAG &amp; GAS 2027-28.xlsx",
                "BF-02- HOT METAL, SLAG",
            )
        )

    def test_dynamic_names_and_extensions_match_stable_identifiers(self):
        examples = {
            "BF-02 BUNKER": "12 BF-02 BUNKER 2027-28 Rev 2.xlsx",
            "BF-01 BUNKER": "11 BF-01 BUNKER 2027-28 Rev 2.xlsx",
            "BF-02 DPR": "BF-02 DPR Sep'27.xlsx",
            "01 COKE OVEN": "01 COKE OVEN 2027-28.xlsm",
            "02 SP-02 PRODUCT": "02 SP-02 PRODUCT 2027-28.xlsx",
            "RM BULK STOCK": "RM BULK STOCK Sep'2027.xls",
            "GCP DUST CATCHER": "15 GCP DUST CATCHER ANALYSIS 27-28.xlsx",
            "ASH ANALYSIS": "ASH ANALYSIS 27-28 Final.xlsx",
        }
        for identifier, filename in examples.items():
            with self.subTest(identifier=identifier):
                self.assertTrue(name_contains_identifier(filename, identifier))

    def test_latest_match_uses_remote_mtime_and_ignores_lock_files(self):
        files = [
            remote("~$12 BF-02 BUNKER 2028-29.xlsx", 300),
            remote("12 BF-02 BUNKER 2026-27.xlsx", 100),
            remote("12 BF-02 BUNKER 2027-28.xlsx", 200),
        ]
        match = latest_matching_file(files, "BF-02 BUNKER")
        self.assertEqual(match.name, "12 BF-02 BUNKER 2027-28.xlsx")
        self.assertTrue(is_excel_lock_file(files[0].name))

    def test_ash_expected_financial_year_is_preferred(self):
        files = [
            remote("ASH ANALYSIS 27-28.xlsx", 300),
            remote("ASH ANALYSIS 26-27 Rev 2.xlsx", 200),
        ]
        expected = ash_expected_name("ASH ANALYSIS", "31-Jul-2026")
        match = latest_matching_file(
            files,
            "ASH ANALYSIS",
            expected_name=expected,
        )
        self.assertEqual(expected, "ASH ANALYSIS 26-27")
        self.assertEqual(match.name, "ASH ANALYSIS 26-27 Rev 2.xlsx")

    def test_financial_year_template_formatter(self):
        template = "ASH ANALYSIS {financial_year_short}"
        self.assertEqual(format_portal_filename(template, "31-Mar-2027"), "ASH ANALYSIS 26-27")
        self.assertEqual(format_portal_filename(template, "01-Apr-2027"), "ASH ANALYSIS 27-28")

    def test_base_config_keeps_stable_identifiers(self):
        portal_files = load_yaml("src/config/base.yaml")["portal_files"]
        self.assertEqual(portal_files["rm"], "BF-02 BUNKER")
        self.assertEqual(portal_files["rm_sinter"], "BF-01 BUNKER")
        self.assertEqual(portal_files["dust_chemical"], "GCP DUST CATCHER")


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.root = [
            remote("12 BF-02 BUNKER 2026-27.xlsx", 200),
            remote("11 BF-01 BUNKER 2026-27.xlsx", 200),
            remote("15 GCP DUST CATCHER ANALYSIS.xlsx", 200),
        ]

    def _config(self):
        return DownloadConfig(
            download_dir="downloads",
            folder_path="/QC_LAB_DATA",
            charge_folder_path="/V-Optimaise Data/BF2 AUTO REPORTS/CHARGE AND DUMP/HOURLY",
            portal_files={
                "rm": "BF-02 BUNKER",
                "rm_sinter": "BF-01 BUNKER",
                "fines_analysis": "BF-02 BUNKER",
                "dust_basic": "BF-02 BUNKER",
                "dust_chemical": "GCP DUST CATCHER",
            },
        )

    def test_shared_bf02_is_downloaded_once_and_fans_out(self):
        class Client:
            def __init__(self, files):
                self.files = files
                self.downloads = []
                self.lists = []

            def list_files(inner, folder):
                inner.lists.append(folder)
                return inner.files

            def download_file(inner, remote_path, local_path, **_kwargs):
                inner.downloads.append(remote_path)
                return str(Path(local_path).resolve())

        client = Client(self.root)
        result = SynologyDownloader(client, self._config(), LOGGER).download(
            ["rm", "fines_analysis", "dust"],
            ["10-Oct-2026"],
            True,
        )

        bf02 = "/reports/12 BF-02 BUNKER 2026-27.xlsx"
        self.assertEqual(client.lists, ["/QC_LAB_DATA"])
        self.assertEqual(client.downloads.count(bf02), 1)
        self.assertEqual(
            set(result.by_mode), {"rm", "fines_analysis", "dust"}
        )
        self.assertIn("12 BF-02 BUNKER 2026-27.xlsx", result.by_mode["rm"].paths[0])

    def test_charge_uses_nested_api_folder_and_today_name(self):
        expected_folder = self._config().charge_folder_path
        charge = remote(
            "CHARGE_AND_DUMP_REPORT_10_10_2026.xlsx",
            500,
            folder=expected_folder,
        )

        class Client:
            def __init__(self):
                self.folders = []

            def list_files(inner, folder):
                inner.folders.append(folder)
                return [charge]

            def download_file(inner, remote_path, local_path, **_kwargs):
                return str(Path(local_path).resolve())

        client = Client()
        result = SynologyDownloader(client, self._config(), LOGGER).download(
            ["charge"], ["10-Oct-2026"], False
        )
        self.assertEqual(client.folders, [expected_folder])
        self.assertEqual(result.by_mode["charge"].status, "downloaded")


if __name__ == "__main__":
    unittest.main()
