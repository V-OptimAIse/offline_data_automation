from __future__ import annotations

import json
import shutil
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import requests


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infrastructure.synology_api_client import SynologyApiClient, SynologyApiError


class Response:
    def __init__(
        self,
        payload=None,
        *,
        chunks=(),
        content_type="application/json",
        status_code=200,
    ):
        self.payload = payload
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class BrokenStreamResponse(Response):
    def iter_content(self, chunk_size):
        yield b"partial"
        raise requests.ConnectionError("connection reset")


def client_with(*responses, retries=1):
    session = Mock()
    session.request.side_effect = list(responses)
    client = SynologyApiClient(
        "https://synology.example",
        "user",
        "password",
        retry_attempts=retries,
        retry_backoff_seconds=0,
        session=session,
    )
    return client, session


class AuthenticationTests(unittest.TestCase):
    def test_successful_login_uses_required_auth_parameters(self):
        client, session = client_with(
            Response({"success": True, "data": {"sid": "secret", "synotoken": "token"}})
        )
        client.login()
        data = session.request.call_args.kwargs["data"]
        self.assertEqual(data["api"], "SYNO.API.Auth")
        self.assertEqual(data["version"], 6)
        self.assertEqual(data["session"], "FileStation")
        self.assertEqual(data["format"], "sid")
        self.assertEqual(data["enable_syno_token"], "yes")
        self.assertEqual(client.sid, "secret")

    def test_login_api_error_is_raised(self):
        client, _ = client_with(
            Response({"success": False, "error": {"code": 400}})
        )
        with self.assertRaisesRegex(SynologyApiError, "error code 400"):
            client.login()

    def test_session_expiry_reauthenticates_and_retries_once(self):
        success_login = {"success": True, "data": {"sid": "sid"}}
        client, session = client_with(
            Response(success_login),
            Response({"success": False, "error": {"code": 119}}),
            Response(success_login),
            Response({"success": True, "data": {"files": [], "total": 0}}),
        )
        client.login()
        self.assertEqual(client.list_files("/reports"), [])
        self.assertEqual(session.request.call_count, 4)


class ListingTests(unittest.TestCase):
    def test_listing_parses_metadata_and_paginates(self):
        success_login = Response({"success": True, "data": {"sid": "sid"}})
        first = Response(
            {
                "success": True,
                "data": {
                    "total": 2,
                    "files": [
                        {
                            "name": "one.xlsx",
                            "path": "/reports/one.xlsx",
                            "isdir": False,
                            "additional": {"size": 10, "time": {"mtime": 100}},
                        }
                    ],
                },
            }
        )
        second = Response(
            {
                "success": True,
                "data": {
                    "total": 2,
                    "files": [
                        {
                            "name": "two.xlsx",
                            "path": "/reports/two.xlsx",
                            "isdir": False,
                            "additional": {"size": 20, "time": {"mtime": 200}},
                        }
                    ],
                },
            }
        )
        client, session = client_with(success_login, first, second)
        client.login()
        files = client.list_files("/reports", page_size=1)
        self.assertEqual([(f.name, f.size, f.mtime) for f in files], [("one.xlsx", 10, 100), ("two.xlsx", 20, 200)])
        list_calls = session.request.call_args_list[1:]
        self.assertEqual([call.kwargs["params"]["offset"] for call in list_calls], [0, 1])
        self.assertEqual(
            json.loads(list_calls[0].kwargs["params"]["additional"]),
            ["size", "time"],
        )


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).resolve().parent / ".api-test-data"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_streams_atomically_and_applies_remote_mtime(self):
        client, _ = client_with(
            Response({"success": True, "data": {"sid": "sid"}}),
            Response(chunks=[b"abc", b"def"], content_type="application/octet-stream"),
        )
        client.login()
        with nullcontext(str(self.temp_dir)) as temp_dir:
            path = Path(temp_dir) / "report.xlsx"
            result = client.download_file(
                "/reports/report.xlsx",
                path,
                expected_size=6,
                remote_mtime=1_700_000_000,
            )
            self.assertEqual(Path(result).read_bytes(), b"abcdef")
            self.assertEqual(int(path.stat().st_mtime), 1_700_000_000)
            self.assertFalse(Path(f"{path}.part").exists())

    def test_json_error_is_not_saved_as_excel(self):
        error = json.dumps({"success": False, "error": {"code": 414}}).encode()
        client, _ = client_with(
            Response({"success": True, "data": {"sid": "sid"}}),
            Response(chunks=[error], content_type="application/json"),
        )
        client.login()
        with nullcontext(str(self.temp_dir)) as temp_dir:
            path = Path(temp_dir) / "report.xlsx"
            Path(f"{path}.part").write_bytes(b"stale partial download")
            with self.assertRaisesRegex(SynologyApiError, "error code 414"):
                client.download_file("/reports/report.xlsx", path, len(error), 1)
            self.assertFalse(path.exists())
            self.assertFalse(Path(f"{path}.part").exists())

    def test_size_mismatch_removes_partial_and_preserves_existing_file(self):
        client, _ = client_with(
            Response({"success": True, "data": {"sid": "sid"}}),
            Response(chunks=[b"short"], content_type="application/octet-stream"),
        )
        client.login()
        with nullcontext(str(self.temp_dir)) as temp_dir:
            path = Path(temp_dir) / "report.xlsx"
            path.write_bytes(b"existing")
            with self.assertRaisesRegex(SynologyApiError, "size mismatch"):
                client.download_file("/reports/report.xlsx", path, 100, 1)
            self.assertEqual(path.read_bytes(), b"existing")
            self.assertFalse(Path(f"{path}.part").exists())

    def test_temporary_stream_failure_retries_with_a_clean_part_file(self):
        client, _ = client_with(
            Response({"success": True, "data": {"sid": "sid"}}),
            BrokenStreamResponse(content_type="application/octet-stream"),
            Response(chunks=[b"complete"], content_type="application/octet-stream"),
            retries=2,
        )
        client.login()
        path = self.temp_dir / "retry.xlsx"
        client.download_file("/reports/retry.xlsx", path, 8, 123)
        self.assertEqual(path.read_bytes(), b"complete")
        self.assertFalse(Path(f"{path}.part").exists())


if __name__ == "__main__":
    unittest.main()
