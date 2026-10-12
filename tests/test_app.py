from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app


class ManualCliTests(unittest.TestCase):
    def test_skip_download_keeps_manual_cli_processing_path(self):
        cfg = {
            "download": {"download_dir": "downloads"},
            "logging": {"level": "INFO", "dir": "output/logs"},
        }
        runner = Mock()
        with patch.object(sys, "argv", ["app.py", "--mode", "dpr", "--today", "--skip-download"]), \
             patch.object(app, "load_config", return_value=cfg), \
             patch.object(app, "configure_logging", return_value=Mock()), \
             patch.object(app, "_download_for_profiles") as download, \
             patch.object(app, "run_processing", runner):
            app.main()

        download.assert_not_called()
        self.assertEqual(runner.call_args.args[0], ["dpr"])
        self.assertTrue(runner.call_args.kwargs["skip_download"])


if __name__ == "__main__":
    unittest.main()
