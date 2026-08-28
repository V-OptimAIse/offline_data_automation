from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infrastructure.selenium_client import SeleniumClient, SeleniumConfig


class SeleniumClientStartTests(unittest.TestCase):
    @patch("infrastructure.selenium_client.WebDriverWait")
    @patch("infrastructure.selenium_client.Service")
    @patch("infrastructure.selenium_client.webdriver.Chrome")
    @patch("infrastructure.selenium_client.which")
    def test_pi_chromium_runs_headless_without_keyring_password_prompt(
        self,
        which,
        chrome,
        service,
        webdriver_wait,
    ):
        paths = {
            "chromium": "/usr/bin/chromium",
            "chromedriver": "/usr/bin/chromedriver",
        }
        which.side_effect = paths.get
        chrome.return_value = Mock()
        client = SeleniumClient(SeleniumConfig())

        with patch.object(client, "_configure_timeouts"):
            client.start()

        options = chrome.call_args.kwargs["options"]
        self.assertEqual(options.binary_location, "/usr/bin/chromium")
        self.assertIn("--headless=new", options.arguments)
        self.assertIn("--password-store=basic", options.arguments)
        self.assertIn("--no-first-run", options.arguments)
        self.assertIn("--no-default-browser-check", options.arguments)
        self.assertEqual(
            options.experimental_options["prefs"],
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        service.assert_called_once_with("/usr/bin/chromedriver")
        self.assertTrue(client._headless)


if __name__ == "__main__":
    unittest.main()
