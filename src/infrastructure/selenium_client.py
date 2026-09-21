from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from shutil import which

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from urllib3.exceptions import ReadTimeoutError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeleniumConfig:
    default_timeout: int = 180
    page_load_timeout: int = 180
    script_timeout: int = 30
    command_timeout: int | None = None
    login_retries: int = 3

    @property
    def effective_command_timeout(self) -> int:
        return self.command_timeout or max(self.default_timeout + 60, 240)


class SeleniumClient:
    """
    Owns webdriver lifecycle + login.
    Based on existing working implementation. :contentReference[oaicite:6]{index=6}
    """

    def __init__(self, config: SeleniumConfig):
        self.config = config
        self.driver = None
        self.wait = None
        self._headless = False

    def start(self) -> None:
        chrome_options = Options()
        self._headless = False

        # Common options
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-first-run")
        chrome_options.add_argument("--no-default-browser-check")
        chrome_options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        chrome_options.page_load_strategy = "eager"
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        chromium_path = which("chromium-browser") or which("chromium")
        chromedriver_path = which("chromedriver")

        if chromium_path and chromedriver_path:
            #  Raspberry Pi / Linux server
            chrome_options.binary_location = chromium_path
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--window-size=1920,1080")
            # Avoid GNOME Keyring/KWallet unlock prompts in the automation-only
            # browser. Password saving is disabled by the preferences above.
            chrome_options.add_argument("--password-store=basic")
            self._headless = True

            service = Service(chromedriver_path)

        else:
            #  Laptop / fallback
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self._configure_timeouts()
        self.wait = WebDriverWait(self.driver, self.config.default_timeout, poll_frequency=1)

    def _configure_timeouts(self) -> None:
        if not self.driver:
            return

        self._set_command_timeout(self.config.effective_command_timeout)
        self.driver.set_page_load_timeout(self.config.page_load_timeout)
        self.driver.set_script_timeout(self.config.script_timeout)

    def _set_command_timeout(self, timeout: int) -> None:
        if not self.driver:
            return

        command_executor = getattr(self.driver, "command_executor", None)
        client_config = getattr(command_executor, "_client_config", None)
        if client_config is not None:
            client_config.timeout = timeout

    def _prepare_window(self) -> None:
        if not self.driver:
            return

        if self._headless:
            self.driver.set_window_size(1920, 1080)
            return

        try:
            self.driver.maximize_window()
        except WebDriverException:
            self.driver.set_window_size(1920, 1080)




    def login(self, login_url: str, user: str, password: str) -> None:
        max_attempts = max(1, int(self.config.login_retries))
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    self.stop()
                    time.sleep(2)

                if not self.driver or not self.wait:
                    self.start()

                self._login_once(login_url, user, password)
                return
            except (ReadTimeoutError, TimeoutException, WebDriverException) as exc:
                last_error = exc
                if attempt < max_attempts:
                    logger.warning(
                        "Portal login attempt %s/%s failed (%s); reconnecting "
                        "with a fresh browser session",
                        attempt,
                        max_attempts,
                        type(exc).__name__,
                    )

        self.stop()
        raise RuntimeError(
            f"Portal login failed after {max_attempts} attempts because "
            "Chrome/ChromeDriver stopped responding or the username/password "
            "fields did not appear. Operation stopped."
        ) from last_error

    def _login_once(self, login_url: str, user: str, password: str) -> None:
        if not self.driver or not self.wait:
            raise RuntimeError("SeleniumClient not started. Call start() first.")

        self._prepare_window()
        self.driver.get(login_url)

        # Same approach as your existing code. :contentReference[oaicite:7]{index=7}
        username_input = self.wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='text']")))
        password_input = self.wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@type='password']")))

        username_input.clear()
        username_input.send_keys(user)

        password_input.clear()
        password_input.send_keys(password + Keys.ENTER)

        time.sleep(4)

    def stop(self) -> None:
        driver = self.driver
        try:
            if not driver:
                return

            service = getattr(driver, "service", None)
            self._set_command_timeout(10)
            try:
                driver.quit()
            except Exception:
                if service is not None:
                    try:
                        service.stop()
                    except Exception:
                        pass
        finally:
            self.driver = None
            self.wait = None
