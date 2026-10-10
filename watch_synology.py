#!/usr/bin/env python3
"""Lightweight Synology File Station change checker for the Raspberry Pi.

Runs as a systemd *timer*, not a continuous Selenium process. One pass logs in
once per existing portal profile, lists file metadata (mtime/size), and runs
src/app.py only when a required source has changed and remained stable over
at least two successive polls.

Run from the project root:
  python watch_synology.py --probe   # Verify API and folder paths; no changes
  python watch_synology.py --prime   # Record existing files as already handled
  python watch_synology.py           # Check and run changed modes

Requires `requests` (pip install requests). Uses existing src/config/base.yaml
and the existing .env credentials. All file operations on the NAS are read-only.

Important: QuickConnect sometimes does not pass custom Web API requests. Use
--probe to check from the Pi before enabling the timer. The script intentionally
does not fall back to launching Selenium on every poll.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from core.config_loader import load_config  # noqa: E402

LOG = logging.getLogger("synology_change_checker")
STATE_PATH = ROOT / "output" / "remote_watch_state.json"
TZ = ZoneInfo("Asia/Kolkata")
MODE_ORDER = (
    "rm", "fines_analysis", "dpr", "hot_metal", "rm_strength", "rm_stock",
    "charge", "dust", "ash",
)
# Keys are taken directly from portal_files in src/config/base.yaml.
# BF-02 BUNKER is intentionally shared by three processing modes.
FILE_TO_MODES = {
    "rm": ("rm", "fines_analysis", "dust"),
    "rm_sinter": ("rm",),
    "fines_analysis": ("fines_analysis",),
    "dpr": ("dpr",),
    "hot_metal": ("hot_metal",),
    "rm_strength_coke": ("rm_strength",),
    "rm_strength_sinter": ("rm_strength",),
    "rm_stock": ("rm_stock",),
    "rm_stock_sinter": ("rm_stock",),
    "dust_basic": ("dust",),
    "dust_chemical": ("dust",),
    "ash": ("ash",),
}


def match_identifier(filename: str, identifier: str) -> bool:
    """Same token-based semantics as the project's PortalDownloader matcher."""
    name_tokens = re.findall(r"[a-z0-9]+", filename.casefold().replace("&", " and "))
    ident_tokens = re.findall(r"[a-z0-9]+", identifier.casefold().replace("&", " and "))
    width = len(ident_tokens)
    return bool(width) and any(
        name_tokens[i:i + width] == ident_tokens
        for i in range(len(name_tokens) - width + 1)
    )


def charge_folder_from_url(url: str) -> str:
    """Extract the actual DSM folder from existing hourly_url (not a URL path)."""
    launch_param = parse_qs(urlsplit(url).query).get("launchParam", [""])[0]
    folder = parse_qs(launch_param).get("openfile", [""])[0]
    if not folder.startswith("/"):
        raise ValueError("Could not determine charge folder from eml.hourly_url")
    return folder.rstrip("/")


def file_signature(file_info: dict) -> str:
    details = file_info.get("additional") or {}
    timestamps = details.get("time") or {}
    mtime = timestamps.get("mtime")
    size = details.get("size", file_info.get("size"))
    if mtime is None or size is None:
        raise ValueError(
            f"Synology metadata missing mtime/size for {file_info.get('path')!r}; "
            "check File Station API permissions and 'additional' fields"
        )
    return json.dumps(
        [file_info["path"], int(mtime), int(size)],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def relevant_file(file_info: dict) -> bool:
    name = str(file_info.get("name") or "")
    return (
        bool(name)
        and not file_info.get("isdir")
        and not name.startswith("~$")
        and not name.casefold().endswith((".tmp", ".part", ".crdownload"))
        and name.casefold().endswith((".xls", ".xlsx", ".xlsm", ".xlsb"))
    )


def latest_matching(files: list[dict], identifier: str) -> dict | None:
    matches = [
        f for f in files
        if relevant_file(f) and match_identifier(str(f.get("name", "")), identifier)
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda f: (
            int(((f.get("additional") or {}).get("time") or {}).get("mtime") or 0),
            str(f.get("path") or ""),
        ),
    )


class DSMClient:
    """A minimal authenticated, read-only DSM File Station API client."""

    def __init__(self, base_url: str, username: str, password: str):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("The DSM URL must use HTTPS")
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        self.username = username
        self.password = password
        self.http = requests.Session()
        self.sid = ""
        self.syno_token = ""
        self.api_info: dict = {}

    def _endpoint(self, api_name: str) -> str:
        endpoint = (self.api_info.get(api_name) or {}).get("path", "entry.cgi")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.cgi", endpoint):
            raise ValueError(f"Unsupported DSM API path: {endpoint!r}")
        return f"{self.base_url}/webapi/{endpoint}"

    def _version(self, api_name: str, preferred: int) -> int:
        data = self.api_info[api_name]
        low, high = int(data["minVersion"]), int(data["maxVersion"])
        return min(max(preferred, low), high)

    def _post(self, api_name: str, version: int, method: str, **kwargs) -> dict:
        data = {"api": api_name, "version": version, "method": method, **kwargs}
        if self.sid:
            data["_sid"] = self.sid
        if self.syno_token:
            data["SynoToken"] = self.syno_token
        try:
            response = self.http.post(
                self._endpoint(api_name), data=data, timeout=(8, 30)
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(
                f"DSM API request {api_name}.{method} failed via "
                f"{self.base_url}: {type(exc).__name__}. "
                "If QuickConnect does not forward /webapi requests, use a "
                "client-approved HTTPS DSM endpoint or VPN."
            ) from exc
        if not isinstance(result, dict) or not result.get("success"):
            code = (result.get("error") or {}).get("code") if isinstance(result, dict) else None
            raise RuntimeError(
                f"DSM API {api_name}.{method} rejected the request "
                f"(error code {code}). Verify the NAS account's File Station "
                "permissions, supported API version, and the folder path."
            )
        return result.get("data") or {}

    def login(self):
        # Synology documents discovery via /webapi/entry.cgi, even without login.
        discovery = self._post(
            "SYNO.API.Info", 1, "query",
            query="SYNO.API.Auth,SYNO.FileStation.List",
        )
        self.api_info = discovery
        for api in ("SYNO.API.Auth", "SYNO.FileStation.List"):
            if api not in self.api_info:
                raise RuntimeError(f"DSM does not advertise {api} on this endpoint")
        auth = self._post(
            "SYNO.API.Auth",
            self._version("SYNO.API.Auth", 6),
            "login",
            account=self.username,
            passwd=self.password,
            session="FileStation",
            format="sid",
        )
        self.sid = str(auth.get("sid") or "")
        self.syno_token = str(auth.get("synotoken") or auth.get("SynoToken") or "")
        if not self.sid:
            raise RuntimeError("DSM login succeeded but returned no session id")

    def list_files(self, folder: str) -> list[dict]:
        """List direct children, paginating without retrieving file contents."""
        if not folder.startswith("/"):
            raise ValueError(f"Invalid File Station folder path {folder!r}")
        all_files: list[dict] = []
        offset = 0
        while True:
            data = self._post(
                "SYNO.FileStation.List",
                self._version("SYNO.FileStation.List", 2),
                "list",
                folder_path=json.dumps(folder),
                additional=json.dumps(["time", "size"]),
                offset=offset,
                limit=500,
            )
            entries = data.get("files", [])
            total = int(data.get("total", 0))
            if not isinstance(entries, list):
                raise RuntimeError("Unexpected File Station response: files is not a list")
            all_files.extend(entries)
            offset += len(entries)
            if offset >= total:
                return all_files
            if not entries:
                raise RuntimeError(f"Pagination stopped early for {folder!r}")

    def logout(self):
        try:
            if self.sid:
                try:
                    self._post(
                        "SYNO.API.Auth", self._version("SYNO.API.Auth", 6), "logout",
                        session="FileStation",
                    )
                except Exception as exc:
                    LOG.warning("DSM logout failed: %s", type(exc).__name__)
        finally:
            self.sid = ""
            self.http.close()


def collect_files(cfg: dict) -> dict[str, dict]:
    """Fetch one directory listing per profile, plus today's charge folder."""
    portal_identifiers = cfg.get("portal_files") or {}
    eml = cfg["eml"]
    located: dict[str, dict] = {}
    for profile_name, profile in eml["profiles"].items():
        allowed_modes = set(profile["modes"])
        user, password = profile.get("user"), profile.get("password")
        if not user or not password:
            raise RuntimeError(f"Missing credentials for {profile_name} in .env")
        client = DSMClient(eml["login_url"], str(user), str(password))
        try:
            client.login()
            root_folder = profile["file_station_search_path"].rstrip("/")
            folders = [root_folder]
            # Optional paths for a customer who stores required sources in
            # additional subdirectories. No recursive NAS-wide scans.
            configured_folders = (cfg.get("remote_watcher") or {}).get("folders", {})
            folders += list(configured_folders.get(profile_name) or [])
            files: list[dict] = []
            for folder in dict.fromkeys(folders):
                entries = client.list_files(folder)
                LOG.info("%s: %d entries in %s", profile_name, len(entries), folder)
                files.extend(entries)

            for key, modes in FILE_TO_MODES.items():
                if not allowed_modes.intersection(modes):
                    continue
                identifier = portal_identifiers.get(key)
                if not identifier:
                    continue
                found = latest_matching(files, identifier)
                if found is None:
                    LOG.debug("%s: no matching file for %s", profile_name, identifier)
                    continue
                located[f"{profile_name}:{key}"] = {
                    "name": found["name"],
                    "path": found["path"],
                    "fingerprint": file_signature(found),
                    "modes": tuple(m for m in modes if m in allowed_modes),
                }

            if "charge" in allowed_modes:
                charge_folder = charge_folder_from_url(eml["hourly_url"])
                entries = client.list_files(charge_folder)
                LOG.info("%s: %d entries in %s", profile_name, len(entries), charge_folder)
                now = datetime.now(TZ)
                prefix = f"CHARGE_AND_DUMP_REPORT_{now.day}_{now.month}_{now.year}"
                matched = [
                    f for f in entries
                    if relevant_file(f)
                    and str(f.get("name", "")).startswith(prefix)
                    and str(f.get("name", "")).casefold().endswith(".xlsx")
                ]
                if matched:
                    latest = max(
                        matched,
                        key=lambda f: int(
                            ((f.get("additional") or {}).get("time") or {}).get("mtime") or 0
                        ),
                    )
                    located[f"{profile_name}:charge:{now.date().isoformat()}"] = {
                        "name": latest["name"],
                        "path": latest["path"],
                        "fingerprint": file_signature(latest),
                        "modes": ("charge",),
                    }
        finally:
            client.logout()
    return located


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"processed": {}, "observed": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Watcher state is not an object")
    return {
        "processed": dict(data.get("processed") or {}),
        "observed": dict(data.get("observed") or {}),
    }


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".remote-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_observations(state: dict, located: dict) -> dict[str, dict]:
    """Return stable changed source files; never mark processed here."""
    changed = {}
    for key, source in located.items():
        fp = source["fingerprint"]
        previous = state["observed"].get(key) or {}
        count = (int(previous.get("checks", 0)) + 1) if previous.get("fingerprint") == fp else 1
        state["observed"][key] = {"fingerprint": fp, "checks": min(2, count)}
        if state["processed"].get(key) == fp:
            continue
        if count >= 2:
            changed[key] = source
        else:
            LOG.info("Change observed, waiting for a stable second poll: %s", source["name"])
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--probe", action="store_true", help="Read-only DSM API connectivity and filename test")
    operation.add_argument("--prime", action="store_true", help="Mark current source versions as already processed")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    located = collect_files(cfg)
    LOG.info("Found %d required source identifiers", len(located))
    if not located:
        raise RuntimeError(
            "No required Excel source files were found in the configured NAS "
            "folders. Check the names and paths with File Station first."
        )
    if args.probe:
        for key, src in sorted(located.items()):
            LOG.info("%s => %s (modes=%s)", key, src["path"], ",".join(src["modes"]))
        for profile_name, profile in cfg["eml"]["profiles"].items():
            allowed = set(profile["modes"])
            for source_key, modes in FILE_TO_MODES.items():
                if allowed.intersection(modes) and f"{profile_name}:{source_key}" not in located:
                    LOG.warning(
                        "Not found in listed folders: %s for %s",
                        (cfg.get("portal_files") or {}).get(source_key, source_key),
                        profile_name,
                    )
        return 0

    state = load_state(STATE_PATH)
    if args.prime:
        state["processed"].update({key: info["fingerprint"] for key, info in located.items()})
        state["observed"].update({
            key: {"fingerprint": info["fingerprint"], "checks": 2}
            for key, info in located.items()
        })
        save_state(STATE_PATH, state)
        LOG.info("Baseline recorded. Subsequent modifications will trigger their modes.")
        return 0

    changed = update_observations(state, located)
    # Persist stable observations before starting a potentially long-running job.
    save_state(STATE_PATH, state)
    if not changed:
        LOG.info("No stable unprocessed remote changes")
        return 0

    modes = set()
    for key, source in changed.items():
        LOG.info("UPDATED: %s (%s)", source["name"], key)
        modes.update(source["modes"])
    ordered_modes = [m for m in MODE_ORDER if m in modes]
    command = [sys.executable, "src/app.py", "--mode", ",".join(ordered_modes), "--today"]
    LOG.info("Starting existing application for modes: %s", ", ".join(ordered_modes))
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        LOG.error("Application exited with code %d. Pending changes will be retried.", result.returncode)
        return result.returncode

    # Existing app currently returns code 0 even for SOME per-mode skips/failures.
    # Production-hardening should make app.py return nonzero for failed modes.
    for key, source in changed.items():
        state["processed"][key] = source["fingerprint"]
    save_state(STATE_PATH, state)
    LOG.info("Recorded %d source versions as handled", len(changed))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.RequestException, RuntimeError, ValueError, KeyError, OSError) as exc:
        logging.getLogger("synology_change_checker").error("Watch check failed: %s", exc)
        raise SystemExit(1) from None
