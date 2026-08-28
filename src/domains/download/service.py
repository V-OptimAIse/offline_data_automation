from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Set, List
from zoneinfo import ZoneInfo

from selenium.common.exceptions import (
    MoveTargetOutOfBoundsException,
    StaleElementReferenceException,
    WebDriverException,
)
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
    file_station_search_path: str | None = None
    portal_files: dict[str, str] | None = None


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


def format_portal_filename(template: str, run_date: str) -> str:
    """Render a portal filename template using the run date's financial year."""
    parsed = datetime.strptime(run_date, "%d-%b-%Y")
    start_year = parsed.year if parsed.month >= 4 else parsed.year - 1
    end_year = start_year + 1
    return str(template).format(
        year=parsed.year,
        year_short=f"{parsed.year % 100:02d}",
        financial_year=f"{start_year}-{end_year % 100:02d}",
        financial_year_short=(
            f"{start_year % 100:02d}-{end_year % 100:02d}"
        ),
        financial_year_start=start_year,
        financial_year_end=end_year,
    )


def _combine_download_outcomes(parts: list[DownloadOutcome]) -> DownloadOutcome:
    paths = tuple(dict.fromkeys(path for part in parts for path in part.paths))
    statuses = {part.status for part in parts}

    if statuses == {"downloaded"}:
        status = "downloaded"
    elif statuses == {"skipped"}:
        status = "skipped"
    elif statuses == {"failed"} or not paths:
        status = "failed"
    else:
        status = "partial"

    return DownloadOutcome(status=status, paths=paths)


# -------------------------------------------------
# DOWNLOADER
# -------------------------------------------------
class PortalDownloader:
    def __init__(self, selenium_client, cfg: DownloadConfig, logger):
        self.sc = selenium_client
        self.cfg = cfg
        self.logger = logger
        self._file_station_session_ready = False

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

    @staticmethod
    def _clean_portal_text(value: str | None) -> str:
        """Decode portal labels and collapse whitespace without changing the filename."""
        text = str(value or "")
        # Synology may expose an already encoded ext:qtip value (for example
        # ``RM &amp; HM.xlsx``). Decode repeatedly to also tolerate nested encoding.
        for _ in range(3):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        return " ".join(text.replace("\xa0", " ").split())

    def _normalize_name(self, name: str):
        normalized = self._clean_portal_text(name).casefold().replace("&", " and ")
        return " ".join(normalized.split())

    def _name_contains_identifier(self, name: str, identifier: str) -> bool:
        """Match a stable filename identifier despite punctuation or suffix changes."""
        name_norm = self._normalize_name(name)
        identifier_norm = self._normalize_name(identifier)
        if not identifier_norm:
            return False
        if identifier_norm in name_norm:
            return True

        name_tokens = re.findall(r"[a-z0-9]+", name_norm)
        identifier_tokens = re.findall(r"[a-z0-9]+", identifier_norm)
        width = len(identifier_tokens)
        return bool(width) and any(
            name_tokens[index : index + width] == identifier_tokens
            for index in range(len(name_tokens) - width + 1)
        )

    def _normalize_file_station_path(self, folder_path: str | None) -> str | None:
        if not folder_path or not folder_path.strip():
            return None

        normalized = folder_path.strip().replace("\\", "/")
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"

        return normalized

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

        if expected_norm and self._name_contains_identifier(filename, expected_name):
            return True

        expected_stem = os.path.splitext(expected_norm)[0]
        filename_stem = os.path.splitext(filename_norm)[0]
        if expected_stem and expected_stem in filename_stem:
            return True

        if keywords:
            return all(
                self._name_contains_identifier(filename, keyword)
                for keyword in keywords
            )

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
        rows = self.sc.driver.execute_script("""
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
        for row in rows:
            row["name"] = self._clean_portal_text(row.get("name"))
            row["modified"] = self._clean_portal_text(row.get("modified"))
        return rows

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

    def _visible_row_signature(self, panel=None) -> tuple[str, ...]:
        return tuple(
            row["name"].strip()
            for row in self._get_visible_rows(panel)
            if row["name"].strip()
        )[:8]

    def _current_file_station_location(self) -> str:
        return self.sc.driver.execute_script("""
            function isVisible(el) {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden';
            }

            function clean(text) {
                return (text || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
            }

            const selected = [...document.querySelectorAll(
                '.x-tree-selected, .x-tree-node-el.x-tree-selected'
            )].find(isVisible);
            if (selected) {
                const selectedText = clean(selected.innerText || selected.textContent);
                if (selectedText) {
                    return selectedText;
                }
            }

            const inputs = [...document.querySelectorAll('input')].filter(isVisible);
            for (const input of inputs) {
                const value = clean(input.value);
                const placeholder = clean(input.getAttribute('placeholder')).toLowerCase();
                if (value && !placeholder.includes('search') && value.toLowerCase() !== 'search') {
                    return value;
                }
            }

            return '';
        """)

    def _find_file_station_tree_item(self, folder_name: str):
        return self.sc.wait.until(
            lambda d: d.execute_script("""
                const folderName = arguments[0];
                const target = folderName.toLowerCase();

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden';
                }

                function clean(text) {
                    return (text || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                }

                const candidates = [...document.querySelectorAll([
                    '.x-tree-node-anchor span',
                    '.x-tree-node-anchor',
                    '.x-tree-node-el',
                    '[role="treeitem"]'
                ].join(','))]
                    .filter(isVisible)
                    .map(el => ({
                        el,
                        text: clean(el.innerText || el.textContent)
                    }))
                    .filter(item => item.text.toLowerCase() === target);

                if (!candidates.length) {
                    return null;
                }

                candidates.sort((a, b) => a.text.length - b.text.length);
                const el = candidates[0].el;
                return el.closest('.x-tree-node-el') ||
                    el.closest('.x-tree-node-anchor') ||
                    el;
            """, folder_name)
        )

    def _click_element(self, el) -> bool:
        metrics = self.sc.driver.execute_script("""
            const el = arguments[0];
            el.scrollIntoView({block: 'center', inline: 'nearest'});

            const rect = el.getBoundingClientRect();
            const left = Math.max(rect.left, 0);
            const right = Math.min(rect.right, window.innerWidth);
            const top = Math.max(rect.top, 0);
            const bottom = Math.min(rect.bottom, window.innerHeight);
            const width = right - left;
            const height = bottom - top;

            if (width < 3 || height < 3) {
                return {visible: false, rect};
            }

            return {
                visible: true,
                x: Math.floor(left + width / 2),
                y: Math.floor(top + height / 2),
                rect
            };
        """, el)

        if not metrics.get("visible"):
            return False

        x = metrics["x"]
        y = metrics["y"]

        try:
            for event in (
                {"type": "mouseMoved"},
                {"type": "mousePressed", "button": "left", "clickCount": 1},
                {"type": "mouseReleased", "button": "left", "clickCount": 1},
            ):
                self.sc.driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {"x": x, "y": y, **event},
                )
            return True
        except (AttributeError, WebDriverException) as exc:
            self.logger.warning(f"CDP click failed, using DOM fallback: {exc}")

        return bool(
            self.sc.driver.execute_script("""
                const el = arguments[0];
                for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        button: 0
                    }));
                }
                return true;
            """, el)
        )

    def _wait_for_file_station_folder(
        self,
        folder_name: str,
        previous_signature: tuple[str, ...],
    ):
        target = self._normalize_name(folder_name)
        return self.sc.wait.until(
            lambda d: d.execute_script("""
                const target = arguments[0];
                const previousSignature = arguments[1] || [];

                function isVisible(el) {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden';
                }

                function clean(text) {
                    return (text || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                }

                function normalize(text) {
                    return clean(text).toLowerCase().replace(/&/g, 'and');
                }

                const selectedMatches = [...document.querySelectorAll(
                    '.x-tree-selected, .x-tree-node-el.x-tree-selected'
                )].some(el => isVisible(el) && normalize(el.innerText || el.textContent).includes(target));

                const addressMatches = [...document.querySelectorAll('input')]
                    .filter(isVisible)
                    .some(input => normalize(input.value).includes(target));

                const rows = [...document.querySelectorAll(
                    '.x-grid3-body .x-grid3-row, .x-grid3-row'
                )]
                    .filter(isVisible)
                    .map(row => {
                        const cells = [...row.querySelectorAll('.x-grid3-cell-inner')];
                        const nameCell = row.querySelector('.x-grid3-cell-inner.x-grid3-col-filename') ||
                            cells[0] ||
                            row;
                        return clean(
                            nameCell.getAttribute('ext:qtip') ||
                            nameCell.innerText ||
                            nameCell.textContent
                        );
                    })
                    .filter(Boolean)
                    .slice(0, 8);

                const hasRows = rows.length > 0;
                const sameRows = rows.length === previousSignature.length &&
                    rows.every((name, index) => name === previousSignature[index]);

                return (selectedMatches || addressMatches) && hasRows && !sameRows;
            """, target, list(previous_signature))
        )

    def _select_file_station_folder(self, folder_path: str | None) -> bool:
        normalized_path = self._normalize_file_station_path(folder_path)
        if not normalized_path:
            return True

        folder_name = normalized_path.strip("/").split("/")[-1]
        current_location = self._current_file_station_location()
        if self._normalize_name(folder_name) in self._normalize_name(current_location):
            self.logger.info(f"File Station already in folder: {normalized_path}")
            return True

        self.logger.info(f"Selecting File Station folder: {normalized_path}")

        try:
            panel = self._find_visible_file_grid_panel()
            previous_signature = self._visible_row_signature(panel)
        except Exception:
            previous_signature = ()

        try:
            tree_item = self._find_file_station_tree_item(folder_name)
        except Exception as exc:
            self.logger.error(
                f"File Station folder not found in tree: {folder_name}. Error: {exc}"
            )
            return False

        if not tree_item:
            self.logger.error(f"File Station folder not found in tree: {folder_name}")
            return False

        if not self._click_element(tree_item):
            self.logger.error(f"Could not click File Station folder: {folder_name}")
            return False

        try:
            self._wait_for_file_station_folder(folder_name, previous_signature)
        except Exception as exc:
            self.logger.error(
                f"File Station folder did not load: {normalized_path}. Error: {exc}"
            )
            return False

        self.logger.info(f"File Station folder selected: {normalized_path}")
        return True

    def _click_grid_cell(self, cell, panel=None) -> bool:
        try:
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
        except StaleElementReferenceException:
            self.logger.warning(
                "File Station row changed before it could be clicked; retrying with a fresh row"
            )
            return False

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

    def _find_visible_row_by_identity(self, panel, target):
        """Return a fresh DOM row for a previously selected portal file."""
        target_name = self._normalize_name(target.get("name") or "")
        target_modified = self._clean_portal_text(target.get("modified"))
        for row in self._get_visible_rows(panel):
            if self._normalize_name(row.get("name") or "") != target_name:
                continue
            if (
                target_modified
                and self._clean_portal_text(row.get("modified")) != target_modified
            ):
                continue
            return row
        return None

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

    def _rewind_file_grid(self, panel) -> None:
        focus_el = self.sc.driver.execute_script("""
            const panel = arguments[0];
            const focusEl = panel.querySelector('.x-grid3-focus') || panel;
            panel.scrollTop = 0;
            focusEl.focus({preventScroll: true});
            panel.dispatchEvent(new WheelEvent('wheel', {
                deltaY: -100000,
                bubbles: true,
                cancelable: true,
                view: window
            }));
            return focusEl;
        """, panel)
        try:
            focus_el.send_keys(Keys.HOME)
        except WebDriverException:
            self.sc.driver.execute_script("""
                const target = arguments[0];
                for (const type of ['keydown', 'keyup']) {
                    target.dispatchEvent(new KeyboardEvent(type, {
                        key: 'Home',
                        code: 'Home',
                        keyCode: 36,
                        which: 36,
                        bubbles: true,
                        cancelable: true
                    }));
                }
            """, focus_el)
        time.sleep(0.6)

    def _restore_grid_row(self, panel, target, max_pages: int = 60):
        """Re-find a fallback row after virtual-grid paging invalidated its DOM node."""
        self._rewind_file_grid(panel)
        seen_signatures = set()

        for _ in range(max_pages):
            fresh_target = self._find_visible_row_by_identity(panel, target)
            if fresh_target:
                return fresh_target

            signature = self._visible_row_signature(panel)
            if signature in seen_signatures:
                break
            seen_signatures.add(signature)
            self._page_down_file_grid(panel)
            time.sleep(0.6)

        return None

    # -------------------------------------------------
    # FIND LATEST FILE
    # -------------------------------------------------
    def _find_latest_matching_file(
        self,
        rows,
        keywords: List[str],
        expected_name: str | None = None,
    ):
        exact_matches = []
        keyword_matches = []
        expected_norm = self._normalize_name(expected_name or "")

        for r in rows:
            name = self._normalize_name(r["name"])

            if not name.strip():
                continue

            dt = _parse_dt(r["modified"]) or datetime.min
            if expected_norm and self._name_contains_identifier(
                r["name"], expected_name or ""
            ):
                exact_matches.append((r, dt))
            elif keywords and all(
                self._name_contains_identifier(r["name"], keyword) for keyword in keywords
            ):
                keyword_matches.append((r, dt))

        matches = exact_matches or keyword_matches
        if not matches:
            return None

        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[0][0]

    def _find_latest_matching_file_in_grid(
        self,
        panel,
        keywords: List[str],
        expected_name: str | None = None,
        max_pages: int = 60,
    ):
        seen_rows = set()
        repeated_pages = 0
        fallback_target = None

        for page in range(max_pages):
            rows = self._get_visible_rows(panel)
            if expected_name:
                target = self._find_latest_matching_file(rows, [], expected_name)
                if fallback_target is None:
                    fallback = self._find_latest_matching_file(rows, keywords)
                    if fallback:
                        # Do not retain Selenium elements while paging a virtual grid.
                        fallback_target = {
                            "name": fallback["name"],
                            "modified": fallback["modified"],
                        }
            else:
                target = self._find_latest_matching_file(rows, keywords)
            if target:
                if page:
                    self.logger.info(
                        f"Required file found after paging File Station grid: {target['name']}"
                    )
                return target

            row_keys = {
                (str(row.get("name") or "").strip(), str(row.get("modified") or "").strip())
                for row in rows
                if str(row.get("name") or "").strip()
            }
            new_rows = row_keys - seen_rows
            seen_rows.update(row_keys)
            repeated_pages = repeated_pages + 1 if not new_rows else 0

            if page == 0 or (page + 1) % 10 == 0:
                self.logger.info(
                    "Required file not visible yet; paging through File Station grid "
                    f"({page + 1}/{max_pages})"
                )

            if panel is None or repeated_pages >= 3:
                break

            self._page_down_file_grid(panel)
            time.sleep(0.6)

        if fallback_target:
            self.logger.warning(
                f"Configured portal identifier {expected_name!r} was not found; "
                f"using keyword match {fallback_target['name']!r}"
            )
            restored_target = self._restore_grid_row(panel, fallback_target)
            if not restored_target:
                self.logger.error(
                    f"Could not restore keyword-matched row: {fallback_target['name']}"
                )
            return restored_target
        return None

    # -------------------------------------------------
    # DOWNLOAD WITH METADATA CHECK
    # -------------------------------------------------
    def _prepare_file_station_session(self, url: str):
        """Open and configure File Station once, then reuse its live file grid."""
        if self._file_station_session_ready:
            try:
                panel = self._find_visible_file_grid_panel()
                if self._wait_for_rows(panel, timeout=3):
                    self.logger.info("Reusing current File Station page")
                    return panel
            except WebDriverException as exc:
                self.logger.warning(
                    f"Current File Station page is unavailable; reopening it: {exc}"
                )

            self._file_station_session_ready = False

        self.logger.info("Opening File Station for profile download session")
        self.sc.driver.get(url)
        self.sc.wait.until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        self.logger.info("File Station page loaded")

        time.sleep(2)
        self.sc.wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "x-grid3-body"))
        )
        self.logger.info("File Station UI loaded")

        if not self._select_file_station_folder(self.cfg.file_station_search_path):
            return None

        try:
            self.logger.info("Sorting File Station grid by Modified Date if available")
            self.sc.driver.find_element(
                By.XPATH, "//span[contains(text(),'Modified Date')]"
            ).click()
            time.sleep(1)
        except Exception:
            pass

        try:
            panel = self._find_visible_file_grid_panel()
        except Exception as exc:
            self.logger.error(f"Could not locate File Station grid panel: {exc}")
            return None

        if not self._wait_for_rows(panel):
            self.logger.error("File list not loaded (timeout)")
            return None

        self._file_station_session_ready = True
        self.logger.info("File Station download session is ready")
        return panel

    def _download_latest_file(
        self,
        url: str,
        keywords: List[str],
        expected_name: str | None = None,
    ) -> DownloadOutcome:
        search_label = expected_name or keywords
        panel = self._prepare_file_station_session(url)
        if panel is None:
            return DownloadOutcome(status="failed")

        self.logger.info(f"Searching current File Station page for: {search_label}")
        self._rewind_file_grid(panel)
        rows = self._wait_for_rows(panel, timeout=3)
        if not rows:
            self.logger.error("File list unavailable in current File Station session")
            return DownloadOutcome(status="failed")

        self.logger.info(
            f"Finding required file using identifier={expected_name!r}, "
            f"keywords={keywords}"
        )
        target = self._find_latest_matching_file_in_grid(
            panel,
            keywords,
            expected_name,
        )

        if not target:
            self.logger.error(
                f"No file found for identifier={expected_name!r}, keywords={keywords}"
            )
            return DownloadOutcome(status="failed")

        metadata = self._load_metadata()
        name = self._normalize_name(target["name"])
        modified = target["modified"]

        self.logger.info(f"Required file found: {target['name']} | Modified: {modified}")

        prev_modified = metadata["root"].get(name)

        if prev_modified == modified:
            self.logger.info(f"SKIPPED (no change): {name}")
            return DownloadOutcome(status="skipped", portal_name=target["name"])

        self.logger.info(f"Preparing browser download: {target['name']}")

        start = time.time()
        fresh_target = self._find_visible_row_by_identity(panel, target)
        if fresh_target:
            target = fresh_target
        if not self._click_grid_cell(target.get("cell") or target["el"], panel):
            self.logger.error(f"Could not click file row: {target['name']}")
            return DownloadOutcome(status="failed", portal_name=target["name"])
        self.logger.info(f"Browser download initiated: {target['name']}")

        downloaded_path = self._wait_for_download(
            start,
            expected_name or name,
            keywords,
        )
        if not downloaded_path:
            self.logger.error("Download failed")
            return DownloadOutcome(status="failed", portal_name=target["name"])

        downloaded_name = os.path.basename(downloaded_path)
        if not self._download_name_matches(
            downloaded_name,
            expected_name or name,
            keywords,
        ):
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
    def _safe_download(self, url, keywords, expected_name: str | None = None):
        for attempt in range(1, 4):
            try:
                outcome = self._download_latest_file(url, keywords, expected_name)
            except WebDriverException as exc:
                self._file_station_session_ready = False
                self.logger.warning(
                    "Browser download attempt failed "
                    f"({attempt}/3) for {expected_name or keywords}: {exc}"
                )
                outcome = DownloadOutcome(status="failed")
            if outcome.status in ("downloaded", "skipped"):
                return outcome
            if attempt < 3:
                self.logger.warning(
                    f"Retrying download ({attempt + 1}/3) for {expected_name or keywords}"
                )
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
            search_path = self._normalize_file_station_path(
                self.cfg.file_station_search_path
            )
            if search_path:
                self.logger.info(f"Using File Station search folder: {search_path}")

            mode_keywords = {
                "rm": ["bf-02", "bunker"],
                "fines_analysis": ["bf-02", "bunker"],
                "dpr": ["bf-02", "dpr"],
                "hot_metal": ["bf-02", "hot", "metal"],
                "rm_hm": ["rm", "hm"],
                "rm_stock": ["bulk", "stock"],
            }

            keyword_results = {}

            def cached_download(keywords: list[str], expected_name: str | None):
                keyword_key = (*keywords, expected_name or "")
                if keyword_key in keyword_results:
                    self.logger.info(
                        "Using the portal download already resolved for another mode"
                    )
                    return keyword_results[keyword_key]

                outcome = self._safe_download(
                    self.cfg.file_station_url,
                    keywords,
                    expected_name,
                )
                keyword_results[keyword_key] = outcome
                return outcome

            for m in modes:
                if m == "charge":
                    continue

                if m == "rm_stock":
                    portal_files = self.cfg.portal_files or {}
                    parts = [
                        self._safe_download(
                            self.cfg.file_station_url,
                            ["bulk", "stock"],
                            portal_files.get("rm_stock"),
                        ),
                        self._safe_download(
                            self.cfg.file_station_url,
                            ["dpr", "sp#2"],
                            portal_files.get("rm_stock_sinter"),
                        ),
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

                if m == "ash":
                    template = (self.cfg.portal_files or {}).get(
                        "ash",
                        "ASH ANALYSIS {financial_year_short}",
                    )
                    expected_names = tuple(
                        dict.fromkeys(
                            format_portal_filename(template, run_date)
                            for run_date in run_dates
                        )
                    )
                    parts = [
                        cached_download(["ash", "analysis"], expected_name)
                        for expected_name in expected_names
                    ]
                    outcomes[m] = _combine_download_outcomes(parts)
                    continue

                if m == "dust":
                    portal_files = self.cfg.portal_files or {}
                    parts = [
                        cached_download(
                            ["bf-02", "bunker"],
                            portal_files.get("dust_basic"),
                        ),
                        cached_download(
                            [
                                "gcp",
                                "dust",
                                "catcher",
                                "esp",
                                "grate",
                                "bar",
                                "sample",
                                "analysis",
                            ],
                            portal_files.get("dust_chemical"),
                        ),
                    ]
                    outcomes[m] = _combine_download_outcomes(parts)
                    continue

                keywords = mode_keywords.get(m)
                if not keywords:
                    continue

                outcome = cached_download(
                    keywords,
                    (self.cfg.portal_files or {}).get(m),
                )

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
