from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Set
from zoneinfo import ZoneInfo

from infrastructure.synology_api_client import RemoteFile, SynologyApiClient


BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
EXCEL_LOCK_FILE_PREFIX = "~$"
EXCEL_EXTENSIONS = (".xls", ".xlsx", ".xlsm", ".xlsb")


@dataclass(frozen=True)
class DownloadConfig:
    download_dir: str
    folder_path: str
    charge_folder_path: str
    portal_files: dict[str, str]


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
        return {m for m, value in self.by_mode.items() if value.status == "downloaded"}

    @property
    def skipped_modes(self) -> Set[str]:
        return {m for m, value in self.by_mode.items() if value.status == "skipped"}

    @property
    def failed_modes(self) -> Set[str]:
        return {m for m, value in self.by_mode.items() if value.status == "failed"}

    @property
    def partial_modes(self) -> Set[str]:
        return {m for m, value in self.by_mode.items() if value.status == "partial"}


def is_excel_lock_file(filename: str | os.PathLike[str]) -> bool:
    basename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    return basename.startswith(EXCEL_LOCK_FILE_PREFIX)


def format_portal_filename(template: str, run_date: str) -> str:
    parsed = datetime.strptime(run_date, "%d-%b-%Y")
    start_year = parsed.year if parsed.month >= 4 else parsed.year - 1
    end_year = start_year + 1
    return str(template).format(
        year=parsed.year,
        year_short=f"{parsed.year % 100:02d}",
        financial_year=f"{start_year}-{end_year % 100:02d}",
        financial_year_short=f"{start_year % 100:02d}-{end_year % 100:02d}",
        financial_year_start=start_year,
        financial_year_end=end_year,
    )


def ash_expected_name(identifier: str, run_date: str) -> str:
    rendered = format_portal_filename(identifier, run_date)
    if "{" in identifier:
        return rendered
    parsed = datetime.strptime(run_date, "%d-%b-%Y")
    start_year = parsed.year if parsed.month >= 4 else parsed.year - 1
    return f"{rendered} {start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def charge_expected_name(run_date: str) -> str:
    parsed = datetime.strptime(run_date, "%d-%b-%Y")
    return f"CHARGE_AND_DUMP_REPORT_{parsed.day}_{parsed.month}_{parsed.year}"


def clean_remote_name(value: str | None) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.replace("\xa0", " ").split())


def normalize_name(value: str | None) -> str:
    return " ".join(
        clean_remote_name(value).casefold().replace("&", " and ").split()
    )


def name_contains_identifier(name: str, identifier: str) -> bool:
    name_normalized = normalize_name(name)
    identifier_normalized = normalize_name(identifier)
    if not identifier_normalized:
        return False
    if identifier_normalized in name_normalized:
        return True
    name_tokens = re.findall(r"[a-z0-9]+", name_normalized)
    identifier_tokens = re.findall(r"[a-z0-9]+", identifier_normalized)
    width = len(identifier_tokens)
    return bool(width) and any(
        name_tokens[index : index + width] == identifier_tokens
        for index in range(len(name_tokens) - width + 1)
    )


def is_remote_excel_file(remote: RemoteFile) -> bool:
    return (
        bool(remote.name)
        and not remote.isdir
        and not is_excel_lock_file(remote.name)
        and remote.name.casefold().endswith(EXCEL_EXTENSIONS)
    )


def latest_matching_file(
    files: Iterable[RemoteFile],
    identifier: str,
    *,
    expected_name: str | None = None,
) -> RemoteFile | None:
    matches = [
        item
        for item in files
        if is_remote_excel_file(item)
        and name_contains_identifier(item.name, identifier)
    ]
    if expected_name:
        expected_matches = [
            item for item in matches if name_contains_identifier(item.name, expected_name)
        ]
        if expected_matches:
            matches = expected_matches
    if not matches:
        return None
    return max(matches, key=lambda item: (item.mtime or 0, item.path))


def _combine_download_outcomes(parts: list[DownloadOutcome]) -> DownloadOutcome:
    paths = tuple(dict.fromkeys(path for part in parts for path in part.paths))
    statuses = {part.status for part in parts}
    if parts and statuses == {"downloaded"}:
        status = "downloaded"
    elif parts and statuses == {"skipped"}:
        status = "skipped"
    elif paths:
        status = "partial"
    else:
        status = "failed"
    return DownloadOutcome(status=status, paths=paths)


MODE_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "rm": ("rm", "rm_sinter"),
    "fines_analysis": ("rm",),
    "dpr": ("dpr",),
    "hot_metal": ("hot_metal",),
    "rm_strength": ("rm_strength_coke", "rm_strength_sinter"),
    "rm_stock": ("rm_stock", "rm_stock_sinter"),
    "dust": ("rm", "dust_chemical"),
    "ash": ("ash",),
}


class SynologyDownloader:
    """Resolve requested source files from one folder listing and download by API."""

    def __init__(self, client: SynologyApiClient, cfg: DownloadConfig, logger) -> None:
        self.client = client
        self.cfg = cfg
        self.logger = logger

    def _identifier(self, source_key: str) -> str:
        aliases = {"rm": ("rm", "fines_analysis", "dust_basic")}
        for key in aliases.get(source_key, (source_key,)):
            value = self.cfg.portal_files.get(key)
            if value:
                return str(value)
        return ""

    def _download_remote(
        self,
        remote: RemoteFile,
        cache: dict[tuple[str, int | None, int | None], DownloadOutcome],
    ) -> DownloadOutcome:
        cache_key = (remote.path, remote.mtime, remote.size)
        cached = cache.get(cache_key)
        if cached is not None:
            self.logger.info("Reusing download already resolved for another mode: %s", remote.name)
            return cached
        destination = Path(self.cfg.download_dir).expanduser() / Path(remote.name).name
        try:
            self.logger.info("Downloading | remote=%s | local=%s", remote.path, destination)
            path = self.client.download_file(
                remote.path,
                destination,
                expected_size=remote.size,
                remote_mtime=remote.mtime,
            )
            outcome = DownloadOutcome(
                status="downloaded",
                paths=(path,),
                portal_name=remote.name,
            )
        except Exception as exc:
            self.logger.error("Download failed | remote=%s | error=%s", remote.path, exc)
            outcome = DownloadOutcome(status="failed", portal_name=remote.name)
        cache[cache_key] = outcome
        return outcome

    def download(
        self,
        modes: list[str],
        run_dates: list[str],
        is_today_mode: bool,
    ) -> DownloadResult:
        outcomes: dict[str, DownloadOutcome] = {}
        cache: dict[tuple[str, int | None, int | None], DownloadOutcome] = {}
        root_modes = [mode for mode in modes if mode != "charge"]
        root_files: list[RemoteFile] = []
        if root_modes:
            self.logger.info("Polling %s", self.cfg.folder_path)
            root_files = self.client.list_files(self.cfg.folder_path.rstrip("/") or "/")

        for mode in root_modes:
            parts: list[DownloadOutcome] = []
            for source_key in MODE_SOURCE_KEYS.get(mode, ()):
                identifier = self._identifier(source_key)
                expected_names: tuple[str | None, ...] = (None,)
                if source_key == "ash":
                    expected_names = tuple(
                        dict.fromkeys(
                            ash_expected_name(identifier or "ASH ANALYSIS", run_date)
                            for run_date in run_dates
                        )
                    )
                for expected_name in expected_names:
                    remote = latest_matching_file(
                        root_files,
                        identifier,
                        expected_name=expected_name,
                    )
                    if remote is None:
                        self.logger.error(
                            "Required remote file not found | mode=%s | identifier=%s",
                            mode,
                            expected_name or identifier,
                        )
                        parts.append(DownloadOutcome(status="failed"))
                        continue
                    parts.append(self._download_remote(remote, cache))
            outcomes[mode] = _combine_download_outcomes(parts)

        if "charge" in modes:
            self.logger.info("Polling %s", self.cfg.charge_folder_path)
            charge_files = self.client.list_files(self.cfg.charge_folder_path)
            charge_dates = (
                [datetime.now(BUSINESS_TZ).strftime("%d-%b-%Y")]
                if is_today_mode
                else run_dates
            )
            parts = []
            for run_date in charge_dates:
                expected = charge_expected_name(run_date)
                remote = latest_matching_file(
                    charge_files,
                    expected,
                    expected_name=expected,
                )
                if remote is None:
                    self.logger.error("Required charge file not found: %s.xlsx", expected)
                    parts.append(DownloadOutcome(status="failed"))
                else:
                    parts.append(self._download_remote(remote, cache))
            outcomes["charge"] = _combine_download_outcomes(parts)

        return DownloadResult(by_mode=outcomes)
