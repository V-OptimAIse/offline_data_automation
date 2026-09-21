from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException, WebDriverException


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


class SeleniumClientLoginTests(unittest.TestCase):
    @staticmethod
    def _mock_browser_lifecycle(client: SeleniumClient):
        def start():
            client.driver = Mock()
            client.wait = Mock()

        def stop():
            client.driver = None
            client.wait = None

        client.start = Mock(side_effect=start)
        client.stop = Mock(side_effect=stop)

    @patch("infrastructure.selenium_client.time.sleep")
    def test_login_reconnects_and_succeeds_on_third_attempt(self, sleep):
        client = SeleniumClient(SeleniumConfig(login_retries=3))
        self._mock_browser_lifecycle(client)
        client._login_once = Mock(
            side_effect=[
                TimeoutException("login page unavailable"),
                WebDriverException("ChromeDriver disconnected"),
                None,
            ]
        )

        client.login("https://portal.example", "user", "password")

        self.assertEqual(client._login_once.call_count, 3)
        self.assertEqual(client.start.call_count, 3)
        self.assertEqual(client.stop.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    @patch("infrastructure.selenium_client.time.sleep")
    def test_login_retries_browser_start_failures(self, sleep):
        client = SeleniumClient(SeleniumConfig(login_retries=3))
        start_attempts = 0

        def start():
            nonlocal start_attempts
            start_attempts += 1
            if start_attempts < 3:
                raise WebDriverException("ChromeDriver unavailable")
            client.driver = Mock()
            client.wait = Mock()

        def stop():
            client.driver = None
            client.wait = None

        client.start = Mock(side_effect=start)
        client.stop = Mock(side_effect=stop)
        client._login_once = Mock()

        client.login("https://portal.example", "user", "password")

        self.assertEqual(client.start.call_count, 3)
        self.assertEqual(client._login_once.call_count, 1)
        self.assertEqual(client.stop.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    @patch("infrastructure.selenium_client.time.sleep")
    def test_login_stops_after_three_failed_attempts(self, sleep):
        client = SeleniumClient(SeleniumConfig(login_retries=3))
        self._mock_browser_lifecycle(client)
        client._login_once = Mock(
            side_effect=TimeoutException("login page unavailable")
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Portal login failed after 3 attempts.*Operation stopped",
        ):
            client.login("https://portal.example", "user", "password")

        self.assertEqual(client._login_once.call_count, 3)
        self.assertEqual(client.start.call_count, 3)
        self.assertEqual(client.stop.call_count, 3)
        self.assertIsNone(client.driver)
        self.assertIsNone(client.wait)


if __name__ == "__main__":
    unittest.main()
