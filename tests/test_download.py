from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import StaleElementReferenceException


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.config_loader import load_yaml
from domains.download.service import DownloadOutcome, PortalDownloader


LOGGER = logging.getLogger("test_download")


class PortalFilenameMatchingTests(unittest.TestCase):
    def setUp(self):
        self.downloader = PortalDownloader(None, None, LOGGER)

    def test_html_encoded_ampersand_matches_stable_identifier(self):
        rows = [
            {
                "name": "RM &amp; HM.xlsx",
                "modified": "08/20/2026 14:19:18",
            }
        ]

        match = self.downloader._find_latest_matching_file(
            rows,
            ["rm", "hm"],
            "RM & HM",
        )

        self.assertIs(match, rows[0])
        self.assertEqual(
            self.downloader._normalize_name("RM &amp; HM.xlsx"),
            "rm and hm.xlsx",
        )
        self.assertTrue(
            self.downloader._name_contains_identifier("RM&HM 2027-28.xlsx", "RM & HM")
        )

    def test_dynamic_filenames_match_stable_identifiers(self):
        examples = {
            "BF-02 BUNKER": "12 BF-02 BUNKER 2027-28 Rev 2.xlsx",
            "BF-02 DPR": "BF-02 DPR Sep'27.xlsx",
            "RM & HM": "RM &amp; HM 2027-28.xlsx",
            "RM BULK STOCK": "RM BULK STOCK Sep'2027.xls",
            "GCP DUST CATCHER": (
                "15 GCP DUST CATCHER ESP GRATE BAR SAMPLE ANALYSIS 27-28.xlsx"
            ),
            "ASH ANALYSIS": "ASH ANALYSIS 27-28 Final.xlsx",
            "BF-02- HOT METAL, SLAG": (
                "06 BF 02 HOT METAL SLAG &amp; GAS 2027-28.xlsx"
            ),
        }

        for identifier, filename in examples.items():
            with self.subTest(identifier=identifier):
                self.assertTrue(
                    self.downloader._name_contains_identifier(filename, identifier)
                )

    def test_base_config_uses_stable_identifiers(self):
        portal_files = load_yaml("src/config/base.yaml")["portal_files"]

        self.assertEqual(portal_files["rm"], "BF-02 BUNKER")
        self.assertEqual(portal_files["dpr"], "BF-02 DPR")
        self.assertEqual(portal_files["rm_hm"], "RM & HM")
        self.assertEqual(portal_files["rm_stock"], "RM BULK STOCK")
        self.assertEqual(portal_files["dust_chemical"], "GCP DUST CATCHER")
        self.assertEqual(portal_files["ash"], "ASH ANALYSIS")
        self.assertEqual(portal_files["hot_metal"], "BF-02- HOT METAL, SLAG")


class PortalDownloadRetryTests(unittest.TestCase):
    @patch("domains.download.service.time.sleep")
    def test_keyword_fallback_restores_a_fresh_virtual_grid_row(self, sleep):
        downloader = PortalDownloader(None, None, LOGGER)
        stale_row = {
            "name": "RM & HM.xlsx",
            "modified": "08/20/2026 14:19:18",
            "cell": "stale-cell",
            "el": "stale-row",
        }
        fresh_row = {**stale_row, "cell": "fresh-cell", "el": "fresh-row"}
        downloader._get_visible_rows = Mock(return_value=[stale_row])
        downloader._page_down_file_grid = Mock()
        downloader._restore_grid_row = Mock(return_value=fresh_row)

        result = downloader._find_latest_matching_file_in_grid(
            "panel",
            ["rm", "hm"],
            "a deliberately different identifier",
        )

        self.assertIs(result, fresh_row)
        restored_descriptor = downloader._restore_grid_row.call_args.args[1]
        self.assertEqual(
            restored_descriptor,
            {
                "name": "RM & HM.xlsx",
                "modified": "08/20/2026 14:19:18",
            },
        )

    @patch("domains.download.service.time.sleep")
    def test_stale_element_retries_the_complete_download_flow(self, sleep):
        downloader = PortalDownloader(None, None, LOGGER)
        downloader._download_latest_file = Mock(
            side_effect=[
                StaleElementReferenceException("row was redrawn"),
                DownloadOutcome(status="downloaded", paths=("download.xlsx",)),
            ]
        )

        outcome = downloader._safe_download(
            "https://file-station.example",
            ["rm", "hm"],
            "RM & HM",
        )

        self.assertEqual(outcome.status, "downloaded")
        self.assertEqual(downloader._download_latest_file.call_count, 2)
        sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
