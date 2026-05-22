# Offline Data Automation

Offline Data Automation processes plant offline Excel reports, prepares clean output files, and syncs validated data to configured storage systems such as NeonDB and InfluxDB.

The project is organized by business domain so each report type has its own reader, transformer or processor, and service layer.

## Supported Modes

Use the `--mode` argument with one or more comma-separated modes:

- `rm` - raw material chemistry and analysis
- `fines_analysis` - material fines size analysis
- `dpr` - daily production report data
- `hot_metal` - hot metal, slag, and gas analysis
- `rm_hm` - raw material and hot metal strength data
- `rm_stock` - raw material physical stock
- `charge` - charge and dump report processing

`fines` is accepted as an alias for `fines_analysis`.

## Requirements

- Python 3.12 or newer
- Chrome or Chromium for portal downloads
- Access to the configured EML portal when download mode is enabled
- Valid database and InfluxDB credentials when sync is enabled

Install dependencies with `uv`:

```powershell
uv sync
```

Or with pip:

```powershell
python -m pip install -e .
```

## Configuration

Configuration is loaded from YAML files in `src/config` and environment values from `.env`.

Important files:

- `src/config/base.yaml` - download paths, portal URLs, logging, and InfluxDB defaults
- `src/config/rm.yaml` - RM sheet mappings, output fields, NeonDB mapping, and InfluxDB mapping
- `src/config/fines_analysis.yaml` - fines analysis sheet and material mappings
- `src/config/dpr.yaml` - DPR field mappings and sheet discovery rules
- `src/config/hot_metal.yaml` - hot metal sheet and field mappings
- `src/config/rm_hm.yaml` - RM and HM field mappings
- `src/config/charge.yaml` - charge report target tables and batch metadata
- `src/config/rm_stock.yaml` - stock material name mappings

Secrets should be provided through `.env` or `src/config/secrets.yaml`. Do not commit secrets.

## Running

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode rm --today
```

Run for a specific date:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode dpr --rundate 15-05-2026
```

Run for a date range:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode rm,dpr,hot_metal --rundate "01-05-2026 to 15-05-2026"
```

Use existing downloaded files without opening the portal:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode rm_stock --today --skip-download
```

Without `--skip-download`, processing runs only for source files downloaded in the
current execution. If a portal file is unchanged, missing, or fails to download,
that mode is skipped and no database write is attempted from an older local file.

## Logs

Logs are written to both the console and rotating log files under:

```text
output/logs
```

Each run includes a unique `run_id`. Log lines show the executing Python file and line number, making operational review easier.

When a source Excel file is read, the log records the exact device filename and resolved path:

```text
Reading source file | domain=RM | device_file_name='12 BF-02 BUNKER  2026-27.xlsx' | device_path='...'
```

Logging defaults can be changed in `src/config/base.yaml`:

```yaml
logging:
  level: INFO
  dir: output/logs
```

## Outputs

Processed Excel outputs are written under `output` or `outputs`, depending on the domain configuration.

Common output locations:

- `output/rm`
- `output/fines_analysis`
- `output/dpr`
- `output/hot_metal`
- `output/rm_hm`
- `outputs` for charge reports

## Notes

- Use `--skip-download` when files are already present in the configured download directory.
- Keep YAML mappings aligned with the latest Excel sheet names and column formats.
- Database sync depends on valid `neon_developer` and InfluxDB configuration.
- Review logs after each production run for missing sheets, skipped rows, or sync warnings.
