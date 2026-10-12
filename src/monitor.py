from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app import run_processing
from core.config_loader import load_config
from core.logging import configure_logging
from core.synology_sources import (
    ResolvedSource,
    SourceSpec,
    build_source_registry,
    dependencies_for_modes,
    listing_keys,
    resolve_sources,
)
from domains.download.service import DownloadOutcome, DownloadResult
from infrastructure.synology_api_client import SynologyApiClient


BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
MODE_ORDER = (
    "rm",
    "fines_analysis",
    "dpr",
    "hot_metal",
    "rm_strength",
    "rm_stock",
    "charge",
    "dust",
    "ash",
)


def load_monitor_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "sources": {}}
    with path.open("r", encoding="utf-8") as state_file:
        data = json.load(state_file)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ValueError(f"Invalid Synology monitor state: {path}")
    if int(data.get("version", 0)) != 1:
        raise ValueError(f"Unsupported Synology monitor state version: {data.get('version')}")
    return data


def save_monitor_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _entry_fingerprint(entry: dict | None) -> tuple[object, object, object] | None:
    if not entry or entry.get("status") != "completed":
        return None
    return entry.get("remote_path"), entry.get("mtime"), entry.get("size")


def _source_changed(source: ResolvedSource, entry: dict | None) -> bool:
    return source.fingerprint != _entry_fingerprint(entry)


class SynologyMonitor:
    def __init__(
        self,
        cfg: dict,
        clients: dict[str, SynologyApiClient],
        logger,
        *,
        process_initial: bool | None = None,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        processor: Callable[..., None] = run_processing,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.logger = logger
        self.monitor_cfg = cfg.get("monitor") or {}
        configured_initial = bool(
            self.monitor_cfg.get("process_initial_snapshot", False)
        )
        self.process_initial = (
            configured_initial if process_initial is None else process_initial
        )
        self.dry_run = dry_run
        self.sleep = sleep
        self.processor = processor
        self.now = now or (lambda: datetime.now(BUSINESS_TZ))
        self.state_path = Path(
            self.monitor_cfg.get(
                "state_path", "output/state/synology_monitor_state.json"
            )
        ).expanduser()
        self._connected_profiles: set[str] = set()
        self._stop_event = threading.Event()

    def request_stop(self, *_args) -> None:
        self.logger.info("Shutdown requested; finishing the current operation")
        self._stop_event.set()

    def close(self) -> None:
        for profile, client in self.clients.items():
            try:
                client.logout()
            except Exception as exc:
                self.logger.warning("Logout failed | profile=%s | error=%s", profile, exc)
        self._connected_profiles.clear()

    def _ensure_connected(self, profile: str) -> None:
        if profile in self._connected_profiles:
            return
        self.clients[profile].login()
        self._connected_profiles.add(profile)
        self.logger.info("Connected %s", profile)

    def _list(
        self,
        keys: tuple[tuple[str, str], ...],
    ) -> dict[tuple[str, str], list]:
        listings = {}
        for profile, folder in keys:
            self._ensure_connected(profile)
            self.logger.info("Polling %s", folder)
            listings[(profile, folder)] = self.clients[profile].list_files(folder)
        return listings

    def _stable_changes(
        self,
        candidates: dict[str, ResolvedSource],
        specs: tuple[SourceSpec, ...],
        resolved: dict[str, ResolvedSource],
    ) -> tuple[dict[str, ResolvedSource], dict[str, ResolvedSource]]:
        delay = max(0.0, float(self.monitor_cfg.get("stability_delay_seconds", 10)))
        if delay:
            self.sleep(delay)
        changed_keys = tuple(
            dict.fromkeys(
                (source.spec.profile, source.spec.folder)
                for source in candidates.values()
            )
        )
        second_listings = self._list(changed_keys)
        affected_specs = tuple(
            spec
            for spec in specs
            if (spec.profile, spec.folder) in set(changed_keys)
        )
        second_resolved = resolve_sources(affected_specs, second_listings)
        current_resolved = dict(resolved)
        for spec in affected_specs:
            current_resolved.pop(spec.source_id, None)
        current_resolved.update(second_resolved)
        stable = {}
        for source_id, first in candidates.items():
            second = second_resolved.get(source_id)
            if second is not None and second.fingerprint == first.fingerprint:
                stable[source_id] = second
                self.logger.info("Source stable | %s", source_id)
            else:
                self.logger.info("Source still changing | %s", source_id)
        return stable, current_resolved

    def _local_path(self, source: ResolvedSource) -> Path:
        return (
            Path(self.cfg["download"]["download_dir"]).expanduser()
            / Path(source.remote.name).name
        )

    def _local_matches(self, source: ResolvedSource) -> bool:
        path = self._local_path(source)
        if not path.is_file():
            return False
        stat = path.stat()
        size_matches = source.remote.size is None or stat.st_size == source.remote.size
        mtime_matches = (
            source.remote.mtime is None or int(stat.st_mtime) == source.remote.mtime
        )
        return size_matches and mtime_matches

    def _download(self, source: ResolvedSource) -> str:
        local_path = self._local_path(source)
        self.logger.info(
            "Downloading | remote=%s | local=%s",
            source.remote.path,
            local_path,
        )
        result = self.clients[source.spec.profile].download_file(
            source.remote.path,
            local_path,
            expected_size=source.remote.size,
            remote_mtime=source.remote.mtime,
        )
        self.logger.info(
            "Download completed | bytes=%s | source=%s",
            source.remote.size,
            source.spec.source_id,
        )
        return result

    def _prepare_download_result(
        self,
        specs: tuple[SourceSpec, ...],
        resolved: dict[str, ResolvedSource],
        changed: dict[str, ResolvedSource],
        modes: list[str],
        state: dict,
    ) -> DownloadResult:
        dependencies = dependencies_for_modes(specs, modes)
        downloaded: dict[tuple[str, int | None, int | None], str] = {}
        mode_paths: dict[str, list[str]] = {mode: [] for mode in modes}
        changed_ids = set(changed)

        for mode in modes:
            for spec in dependencies.get(mode, ()):
                source = resolved.get(spec.source_id)
                if source is None:
                    raise RuntimeError(
                        f"Required companion source is unavailable: {spec.source_id}"
                    )
                previous = state["sources"].get(spec.source_id)
                if (
                    spec.source_id not in changed_ids
                    and _source_changed(source, previous)
                ):
                    raise RuntimeError(
                        f"Required companion source is not yet stable: {spec.source_id}"
                    )

                fingerprint = source.fingerprint
                if fingerprint in downloaded:
                    path = downloaded[fingerprint]
                elif spec.source_id not in changed_ids and self._local_matches(source):
                    path = str(self._local_path(source).resolve())
                else:
                    path = self._download(source)
                downloaded[fingerprint] = path
                if path not in mode_paths[mode]:
                    mode_paths[mode].append(path)

        return DownloadResult(
            by_mode={
                mode: DownloadOutcome(status="downloaded", paths=tuple(paths))
                for mode, paths in mode_paths.items()
            }
        )

    def run_cycle(self) -> bool:
        cycle_now = self.now().astimezone(BUSINESS_TZ)
        specs = build_source_registry(self.cfg, cycle_now.date())
        state_existed = self.state_path.exists()
        state = load_monitor_state(self.state_path)
        listings = self._list(listing_keys(specs))
        resolved = resolve_sources(specs, listings)

        missing = [spec.source_id for spec in specs if spec.source_id not in resolved]
        if missing:
            self.logger.warning("Required sources not found | %s", ",".join(missing))

        if not state_existed and not self.process_initial:
            baseline = {
                source_id: source.state_entry()
                for source_id, source in resolved.items()
            }
            if not self.dry_run:
                state["sources"].update(baseline)
                save_monitor_state(self.state_path, state)
            self.logger.info(
                "Initial snapshot recorded without processing | sources=%d",
                len(baseline),
            )
            return False

        candidates = {
            source_id: source
            for source_id, source in resolved.items()
            if _source_changed(source, state["sources"].get(source_id))
        }
        if not candidates:
            self.logger.info(
                "Poll complete | changed=0 | resolved=%d | missing=%d",
                len(resolved),
                len(missing),
            )
            return False

        for source_id, source in candidates.items():
            old = state["sources"].get(source_id) or {}
            self.logger.info(
                "Change detected | source=%s | old_mtime=%s | new_mtime=%s | "
                "old_size=%s | new_size=%s",
                source_id,
                old.get("mtime"),
                source.remote.mtime,
                old.get("size"),
                source.remote.size,
            )

        stable, current_resolved = self._stable_changes(candidates, specs, resolved)
        if not stable:
            return False
        modes = [
            mode
            for mode in MODE_ORDER
            if any(mode in source.spec.modes for source in stable.values())
        ]
        self.logger.info("Triggered modes | %s", ",".join(modes))
        if self.dry_run:
            self.logger.info("Dry run; no files downloaded and no state committed")
            return True

        result = self._prepare_download_result(
            specs,
            current_resolved,
            stable,
            modes,
            state,
        )
        run_date = cycle_now.strftime("%d-%b-%Y")
        self.processor(
            modes,
            [run_date],
            self.cfg,
            self.logger,
            result,
            skip_download=False,
        )
        self.logger.info("Processing completed | sources=%s", ",".join(stable))

        for source_id, source in stable.items():
            state["sources"][source_id] = source.state_entry()
        save_monitor_state(self.state_path, state)
        for source_id, source in stable.items():
            self.logger.info(
                "State committed | source=%s | mtime=%s | size=%s",
                source_id,
                source.remote.mtime,
                source.remote.size,
            )
        return True

    def run_forever(self) -> None:
        interval = max(1.0, float(self.monitor_cfg.get("poll_interval_seconds", 60)))
        self.logger.info("Synology monitor started | interval_seconds=%s", interval)
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                self.logger.warning(
                    "Monitor cycle failed; state was not changed and the next cycle will retry | %s",
                    exc,
                )
                self.logger.debug("Monitor cycle failure details", exc_info=True)
            self._stop_event.wait(interval)


def create_clients(cfg: dict) -> dict[str, SynologyApiClient]:
    monitor_cfg = cfg.get("monitor") or {}
    clients = {}
    for profile_name, profile in cfg["eml"]["profiles"].items():
        user = str(profile.get("user") or "").strip()
        password = str(profile.get("password") or "").strip()
        if not user or not password:
            normalized = profile_name.upper()
            raise ValueError(
                f"Missing credentials for {profile_name}; set "
                f"EML_{normalized}_USER and EML_{normalized}_PASSWORD in .env"
            )
        clients[profile_name] = SynologyApiClient(
            cfg["eml"]["base_url"],
            user,
            password,
            request_timeout_seconds=int(
                monitor_cfg.get("request_timeout_seconds", 30)
            ),
            download_timeout_seconds=int(
                monitor_cfg.get("download_timeout_seconds", 300)
            ),
            retry_attempts=int(monitor_cfg.get("retry_attempts", 3)),
            retry_backoff_seconds=float(
                monitor_cfg.get("retry_backoff_seconds", 5)
            ),
        )
    return clients


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Synology files via HTTP API")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument(
        "--process-initial",
        action="store_true",
        help="Process current files when monitor state does not yet exist",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and log changes without downloading, processing, or committing state",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    logging_cfg = cfg.get("logging") or {}
    logger = configure_logging(
        level=logging_cfg.get("level", "INFO"),
        log_dir=logging_cfg.get("dir", "output/logs"),
    )
    monitor = SynologyMonitor(
        cfg,
        create_clients(cfg),
        logger,
        process_initial=True if args.process_initial else None,
        dry_run=args.dry_run,
    )
    signal.signal(signal.SIGINT, monitor.request_stop)
    signal.signal(signal.SIGTERM, monitor.request_stop)
    try:
        if args.once:
            try:
                monitor.run_cycle()
                return 0
            except Exception as exc:
                logger.error("Monitor cycle failed; state was not changed | %s", exc)
                return 1
        monitor.run_forever()
        return 0
    finally:
        monitor.close()


if __name__ == "__main__":
    raise SystemExit(main())
