# Offline Data Automation

This project downloads plant Excel reports from Synology File Station, runs the existing domain readers/processors, and writes validated data to the configured NeonDB, PI_DB, and InfluxDB targets.

Downloads and monitoring use the Synology File Station HTTP API. Chrome, Chromium, Selenium, and browser tokens are not required.

## Architecture

`src/infrastructure/synology_api_client.py` maintains one authenticated `requests.Session` per configured profile. It lists folder metadata with pagination and streams downloads to `filename.xlsx.part`; the byte count is validated before an atomic rename. The local file modification time is set to the NAS `mtime`.

`src/monitor.py` is a single-process service. Every 60 seconds by default it performs three directory listings:

- profile 1: `/V-Optimaise Data`
- profile 1: `/V-Optimaise Data/BF2 AUTO REPORTS/CHARGE AND DUMP/HOURLY`
- profile 2: `/QC_LAB_DATA`

It resolves all required files locally from those listings. A source changes when its remote path, integer `mtime`, or byte size changes. After a configurable stability delay, changed sources are rechecked once per affected folder, downloaded once, and fanned out only to their dependent modes. Multi-file modes reuse a current canonical local companion or download the companion if it is missing or stale.

Completed revisions are stored atomically in `output/state/synology_monitor_state.json`. State is committed only after processing and database work return successfully, so failed revisions remain retryable. On the first startup, current files are recorded as a baseline without processing unless `--process-initial` is explicitly supplied.

Whenever a changed source triggers a mode, the monitor processes the rolling non-charge window from day before yesterday through today. For example, a run on 14-Oct-2026 processes 12-Oct-2026, 13-Oct-2026, and 14-Oct-2026. Charge is intentionally excluded from that lookback and processes today only. The dates are calculated in the Asia/Kolkata timezone on every cycle.

## Requirements and installation

- Python 3.12 or newer
- HTTPS access to `https://ithelpdesk-ugml.sg4.quickconnect.to/webapi/`
- Synology File Station permissions for both accounts
- Database credentials for enabled write targets

Install with:

```bash
uv sync
```

or:

```bash
python -m pip install -e .
```

## Configuration

The main settings are in `src/config/base.yaml`:

```yaml
eml:
  base_url: "https://ithelpdesk-ugml.sg4.quickconnect.to"
  charge_folder_path: "/V-Optimaise Data/BF2 AUTO REPORTS/CHARGE AND DUMP/HOURLY"

monitor:
  poll_interval_seconds: 60
  stability_delay_seconds: 10
  state_path: "output/state/synology_monitor_state.json"
  process_initial_snapshot: false
  request_timeout_seconds: 30
  download_timeout_seconds: 300
  retry_attempts: 3
  retry_backoff_seconds: 5
```

Keep credentials only in `.env`:

```dotenv
EML_PROFILE_1_USER=
EML_PROFILE_1_PASSWORD=
EML_PROFILE_2_USER=
EML_PROFILE_2_PASSWORD=
```

Profile 1 owns `charge`, `dpr`, `rm_strength`, `rm_stock`, and `ash`. Profile 2 owns `rm`, `fines_analysis`, `hot_metal`, and `dust`. Stable identifiers under `portal_files` tolerate financial-year/month changes, revision suffixes, punctuation and spacing changes, HTML ampersands, and supported Excel extensions.

Authentication session IDs and Synology tokens are held only in memory and are never logged or written to disk.

## Manual processing

Run commands from the repository root:

```bash
python src/app.py --mode rm --today
python src/app.py --mode rm,dpr,hot_metal --today
python src/app.py --mode dpr --rundate 15-05-2026
python src/app.py --mode rm,dpr --rundate "01-05-2026 to 15-05-2026"
python src/app.py --mode rm_stock --today --skip-download
```

Without `--skip-download`, the requested profile logs in through the API, lists each required folder once, downloads canonical filenames, and invokes only the requested domain services. `--skip` remains an alias for `--skip-download`.

## Monitoring

Test one cycle safely. On a new installation this records the initial baseline without processing:

```bash
python src/monitor.py --once
```

Preview detected changes without downloads, database work, or state updates:

```bash
python src/monitor.py --once --dry-run
```

Explicitly process the initial snapshot:

```bash
python src/monitor.py --once --process-initial
```

Run continuously:

```bash
python src/monitor.py
```

Temporary network, QuickConnect, session, NAS, download, and processing failures leave completed state untouched. Continuous mode logs the failure and retries on the next cycle. `SIGINT` and `SIGTERM` finish the current operation, stop the loop, and attempt to log out both API sessions.

## Logs

Console and rotating file logs are written under `output/logs` by default. Follow the service log with:

```bash
journalctl -u offline-data-synology-monitor.service -f
```

Look for `Change detected`, `Source stable`, `Downloading`, `Triggered modes`, `Processing completed`, and `State committed` events. Unchanged filenames are not logged individually at INFO level.

## systemd deployment

Copy `deployment/offline-data-synology-monitor.service.example` to `/etc/systemd/system/offline-data-synology-monitor.service`. The template uses `/home/voptimaise/Desktop/evonith/dataloading/offline_data/offline_data_automation`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now offline-data-synology-monitor.service
sudo systemctl status offline-data-synology-monitor.service
journalctl -u offline-data-synology-monitor.service -f
```

Validate the new watcher and database results before disabling the old hourly application timers.

### Scheduled daily charge and weekly backfill

The deployment directory also contains two scheduled jobs. Each job needs both its `.service` and `.timer` unit:

- `offline-data-charge-daily`: runs every day at 00:10 Asia/Kolkata and processes charge for yesterday.
- `offline-data-weekly-backfill`: runs every Sunday at 00:30 Asia/Kolkata and processes all modes for the previous seven completed days, Sunday through Saturday.

The weekly job starts 20 minutes after the daily job. Both services use the same `flock` file, so the weekly job waits if the daily job is still running. They download current source files through the Synology API and then perform the requested database writes.

Install and enable the units on the Raspberry Pi:

```bash
cd /home/voptimaise/Desktop/evonith/dataloading/offline_data/offline_data_automation

sudo install -m 0644 deployment/offline-data-charge-daily.service.example /etc/systemd/system/offline-data-charge-daily.service
sudo install -m 0644 deployment/offline-data-charge-daily.timer.example /etc/systemd/system/offline-data-charge-daily.timer
sudo install -m 0644 deployment/offline-data-weekly-backfill.service.example /etc/systemd/system/offline-data-weekly-backfill.service
sudo install -m 0644 deployment/offline-data-weekly-backfill.timer.example /etc/systemd/system/offline-data-weekly-backfill.timer

sudo systemctl daemon-reload
sudo systemctl enable --now offline-data-charge-daily.timer
sudo systemctl enable --now offline-data-weekly-backfill.timer
systemctl list-timers --all | grep offline-data
```

Test each service without waiting for its timer:

```bash
sudo systemctl start offline-data-charge-daily.service
sudo systemctl status offline-data-charge-daily.service
sudo journalctl -u offline-data-charge-daily.service -n 100 --no-pager

sudo systemctl start offline-data-weekly-backfill.service
sudo systemctl status offline-data-weekly-backfill.service
sudo journalctl -u offline-data-weekly-backfill.service -n 100 --no-pager
```

The `OnFailure` entries use the existing `offline-data-automation-failure@.service` mail handler. Both scheduled services append uncaught errors to `/home/voptimaise/Desktop/evonith/dataloading/offline_data/offline_data_automation/logs/offline-data-error.log`, so update the old mail handler to tail that path. If the failure handler is not installed, remove the `OnFailure` lines.

## Supported modes

The existing business processing remains available for `rm`, `fines_analysis` (`fines` alias), `dpr`, `hot_metal`, `rm_strength`, `rm_stock`, `charge`, `dust`, and `ash`. Domain sheet mappings remain in `src/config/*.yaml`; this API change does not alter database schemas or transformations.
