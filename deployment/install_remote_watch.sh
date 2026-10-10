#!/usr/bin/env bash
# Run from anywhere on the Raspberry Pi *after* successful --probe and --prime.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_USER="$(id -un)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing $PYTHON. First create/sync your existing project venv." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import requests, yaml, dotenv' 2>/dev/null; then
  echo "Install dependency in .venv: $PYTHON -m pip install requests" >&2
  echo "Or add requests to pyproject.toml and run uv sync." >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/output/remote_watch_state.json" ]]; then
  echo "Baseline state missing. Run '$PYTHON watch_synology.py --prime' from your project root." >&2
  exit 1
fi
sudo tee /etc/systemd/system/offline-change.service >/dev/null <<UNIT
[Unit]
Description=Offline Data Automation - remote Synology change detector
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$PI_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON $PROJECT_DIR/watch_synology.py
TimeoutStartSec=2700
UNIT
sudo cp "$PROJECT_DIR/deployment/offline-change.timer" /etc/systemd/system/offline-change.timer
sudo systemctl daemon-reload
sudo systemctl enable --now offline-change.timer
echo "Enabled offline-change.timer. Inspect with: systemctl list-timers --all | grep offline-change"
echo "Logs: journalctl -u offline-change.service -f"
echo "After verifying, disable your OLD hourly systemd job to avoid duplicate runs."
