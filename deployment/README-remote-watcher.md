# Remote Synology change detection for Raspberry Pi

## Why

Your existing src/domains/download/service.py only reads Synology's modified date
**after** Selenium has logged in. A lightweight DSM File Station metadata poll
avoids starting Chrome for unchanged files. A true event requires a NAS-side
watcher or webhook; the Raspberry Pi cannot use inotify for remote NAS changes.

## Installation (run on Raspberry Pi)

1. Copy `watch_synology.py` to the root of your installed project, and copy
   `deployment/` there. Existing `src/` and YAML mappings are unchanged.
2. Use the Python interpreter from your existing `.venv`. Add the explicit
   `requests` dependency to the project (`uv add requests` / `uv sync`) if it
   is not installed. Existing credentials are loaded from `.env`.
3. **First test DSM API access without changing any metadata or running jobs:**

   ```bash
   cd /path/to/offline_data_automation-uat
   .venv/bin/python watch_synology.py --probe
   ```

   This must print required source names and their mapped modes. A browser
   successfully accessing QuickConnect does **not** prove that `webapi` is
   available to a separate HTTP client. If `--probe` fails, have the client
   authorize a DSM HTTPS/DDNS/VPN API endpoint, or use a NAS-side webhook.

4. **Create a baseline to avoid reprocessing every existing file at startup:**

   ```bash
   .venv/bin/python watch_synology.py --prime
   ```

   Skip `--prime` if you intentionally want all existing sources to be
   processed when the timer starts.

5. Run a manual check twice (two polls must see the same mtime and size):

   ```bash
   .venv/bin/python watch_synology.py
   .venv/bin/python watch_synology.py
   ```

6. Install the timer, with your current directory and user auto-detected:

   ```bash
   bash deployment/install_remote_watch.sh
   systemctl list-timers --all | grep offline-change
   journalctl -u offline-change.service -f
   ```

7. After verifying everything, **disable the previous hourly timer** so it
   does not collide with this detector and update downloaded_metadata.json
   concurrently. Keep a low-frequency safety check if desired, but ensure
   it uses the same process lock and handles service overlaps.

## Behavior

- Per poll: uses `SYNO.FileStation.List` for the two configured profile root
  directories and the special charge folder. It paginates direct children and
  reads only file metadata; Excel content is not transferred during polls.
- Matches stable identifiers from `src/config/base.yaml`.
- Ignores `~$` Excel temporary files and partial/temp downloads.
- Detects filename/path or `mtime` or byte-size differences.
- Requires two successive identical metadata observations to avoid partial
  uploads / half-written Excel files.
- Runs existing `src/app.py --mode <changed_modes> --today` without passing
  `--skip-download`.
- Runs `rm`, `fines_analysis`, and `dust` together if BF-02 BUNKER is updated.
- Retries nonzero process exits on the next poll.
- `--prime` stores the current baseline under `output/remote_watch_state.json`.
  Delete state only if a full re-detection is intended.

## Additional subfolders

The script lists **direct children** of the configured profile search folders.
The source code's existing Selenium downloader also navigates the profile's
specified root. If a source is in an additional nested folder, add its path:

```yaml
remote_watcher:
  folders:
    profile_1:
      - "/V-Optimaise Data/Additional Reports"
    profile_2:
      - "/QC_LAB_DATA/Additional Lab Reports"
```

Then rerun `--probe`. Note: finding a file from an additional watcher folder
**does not** teach the existing Selenium downloader to open that folder. If
the Selenium downloader cannot already access it, you must also update its
folder navigation. Don't scan the entire NAS recursively every minute.

## Important limitation of existing application

`src/domains/download/service.py` writes `downloaded_metadata.json` immediately
after downloading, **before** DB processing. Also, some failure paths log an
error but return exit code 0. A future production-hardening change should
report per-mode success/failure and checkpoint download metadata only after
that mode has been processed and written successfully. Until then, a failed DB
write may not be retried automatically simply by rerunning the watcher.

`--today` processes today's date; if the client changes historical data, change
the processing date policy to include the affected date/range.

## Unverified environment assumptions

This script has unit tests only. It has not been authenticated against your
client NAS. The `--probe` step must be completed on the Pi before enabling.
