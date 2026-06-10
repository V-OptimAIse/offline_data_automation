from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, List
from zoneinfo import ZoneInfo

from selenium.common.exceptions import MoveTargetOutOfBoundsException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
CHARGE_DOWNLOAD_RETRIES = 5


# -------------------------------------------------
# CONFIG
# -------------------------------------------------
@dataclass(frozen=True)
class DownloadConfig:
    download_dir: str
    metadata_path: str
    file_station_url: str
    hourly_url: str


@dataclass(frozen=True)
class DownloadOutcome:
    status: str
    paths: tuple[str, ...] = ()
    portal_name: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    by_mode: Dict[str, DownloadOutcome]

    @property
    def downloaded_modes(self) -> Set[str]:
        return {
            mode
            for mode, outcome in self.by_mode.items()
            if outcome.status == "downloaded"
        }

    @property
    def skipped_modes(self) -> Set[str]:
        return {
            mode
            for mode, outcome in self.by_mode.items()
            if outcome.status == "skipped"
        }

    @property
    def failed_modes(self) -> Set[str]:
        return {
            mode
            for mode, outcome in self.by_mode.items()
            if outcome.status == "failed"
        }

    @property
    def partial_modes(self) -> Set[str]:
        return {
            mode
            for mode, outcome in self.by_mode.items()
            if outcome.status == "partial"
        }


# -------------------------------------------------
# UTILS
# -------------------------------------------------
def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except Exception:
            pass
    return None


# -------------------------------------------------
# DOWNLOADER
# -------------------------------------------------
class PortalDownloader:
    def __init__(self, selenium_client, cfg: DownloadConfig, logger):
        self.sc = selenium_client
        self.cfg = cfg
        self.logger = logger

    # -------------------------------------------------
    # METADATA
    # -------------------------------------------------
    def _load_metadata(self):
        if not os.path.exists(self.cfg.metadata_path):
            return {"root": {}, "hourly": {}}
        with open(self.cfg.metadata_path, "r") as f:
            return json.load(f)

    def _save_metadata(self, data):
        with open(self.cfg.metadata_path, "w") as f:
            json.dump(data, f, indent=2)

    def _normalize_name(self, name: str):
        return (
            name.lower()
            .replace("&", "and")
            .replace("\xa0", " ")
            .replace("  ", " ")
            .strip()
        )

    def _wait_for_rows(self, root=None, timeout=15):
        for _ in range(timeout):
            rows = self._get_visible_rows(root)
            if rows and any(r["name"].strip() for r in rows):
                return rows
            time.sleep(1)
        return []

    # -------------------------------------------------
    # WAIT FOR DOWNLOAD
    # -------------------------------------------------
    def _download_name_matches(
        self,
        filename: str,
        expected_name: str,
        keywords: List[str] | None = None,
    ) -> bool:
        filename_norm = self._normalize_name(filename)
        expected_norm = self._normalize_name(expected_name)

        if expected_norm and expected_norm in filename_norm:
            return True

        expected_stem = os.path.splitext(expected_norm)[0]
        filename_stem = os.path.splitext(filename_norm)[0]
        if expected_stem and expected_stem in filename_stem:
            return True

        if keywords:
            keyword_norms = [self._normalize_name(k) for k in keywords]
            return all(k in filename_norm for k in keyword_norms)

        return False

    def _wait_for_download(
        self,
        started_at: float,
        expected_name: str,
        keywords: List[str] | None = None,
        timeout: int = 240,
    ) -> str | None:
        end = time.time() + timeout
        d = os.path.expanduser(self.cfg.download_dir)

        while time.time() < end:
            files = os.listdir(d)

            for f in files:
                # ignore temp files
                if f.endswith((".crdownload", ".tmp", ".part")):
                    continue

                # must match expected file
                if self._download_name_matches(f, expected_name, keywords):
                    p = os.path.join(d, f)

                    if os.path.getmtime(p) < started_at - 2:
                        continue

                    # ensure file is stable (size not changing)
                    size1 = os.path.getsize(p)
                    time.sleep(1)
                    size2 = os.path.getsize(p)

                    if size1 == size2:
                        return str(Path(p).resolve())

            time.sleep(1)

        return None

    # -------------------------------------------------
    # GET ROWS
    # -------------------------------------------------
    def _get_visible_rows(self, root=None):
        return self.sc.driver.execute_script("""
            const root = arguments[0] || document;
            const rows = [...root.querySelectorAll('.x-grid3-body .x-grid3-row, .x-grid3-row')];

            return rows.map(r=>{
                const cells = [...r.querySelectorAll('.x-grid3-cell-inner')];
                const nameCell = r.querySelector('.x-grid3-cell-inner.x-grid3-col-filename') || cells[0] || r;
                const name = (
                    nameCell.getAttribute('ext:qtip') ||
                    nameCell.innerText ||
                    (cells[0] && cells[0].innerText) ||
                    ''
                ).replace(/\\u00a0/g, ' ').trim();

                return {
                    el: r,
                    cell: nameCell,
                    name,
                    modified: (cells[3] && cells[3].innerText.trim()) || ''
                };
            }).filter(r => r.name || r.modified);
        """, root)

    def _find_visible_file_grid_panel(self):
        return self.sc.wait.until(
            lambda d: d.execute_script("""
                const panels = [
                    ...document.querySelectorAll(
                        '.syno-sds-fs-grid-scroller.x-grid3-scroller, .x-grid3-scroller'
                    )
                ];

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 50 &&
                        rect.height > 50 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        !!el.querySelector('.x-grid3-body');
                }

                const visiblePanels = panels.filter(isVisible);
                return visiblePanels.find(p => p.classList.contains('syno-sds-fs-grid-scroller')) ||
                    visiblePanels.find(p => p.querySelector('.webfm-file-type-icon')) ||
                    visiblePanels[0] ||
                    null;
            """)
        )

    def _click_grid_cell(self, cell, panel=None) -> bool:
        metrics = self.sc.driver.execute_script("""
            const el = arguments[0];
            const panel = arguments[1];

            el.scrollIntoView({block: 'center', inline: 'nearest'});

            const rect = el.getBoundingClientRect();
            const panelRect = panel ? panel.getBoundingClientRect() : {
                left: 0,
                top: 0,
                right: window.innerWidth,
                bottom: window.innerHeight
            };

            const left = Math.max(rect.left, panelRect.left, 0);
            const right = Math.min(rect.right, panelRect.right, window.innerWidth);
            const top = Math.max(rect.top, panelRect.top, 0);
            const bottom = Math.min(rect.bottom, panelRect.bottom, window.innerHeight);
            const width = right - left;
            const height = bottom - top;

            if (width < 3 || height < 3) {
                return {visible: false, rect, panelRect};
            }

            return {
                visible: true,
                x: Math.floor(left + Math.min(24, width / 2)),
                y: Math.floor(top + height / 2),
                rect,
                panelRect
            };
        """, cell, panel)

        if not metrics.get("visible"):
            return False

        x = metrics["x"]
        y = metrics["y"]

        try:
            for event in (
                {"type": "mouseMoved"},
                {"type": "mousePressed", "button": "left", "clickCount": 1},
                {"type": "mouseReleased", "button": "left", "clickCount": 1},
                {"type": "mousePressed", "button": "left", "clickCount": 2},
                {"type": "mouseReleased", "button": "left", "clickCount": 2},
            ):
                self.sc.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"x": x, "y": y, **event},
                )
            return True
        except (AttributeError, WebDriverException) as exc:
            self.logger.warning(f"CDP double-click failed, using DOM fallback: {exc}")

        return bool(
            self.sc.driver.execute_script("""
                const el = arguments[0];
                const events = [
                    'mouseover', 'mousemove',
                    'mousedown', 'mouseup', 'click',
                    'mousedown', 'mouseup', 'click',
                    'dblclick'
                ];

                for (const type of events) {
                    el.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        button: 0
                    }));
                }
                return true;
            """, cell)
        )

    def _page_down_file_grid(self, panel) -> None:
        focus_el = self.sc.driver.execute_script("""
            const panel = arguments[0];
            const focusEl = panel.querySelector('.x-grid3-focus') || panel;
            focusEl.focus({preventScroll: true});
            panel.dispatchEvent(new WheelEvent('wheel', {
                deltaY: Math.max(500, panel.clientHeight * 0.85),
                bubbles: true,
                cancelable: true,
                view: window
            }));
            return focusEl;
        """, panel)

        try:
            focus_el.send_keys(Keys.PAGE_DOWN)
        except WebDriverException:
            self.sc.driver.execute_script("""
                const target = arguments[0];
                for (const type of ['keydown', 'keyup']) {
                    target.dispatchEvent(new KeyboardEvent(type, {
                        key: 'PageDown',
                        code: 'PageDown',
                        keyCode: 34,
                        which: 34,
                        bubbles: true,
                        cancelable: true
                    }));
                }
            """, focus_el)

    # -------------------------------------------------
    # FIND LATEST FILE
    # -------------------------------------------------
    def _find_latest_matching_file(self, rows, keywords: List[str]):
        matches = []
        keyword_norms = [self._normalize_name(k) for k in keywords]

        for r in rows:
            name = self._normalize_name(r["name"])

            if not name.strip():
                continue

            if all(k in name for k in keyword_norms):
                dt = _parse_dt(r["modified"]) or datetime.min
                matches.append((r, dt))

        if not matches:
            return None

        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[0][0]

    # -------------------------------------------------
    # DOWNLOAD WITH METADATA CHECK
    # -------------------------------------------------
    def _download_latest_file(self, url: str, keywords: List[str]) -> DownloadOutcome:
        self.logger.info(f"Opening File Station for keyword search: {keywords}")

        self.sc.driver.get(url)
        self.sc.wait.until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        self.logger.info("File Station page loaded")

        time.sleep(2)

        try:
            self.logger.info("Sorting File Station grid by Modified Date if available")
            self.sc.driver.find_element(
                By.XPATH, "//span[contains(text(),'Modified Date')]"
            ).click()
            time.sleep(1)
        except Exception:
            pass

        self.sc.wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "x-grid3-body"))
        )
        self.logger.info("File Station grid is ready; reading visible rows")

        try:
            panel = self._find_visible_file_grid_panel()
        except Exception:
            panel = None

        rows = self._wait_for_rows(panel)

        if not rows:
            self.logger.error("File list not loaded (timeout)")
            return DownloadOutcome(status="failed")

        self.logger.info(f"Finding required file using keywords: {keywords}")
        target = self._find_latest_matching_file(rows, keywords)

        if not target:
            self.logger.error(f"No file found for {keywords}")
            return DownloadOutcome(status="failed")

        metadata = self._load_metadata()
        name = self._normalize_name(target["name"])
        modified = target["modified"]

        self.logger.info(f"Required file found: {target['name']} | Modified: {modified}")

        prev_modified = metadata["root"].get(name)

        if prev_modified == modified:
            self.logger.info(f"SKIPPED (no change): {name}")
            return DownloadOutcome(status="skipped", portal_name=target["name"])

        self.logger.info(f"Starting browser download: {target['name']}")

        start = time.time()
        if not self._click_grid_cell(target.get("cell") or target["el"], panel):
            self.logger.error(f"Could not click file row: {target['name']}")
            return DownloadOutcome(status="failed", portal_name=target["name"])

        downloaded_path = self._wait_for_download(start, name, keywords)
        if not downloaded_path:
            self.logger.error("Download failed")
            return DownloadOutcome(status="failed", portal_name=target["name"])

        downloaded_name = os.path.basename(downloaded_path).lower()
        if not all(k in downloaded_name for k in keywords):
            self.logger.error("Downloaded file mismatch")
            return DownloadOutcome(status="failed", portal_name=target["name"])

        metadata["root"][name] = modified
        self._save_metadata(metadata)

        self.logger.info(f"Metadata updated for: {name}")

        return DownloadOutcome(
            status="downloaded",
            paths=(downloaded_path,),
            portal_name=target["name"],
        )

    # -------------------------------------------------
    # RETRY WRAPPER
    # -------------------------------------------------
    def _safe_download(self, url, keywords):
        for attempt in range(3):
            outcome = self._download_latest_file(url, keywords)
            if outcome.status in ("downloaded", "skipped"):
                return outcome
            self.logger.warning(f"Retry {attempt+1}/3 for {keywords}")
            time.sleep(3)
        return DownloadOutcome(status="failed")

    def _charge_name_matches(self, name: str, stem: str) -> bool:
        return name == f"{stem}.xlsx" or (
            name.startswith(stem) and name.lower().endswith(".xlsx")
        )

    def _find_visible_charge_row(self, panel, stem: str):
        for row in self._get_visible_rows(panel):
            name = row["name"].strip()
            if name and self._charge_name_matches(name, stem):
                return row
        return None

    def _download_charge_file_with_retry(self, panel, stem: str) -> tuple[str | None, str]:
        last_name = f"{stem}.xlsx"

        for attempt in range(1, CHARGE_DOWNLOAD_RETRIES + 1):
            try:
                panel = self._find_visible_file_grid_panel()
            except Exception:
                pass

            row = self._find_visible_charge_row(panel, stem)
            if not row:
                self.logger.warning(
                    "Charge file row not visible during download retry "
                    f"({attempt}/{CHARGE_DOWNLOAD_RETRIES}): {last_name}"
                )
                self._page_down_file_grid(panel)
                time.sleep(0.6)
                continue

            name = row["name"].strip()
            last_name = name

            try:
                self.logger.info(
                    f"Starting browser download: {name} "
                    f"(attempt {attempt}/{CHARGE_DOWNLOAD_RETRIES})"
                )
                start = time.time()

                if not self._click_grid_cell(row.get("cell") or row["el"], panel):
                    raise WebDriverException(f"Could not click charge file: {name}")

                downloaded_path = self._wait_for_download(start, name)
                if downloaded_path:
                    if attempt > 1:
                        self.logger.info(
                            f"Charge file downloaded after retry {attempt}: {name}"
                        )
                    return downloaded_path, name

                self.logger.warning(
                    f"Download attempt timed out ({attempt}/{CHARGE_DOWNLOAD_RETRIES}): {name}"
                )
            except MoveTargetOutOfBoundsException as exc:
                self.logger.warning(
                    "Move target out of bounds while downloading charge file "
                    f"({attempt}/{CHARGE_DOWNLOAD_RETRIES}): {name}. Retrying. "
                    f"Error: {exc}"
                )
            except WebDriverException as exc:
                self.logger.warning(
                    "Browser click/download attempt failed for charge file "
                    f"({attempt}/{CHARGE_DOWNLOAD_RETRIES}): {name}. Retrying. "
                    f"Error: {exc}"
                )

            time.sleep(min(attempt, 3))

        return None, last_name

    # -------------------------------------------------
    # CHARGE
    # -------------------------------------------------
    def _scroll_and_download_charge(self, url: str, run_dates: list) -> DownloadOutcome:
        self.logger.info("Opening File Station charge folder")
        self.sc.driver.get(url)
        self.sc.wait.until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        self.logger.info("File Station charge folder loaded")
        time.sleep(1)

        pending_stems = set()
        for rd in run_dates:
            dt = datetime.strptime(rd, "%d-%b-%Y")
            pending_stems.add(
                f"CHARGE_AND_DUMP_REPORT_{dt.day}_{dt.month}_{dt.year}"
            )

        required_files = ", ".join(sorted(stem + ".xlsx" for stem in pending_stems))
        self.logger.info(f"Finding required charge file(s): {required_files}")
        self.logger.info("Using File Station UI download path for charge files")

        try:
            panel = self._find_visible_file_grid_panel()
        except Exception:
            self.logger.error("Could not locate File Station grid panel.")
            return DownloadOutcome(status="failed")
        self.logger.info("File Station grid is ready; scanning visible rows")

        downloaded_paths: list[str] = []
        failed: Set[str] = set()

        for attempt in range(60):
            if not pending_stems:
                break

            rows = self._get_visible_rows(panel)

            matched_on_page = False
            for r in rows:
                name = r["name"].strip()
                if not name:
                    continue

                stem = next(
                    (s for s in pending_stems if self._charge_name_matches(name, s)),
                    None,
                )
                if not stem:
                    continue

                self.logger.info(f"Required charge file found: {name}")

                downloaded_path, downloaded_name = self._download_charge_file_with_retry(
                    panel, stem
                )
                if downloaded_path:
                    self.logger.info(f"Download completed: {downloaded_name}")
                    downloaded_paths.append(downloaded_path)
                else:
                    self.logger.error(
                        f"Download failed after {CHARGE_DOWNLOAD_RETRIES} attempts: "
                        f"{downloaded_name}"
                    )
                    failed.add(downloaded_name)

                pending_stems.remove(stem)
                matched_on_page = True

            if matched_on_page:
                continue

            if attempt == 0 or (attempt + 1) % 10 == 0:
                self.logger.info(
                    "Required charge file not visible yet; paging through lazy-loaded grid "
                    f"({attempt + 1}/60)"
                )
            self._page_down_file_grid(panel)
            time.sleep(0.6)

        if pending_stems:
            for stem in sorted(pending_stems):
                self.logger.warning(f"Charge file not found after scrolling: {stem}.xlsx")

        if downloaded_paths and not failed and not pending_stems:
            status = "downloaded"
        elif downloaded_paths:
            status = "partial"
        else:
            status = "failed"

        return DownloadOutcome(status=status, paths=tuple(downloaded_paths))

    # -------------------------------------------------
    # MAIN ENTRY
    # -------------------------------------------------
    def download(
        self,
        modes: list[str],
        run_dates: list[str],
        is_today_mode: bool,
    ) -> DownloadResult:
        outcomes: Dict[str, DownloadOutcome] = {}

        try:
            mode_keywords = {
                "rm": ["bf-02", "bunker"],
                "fines_analysis": ["bf-02", "bunker"],
                "dpr": ["bf-02", "dpr"],
                "hot_metal": ["bf-02", "hot", "metal"],
                "rm_hm": ["rm", "hm"],
                "rm_stock": ["bulk", "stock"],
            }

            keyword_results = {}
            for m in modes:
                if m == "charge":
                    continue

                if m == "rm_stock":
                    parts = [
                        self._safe_download(self.cfg.file_station_url, ["bulk", "stock"]),
                        self._safe_download(self.cfg.file_station_url, ["dpr", "sp#2"]),
                    ]
                    paths = tuple(path for part in parts for path in part.paths)
                    statuses = {part.status for part in parts}
                    status = (
                        "failed"
                        if statuses == {"failed"}
                        else "partial"
                        if "failed" in statuses
                        else "downloaded"
                        if "downloaded" in statuses
                        else "skipped"
                    )
                    outcomes[m] = DownloadOutcome(status=status, paths=paths)
                    continue

                keywords = mode_keywords.get(m)
                if not keywords:
                    continue

                keyword_key = tuple(keywords)
                if keyword_key in keyword_results:
                    outcome = keyword_results[keyword_key]
                    self.logger.info(f"{m} uses the same portal file as an earlier mode")
                else:
                    outcome = self._safe_download(self.cfg.file_station_url, keywords)
                    keyword_results[keyword_key] = outcome

                outcomes[m] = outcome
                if outcome.status == "failed":
                    self.logger.warning(f"{m} download failed; processing will be skipped")
                elif outcome.status == "skipped":
                    self.logger.info(f"{m} skipped (no update)")

            # ---------------- CHARGE ----------------
            if "charge" in modes:
                charge_dates = (
                    [datetime.now(BUSINESS_TZ).strftime("%d-%b-%Y")]
                    if is_today_mode
                    else run_dates
                )
                outcomes["charge"] = self._scroll_and_download_charge(
                    self.cfg.hourly_url,
                    charge_dates,
                )

            return DownloadResult(by_mode=outcomes)
        finally:
            self.sc.stop()
