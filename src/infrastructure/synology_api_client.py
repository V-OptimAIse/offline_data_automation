from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests


SESSION_ERROR_CODES = {106, 107, 119}
TEMPORARY_HTTP_STATUSES = {429, 500, 502, 503, 504}


class SynologyApiError(RuntimeError):
    """Raised when File Station rejects a request or returns invalid data."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RemoteFile:
    name: str
    path: str
    size: int | None
    mtime: int | None
    isdir: bool

    @classmethod
    def from_api(cls, value: dict[str, Any]) -> "RemoteFile":
        additional = value.get("additional") or {}
        time_data = additional.get("time") or {}
        size = additional.get("size", value.get("size"))
        mtime = time_data.get("mtime", value.get("mtime"))
        return cls(
            name=str(value.get("name") or ""),
            path=str(value.get("path") or ""),
            size=int(size) if size is not None else None,
            mtime=int(mtime) if mtime is not None else None,
            isdir=bool(value.get("isdir")),
        )


class SynologyApiClient:
    """Small File Station HTTP API client with a reusable HTTPS session."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        request_timeout_seconds: int = 30,
        download_timeout_seconds: int = 300,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 5,
        session: requests.Session | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Synology base_url must be an absolute HTTPS URL")
        if not username or not password:
            raise ValueError("Synology username and password are required")

        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.username = username
        self._password = password
        self.request_timeout_seconds = int(request_timeout_seconds)
        self.download_timeout_seconds = int(download_timeout_seconds)
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.session = session or requests.Session()
        self.sid: str | None = None
        self.syno_token: str | None = None

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/webapi/auth.cgi"

    @property
    def entry_url(self) -> str:
        return f"{self.base_url}/webapi/entry.cgi"

    def _sleep_before_retry(self, attempt: int) -> None:
        if self.retry_backoff_seconds:
            time.sleep(self.retry_backoff_seconds * attempt)

    def _send(
        self,
        method: str,
        url: str,
        *,
        timeout: tuple[int, int],
        stream: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=timeout,
                    stream=stream,
                    **kwargs,
                )
                if response.status_code in TEMPORARY_HTTP_STATUSES:
                    if attempt < self.retry_attempts:
                        response.close()
                        self._sleep_before_retry(attempt)
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                self._sleep_before_retry(attempt)
        raise SynologyApiError(
            f"Synology request failed after {self.retry_attempts} attempt(s): "
            f"{type(last_error).__name__}"
        ) from last_error

    @staticmethod
    def _checked_payload(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SynologyApiError(
                f"Synology {operation} returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SynologyApiError(f"Synology {operation} returned an invalid response")
        if not payload.get("success"):
            error = payload.get("error") or {}
            code_value = error.get("code") if isinstance(error, dict) else None
            try:
                code = int(code_value) if code_value is not None else None
            except (TypeError, ValueError):
                code = None
            raise SynologyApiError(
                f"Synology {operation} failed"
                + (f" (error code {code})" if code is not None else ""),
                code=code,
            )
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise SynologyApiError(f"Synology {operation} returned invalid data")
        return data

    def login(self) -> None:
        response = self._send(
            "POST",
            self.auth_url,
            timeout=(self.request_timeout_seconds, self.request_timeout_seconds),
            data={
                "api": "SYNO.API.Auth",
                "version": 6,
                "method": "login",
                "account": self.username,
                "passwd": self._password,
                "session": "FileStation",
                "format": "sid",
                "enable_syno_token": "yes",
            },
        )
        try:
            data = self._checked_payload(response, "login")
        finally:
            response.close()
        sid = data.get("sid")
        if not sid:
            raise SynologyApiError("Synology login returned no session id")
        self.sid = str(sid)
        token = data.get("synotoken", data.get("SynoToken"))
        self.syno_token = str(token) if token else None

    def _auth_params(self) -> dict[str, str]:
        if not self.sid:
            self.login()
        result = {"_sid": str(self.sid)}
        if self.syno_token:
            result["SynoToken"] = self.syno_token
        return result

    def _json_call(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        allow_reauth: bool = True,
    ) -> dict[str, Any]:
        request_params = {**params, **self._auth_params()}
        response = self._send(
            "GET",
            self.entry_url,
            timeout=(self.request_timeout_seconds, self.request_timeout_seconds),
            params=request_params,
        )
        try:
            return self._checked_payload(response, operation)
        except SynologyApiError as exc:
            if allow_reauth and exc.code in SESSION_ERROR_CODES:
                self.sid = None
                self.syno_token = None
                self.login()
                return self._json_call(operation, params, allow_reauth=False)
            raise
        finally:
            response.close()

    def list_files(self, folder_path: str, *, page_size: int = 500) -> list[RemoteFile]:
        if not folder_path.startswith("/"):
            raise ValueError("Synology folder_path must start with '/'")
        page_size = max(1, int(page_size))
        files: list[RemoteFile] = []
        offset = 0
        while True:
            data = self._json_call(
                f"list {folder_path}",
                {
                    "api": "SYNO.FileStation.List",
                    "version": 2,
                    "method": "list",
                    "folder_path": json.dumps(folder_path),
                    "additional": json.dumps(["size", "time"]),
                    "offset": offset,
                    "limit": page_size,
                },
            )
            raw_files = data.get("files") or []
            if not isinstance(raw_files, list):
                raise SynologyApiError("Synology file listing contains invalid files data")
            page = [RemoteFile.from_api(item) for item in raw_files if isinstance(item, dict)]
            files.extend(page)
            total = int(data.get("total", len(files)))
            offset += len(raw_files)
            if offset >= total:
                return files
            if not raw_files:
                raise SynologyApiError(
                    f"Synology pagination stopped before all files were returned for {folder_path}"
                )

    @staticmethod
    def _download_error_from_bytes(content: bytes) -> SynologyApiError | None:
        if not content.lstrip().startswith(b"{"):
            return None
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("success", True):
            return None
        error = payload.get("error") or {}
        code_value = error.get("code") if isinstance(error, dict) else None
        try:
            code = int(code_value) if code_value is not None else None
        except (TypeError, ValueError):
            code = None
        return SynologyApiError(
            "Synology download failed"
            + (f" (error code {code})" if code is not None else ""),
            code=code,
        )

    def _download_response(
        self,
        remote_path: str,
    ) -> requests.Response:
        params: dict[str, Any] = {
            "api": "SYNO.FileStation.Download",
            "version": 2,
            "method": "download",
            "path": json.dumps(remote_path),
            "mode": "download",
            **self._auth_params(),
        }
        return self._send(
            "GET",
            self.entry_url,
            timeout=(self.request_timeout_seconds, self.download_timeout_seconds),
            stream=True,
            params=params,
        )

    @staticmethod
    def _chunks(response: requests.Response) -> Iterable[bytes]:
        return response.iter_content(chunk_size=1024 * 1024)

    def download_file(
        self,
        remote_path: str,
        local_path: str | os.PathLike[str],
        expected_size: int | None = None,
        remote_mtime: int | None = None,
    ) -> str:
        """Stream a remote file to a validated temporary file, then replace atomically."""
        if not remote_path.startswith("/"):
            raise ValueError("Synology remote_path must start with '/'")
        destination = Path(local_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_name(f"{destination.name}.part")
        part_path.unlink(missing_ok=True)

        auth_retried = False
        stream_attempt = 1
        while stream_attempt <= self.retry_attempts:
            response = self._download_response(remote_path)
            try:
                try:
                    chunks = iter(self._chunks(response))
                    first = next((chunk for chunk in chunks if chunk), b"")
                    content_type = response.headers.get("Content-Type", "").casefold()
                    if "json" in content_type or first.lstrip().startswith(b"{"):
                        body = first + b"".join(chunks)
                        error = self._download_error_from_bytes(body)
                        if error is not None:
                            if not auth_retried and error.code in SESSION_ERROR_CODES:
                                self.sid = None
                                self.syno_token = None
                                self.login()
                                auth_retried = True
                                continue
                            raise error
                        if "json" in content_type:
                            raise SynologyApiError(
                                "Synology download returned unexpected JSON"
                            )

                    with part_path.open("wb") as output:
                        if first:
                            output.write(first)
                        for chunk in chunks:
                            if chunk:
                                output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())

                    actual_size = part_path.stat().st_size
                    if expected_size is not None and actual_size != int(expected_size):
                        raise SynologyApiError(
                            f"Downloaded size mismatch for {remote_path}: "
                            f"expected {int(expected_size)}, received {actual_size}"
                        )
                    os.replace(part_path, destination)
                    if remote_mtime is not None:
                        os.utime(destination, (int(remote_mtime), int(remote_mtime)))
                    return str(destination.resolve())
                except requests.RequestException as exc:
                    part_path.unlink(missing_ok=True)
                    if stream_attempt >= self.retry_attempts:
                        raise SynologyApiError(
                            f"Synology download stream failed after "
                            f"{self.retry_attempts} attempt(s): {type(exc).__name__}"
                        ) from exc
                    self._sleep_before_retry(stream_attempt)
                    stream_attempt += 1
                    continue
                except Exception:
                    part_path.unlink(missing_ok=True)
                    raise
            finally:
                response.close()

        raise SynologyApiError("Synology download failed")

    def logout(self) -> None:
        try:
            if self.sid:
                params = {
                    "api": "SYNO.API.Auth",
                    "version": 6,
                    "method": "logout",
                    "session": "FileStation",
                    **self._auth_params(),
                }
                try:
                    response = self._send(
                        "POST",
                        self.auth_url,
                        timeout=(
                            self.request_timeout_seconds,
                            self.request_timeout_seconds,
                        ),
                        data=params,
                    )
                    try:
                        self._checked_payload(response, "logout")
                    finally:
                        response.close()
                except SynologyApiError:
                    pass
        finally:
            self.sid = None
            self.syno_token = None
            self.session.close()
