# Offline Data Automation

Offline Data Automation processes plant offline Excel reports, prepares clean output files, and syncs validated data to configured storage systems such as NeonDB, PI_DB, and InfluxDB.

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
- `dust` - BF2 dust basic and detailed chemical analysis
- `ash` - coke, nut coke, and PCI ash chemical analysis

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
- `src/config/dust.yaml` - dust workbooks, BF2 filter, material codes, tables, and measurements
- `src/config/ash.yaml` - ash sheet patterns, header mappings, material types, and target table

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

Process both dust workbooks for a date or date range:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode dust --rundate 05-08-2026 --skip-download
```

Without `--skip-download`, dust mode resolves both the shared BF-02 BUNKER
workbook and the GCP/Dust Catcher chemical-analysis workbook. It processes only
newly downloaded files; with `--skip-download`, it selects the latest matching
local copy of each workbook.

Process coke, nut coke, and PCI ash analysis from an existing workbook:

```powershell
.\.venv\Scripts\python.exe src\app.py --mode ash --rundate 31-07-2026 --skip-download
```

Without `--skip-download`, ash mode calculates the financial year from each run
date and renders the portal filename template in `base.yaml` (for example,
`ASH ANALYSIS 26-27`). Sheet names and Excel columns are discovered through the
patterns in `ash.yaml`, so financial-year suffixes, spacing changes, and shifted
columns do not require Python changes.

`--skip` is also accepted as a short alias for `--skip-download`.

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
- `output/dust`
- `output/ash`
- `outputs` for charge reports

## Notes

- Use `--skip-download` when files are already present in the configured download directory.
- Keep YAML mappings aligned with the latest Excel sheet names and column formats.
- Database sync depends on valid NeonDB developer, PI_DB, and InfluxDB
  configuration. Set `NEON_DEVELOPER_URL` or `NEON_DB_URL` for the developer
  Neon branch and `PI_DB_URL` for the Raspberry Pi PostgreSQL clone.
- Choose write destinations in `src/config/base.yaml` with `write_db`, for
  example `[neon_db]`, `[influx_db]`, `[pi_db]`, or
  `[neon_db, influx_db, pi_db]`.
- PostgreSQL writes use the same schemas/tables for NeonDB developer and PI_DB,
  with update-then-insert behavior for reruns: matching rows are updated in
  place and missing rows are inserted.
- Review logs after each production run for missing sheets, skipped rows, or sync warnings.
