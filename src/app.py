# src/app.py
import argparse
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config_loader import load_config
from core.logging import configure_logging
from infrastructure.selenium_client import SeleniumClient, SeleniumConfig
from domains.download.service import (
    DownloadConfig,
    DownloadResult,
    PortalDownloader,
    format_portal_filename,
)

from domains.rm.service import RMService
from domains.fines_analysis.service import FinesAnalysisService
from domains.dpr.service import DPRService
from domains.hot_metal.service import HotMetalService
from domains.rm_hm.service import RMHMService
from domains.rm_stock.service import RMStockService
from domains.charge.service import ChargeService, ChargeServiceConfig
from domains.dust.service import DustService
from domains.ash.service import AshService

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
RM_STOCK_BULK_PATTERNS = ("RM BULK STOCK*",)
RM_STOCK_SINTER_PATTERNS = ("*DPR SP#2*.xls*",)
DUST_BASIC_PATTERNS = ("*BUNKER*.xlsx",)
DUST_CHEMICAL_PATTERNS = (
    "*GCP DUST CATCHER ESP GRATE BAR SAMPLE ANALYSIS*.xlsx",
)
ASH_PATTERNS = ("*ASH ANALYSIS*.xlsx",)
MODE_FILE_PATTERNS = {
    "rm": ("*BUNKER*.xlsx",),
    "fines_analysis": ("*BUNKER*.xlsx",),
    "dpr": ("*DPR*.xlsx",),
    "hot_metal": ("*HOT METAL*.xlsx",),
    "rm_hm": ("*RM & HM*.xlsx",),
    "rm_stock": RM_STOCK_BULK_PATTERNS,
    "charge": ("CHARGE_AND_DUMP_REPORT_*.xlsx",),
    "ash": ASH_PATTERNS,
}


# -------------------------------------------------
# ARGUMENT PARSING
# -------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser("Offline Data Automation")

    parser.add_argument(
        "--mode",
        required=True,
        help=(
            "Comma separated modes. Supported: rm, fines_analysis, dpr, "
            "hot_metal, rm_hm, rm_stock, charge, dust, ash"
        ),
    )

    parser.add_argument(
        "--today",
        action="store_true",
        help="Use today as run date",
    )

    parser.add_argument(
        "--rundate",
        type=str,
        help="DD-Mon-YYYY | DD-MM-YYYY | 'DD-MM-YYYY to DD-MM-YYYY'",
    )
    parser.add_argument(
        "--skip-download",
        "--skip",
        action="store_true",
        dest="skip_download",
        help="Skip Selenium download step (use existing files)",
    )

    return parser.parse_args()


# -------------------------------------------------
# DATE PARSING (SINGLE + RANGE)
# -------------------------------------------------
def parse_run_dates(raw: str | None, today: bool) -> list[str]:
    if today:
        return [datetime.now(BUSINESS_TZ).strftime("%d-%b-%Y")]

    if not raw:
        raise SystemExit("Provide --today or --rundate")

    raw = raw.strip()

    if "to" in raw.lower():
        start_raw, end_raw = [x.strip() for x in raw.lower().split("to")]

        start = datetime.strptime(start_raw, "%d-%m-%Y").date()
        end = datetime.strptime(end_raw, "%d-%m-%Y").date()

        if start > end:
            raise SystemExit("Start date must be before end date")

        days = (end - start).days + 1
        return [(start + timedelta(days=i)).strftime("%d-%b-%Y") for i in range(days)]

    for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
        try:
            return [datetime.strptime(raw, fmt).strftime("%d-%b-%Y")]
        except ValueError:
            pass

    raise SystemExit(
        "Invalid --rundate.\n"
        "Use:\n"
        "  DD-Mon-YYYY\n"
        "  DD-MM-YYYY\n"
        "  DD-MM-YYYY to DD-MM-YYYY"
    )


def _latest_existing_file(download_dir: Path, patterns: tuple[str, ...]) -> Path | None:
    files = []
    for pattern in patterns:
        files.extend(download_dir.glob(pattern))

    files = [path for path in files if path.is_file()]
    if not files:
        return None

    return max(files, key=lambda path: path.stat().st_mtime)


def _latest_matching_file(files: list[Path], patterns: tuple[str, ...]) -> Path | None:
    matches = [
        path
        for path in files
        if path.is_file()
        and any(fnmatch(path.name.lower(), pattern.lower()) for pattern in patterns)
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _downloaded_files_for_mode(
    mode: str,
    download_result: DownloadResult | None,
    logger,
) -> list[Path]:
    if download_result is None:
        logger.info(f"{mode}: no download result available; skipping read/DB write")
        return []

    outcome = download_result.by_mode.get(mode)
    if outcome is None:
        logger.info(f"{mode}: no download attempted; skipping read/DB write")
        return []

    files = [Path(path) for path in outcome.paths]
    existing_files = [path for path in files if path.exists()]
    missing_files = [path for path in files if not path.exists()]

    for path in missing_files:
        logger.warning(f"{mode}: downloaded file path is missing: {path}")

    if existing_files:
        for path in existing_files:
            logger.info(f"{mode}: processing newly downloaded file: {path}")
        return existing_files

    if outcome.status == "skipped":
        logger.info(f"{mode}: no new portal file; skipping read/DB write")
    elif outcome.status == "failed":
        logger.warning(f"{mode}: download failed; skipping read/DB write")
    else:
        logger.info(f"{mode}: no downloaded file to process; skipping read/DB write")

    return []


def _source_file_for_mode(
    mode: str,
    *,
    skip_download: bool,
    download_dir: Path,
    download_result: DownloadResult | None,
    logger,
) -> Path | None:
    if not skip_download:
        files = _downloaded_files_for_mode(mode, download_result, logger)
        return files[0] if files else None

    source_file = _latest_existing_file(download_dir, MODE_FILE_PATTERNS[mode])
    if source_file is None:
        logger.warning(f"{mode}: no existing source file found; skipping read/DB write")
        return None

    logger.info(f"{mode}: using existing source file: {source_file}")
    return source_file


def _charge_file_for_date(files: list[Path], run_date: str) -> Path | None:
    dt = datetime.strptime(run_date, "%d-%b-%Y")
    stem = f"CHARGE_AND_DUMP_REPORT_{dt.day}_{dt.month}_{dt.year}"
    matches = [path for path in files if stem in path.name]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _charge_source_files(
    *,
    skip_download: bool,
    download_dir: Path,
    download_result: DownloadResult | None,
    logger,
) -> list[Path]:
    if skip_download:
        files = []
        for pattern in MODE_FILE_PATTERNS["charge"]:
            files.extend(download_dir.glob(pattern))
        files = [path for path in files if path.is_file()]
        if not files:
            logger.warning(
                "charge: no existing source files found; skipping read/DB write"
            )
        return files

    return _downloaded_files_for_mode("charge", download_result, logger)


def _rm_stock_source_files(
    *,
    skip_download: bool,
    download_dir: Path,
    download_result: DownloadResult | None,
    logger,
) -> tuple[Path | None, Path | None]:
    downloaded = [] if skip_download else _downloaded_files_for_mode(
        "rm_stock",
        download_result,
        logger,
    )
    if not skip_download and not downloaded:
        return None, None

    bulk_file = _latest_matching_file(downloaded, RM_STOCK_BULK_PATTERNS)
    sinter_file = _latest_matching_file(downloaded, RM_STOCK_SINTER_PATTERNS)
    bulk_file = bulk_file or _latest_existing_file(download_dir, RM_STOCK_BULK_PATTERNS)
    sinter_file = sinter_file or _latest_existing_file(download_dir, RM_STOCK_SINTER_PATTERNS)

    if bulk_file:
        logger.info(f"rm_stock: using bulk stock source file: {bulk_file}")
    else:
        logger.warning("rm_stock: no RM BULK STOCK source file found")

    if sinter_file:
        logger.info(f"rm_stock: using sinter stock source file: {sinter_file}")
    else:
        logger.warning("rm_stock: no DPR SP#2 source file found; sinter stock skipped")

    return bulk_file, sinter_file


def _dust_source_files(
    *,
    skip_download: bool,
    download_dir: Path,
    download_result: DownloadResult | None,
    logger,
) -> tuple[Path | None, Path | None]:
    if skip_download:
        basic_file = _latest_existing_file(download_dir, DUST_BASIC_PATTERNS)
        chemical_file = _latest_existing_file(download_dir, DUST_CHEMICAL_PATTERNS)
    else:
        files = _downloaded_files_for_mode("dust", download_result, logger)
        basic_file = _latest_matching_file(files, DUST_BASIC_PATTERNS)
        chemical_file = _latest_matching_file(files, DUST_CHEMICAL_PATTERNS)

    if basic_file:
        logger.info(f"dust: using basic-analysis source file: {basic_file}")
    else:
        logger.info("dust: no basic-analysis source file selected")

    if chemical_file:
        logger.info(f"dust: using chemical-analysis source file: {chemical_file}")
    else:
        logger.info("dust: no chemical-analysis source file selected")

    return basic_file, chemical_file


def _ash_source_files(
    *,
    skip_download: bool,
    download_dir: Path,
    download_result: DownloadResult | None,
    run_dates: list[str],
    filename_template: str,
    logger,
) -> list[Path]:
    if skip_download:
        candidates = [
            path
            for pattern in ASH_PATTERNS
            for path in download_dir.glob(pattern)
            if path.is_file() and not path.name.startswith("~$")
        ]
        expected_names = tuple(
            dict.fromkeys(
                format_portal_filename(filename_template, run_date)
                for run_date in run_dates
            )
        )
        files = []
        for expected_name in expected_names:
            normalized_expected = _normalize_filename(expected_name)
            matches = [
                path
                for path in candidates
                if normalized_expected in _normalize_filename(path.name)
            ]
            if matches:
                files.append(max(matches, key=lambda path: path.stat().st_mtime))

        files = list(dict.fromkeys(files))
        if not files and candidates:
            fallback = max(candidates, key=lambda path: path.stat().st_mtime)
            logger.warning(
                "ash: no local filename matched the requested financial year; "
                f"using latest keyword match: {fallback}"
            )
            files = [fallback]
    else:
        downloaded = _downloaded_files_for_mode("ash", download_result, logger)
        files = [
            path
            for path in downloaded
            if any(
                fnmatch(path.name.lower(), pattern.lower())
                for pattern in ASH_PATTERNS
            )
        ]

    if files:
        for path in files:
            logger.info(f"ash: using source file: {path}")
    else:
        logger.warning("ash: no source file found; skipping read/DB write")
    return files


def _normalize_filename(value: str) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    args = parse_args()
    cfg = load_config()

    logging_cfg = cfg.get("logging") or {}
    logger = configure_logging(
        level=logging_cfg.get("level", "INFO"),
        log_dir=logging_cfg.get("dir", "output/logs"),
    )

    mode_aliases = {"fines": "fines_analysis"}
    modes = [
        mode_aliases.get(m.strip().lower(), m.strip().lower())
        for m in args.mode.split(",")
        if m.strip()
    ]
    valid_modes = {
        "rm",
        "fines_analysis",
        "dpr",
        "hot_metal",
        "rm_hm",
        "charge",
        "rm_stock",
        "dust",
        "ash",
    }

    invalid = set(modes) - valid_modes
    if invalid:
        raise SystemExit(f"Unsupported modes: {sorted(invalid)}")

    # -------------------------------------------------
    # RESOLVE RUN DATES
    # -------------------------------------------------
    run_dates = parse_run_dates(args.rundate, args.today)
    logger.info(
        f"Run dates resolved: {run_dates[0]} to {run_dates[-1]} "
        f"({len(run_dates)} day(s))"
    )
    download_dir = Path(cfg["download"]["download_dir"]).expanduser()

    # -------------------------------------------------
    # DOWNLOAD STEP (OPTIONAL)
    # -------------------------------------------------
    download_result: DownloadResult | None = None
    if args.skip_download:
        logger.info("Skipping download step (using existing files)")
    else:
        logger.info("Starting download step")

        selenium = SeleniumClient(
            SeleniumConfig(default_timeout=int(cfg["download"]["default_timeout"]))
        )

        downloader = PortalDownloader(
            selenium,
            DownloadConfig(
                download_dir=cfg["download"]["download_dir"],
                metadata_path=cfg["download"]["metadata_path"],
                file_station_url=cfg["eml"]["file_station_url"],
                hourly_url=cfg["eml"]["hourly_url"],
                file_station_search_path=cfg["eml"].get("file_station_search_path"),
                portal_files=cfg.get("portal_files", {}),
            ),
            logger,
        )

        try:
            logger.info("Starting browser for portal login")
            selenium.start()

            logger.info("Logging in to portal")
            selenium.login(
                login_url=cfg["eml"]["login_url"],
                user=cfg["eml"]["user"],
                password=cfg["eml"]["password"],
            )
            logger.info("Login successful; moving to File Station download flow")

            download_result = downloader.download(
                modes=modes,
                run_dates=run_dates,
                is_today_mode=bool(args.today),
            )

            logger.info(
                "Download completed. "
                f"Downloaded modes: {sorted(download_result.downloaded_modes)} | "
                f"Partial modes: {sorted(download_result.partial_modes)} | "
                f"Skipped modes: {sorted(download_result.skipped_modes)} | "
                f"Failed modes: {sorted(download_result.failed_modes)}"
            )

        finally:
            selenium.stop()

    # -------------------------------------------------
    # RM
    # -------------------------------------------------
    if "rm" in modes:
        rm_file = _source_file_for_mode(
            "rm",
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if rm_file:
            RMService(logger).process(str(rm_file), cfg, run_dates)

    # -------------------------------------------------
    # FINES ANALYSIS
    # -------------------------------------------------
    if "fines_analysis" in modes:
        fines_file = _source_file_for_mode(
            "fines_analysis",
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if fines_file:
            FinesAnalysisService(logger).process(str(fines_file), cfg, run_dates)

    # -------------------------------------------------
    # DPR
    # -------------------------------------------------
    if "dpr" in modes:
        dpr_file = _source_file_for_mode(
            "dpr",
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if dpr_file:
            DPRService(logger).process(str(dpr_file), cfg, run_dates)

    # -------------------------------------------------
    # HOT METAL
    # -------------------------------------------------
    if "hot_metal" in modes:
        hm_file = _source_file_for_mode(
            "hot_metal",
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if hm_file:
            HotMetalService(logger).process(str(hm_file), cfg, run_dates)

    # -------------------------------------------------
    # RM & HM
    # -------------------------------------------------
    if "rm_hm" in modes:
        rm_hm_file = _source_file_for_mode(
            "rm_hm",
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if rm_hm_file:
            RMHMService(
                logger,
                neon_cfg=cfg["neon_developer"],
                write_to_neon=True,
            ).process(str(rm_hm_file), cfg, run_dates)

    # -------------------------------------------------
    # RM STOCK
    # -------------------------------------------------
    if "rm_stock" in modes:
        stock_file, sinter_stock_file = _rm_stock_source_files(
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )

        if stock_file:
            RMStockService(logger).process(
                file_path=str(stock_file),
                cfg=cfg,
                run_dates=run_dates,
                sinter_file_path=str(sinter_stock_file) if sinter_stock_file else None,
            )

    # -------------------------------------------------
    # CHARGE
    # -------------------------------------------------
    if "charge" in modes:
        charge_files = _charge_source_files(
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )

        charge_service = ChargeService(
            ChargeServiceConfig(
                output_dir="outputs",
                neon_cfg=cfg["neon_developer"],
                setting_cfg=cfg,
                charge_yaml_path="src/config/charge.yaml",
                write_to_neon=True,
            ),
            logger,
        )

        for run_date in run_dates:
            charge_file = _charge_file_for_date(charge_files, run_date)

            if not charge_file:
                if args.skip_download:
                    logger.error(f"Charge file not found for {run_date}")
                else:
                    logger.info(
                        f"Charge file not downloaded for {run_date}; "
                        "skipping read/DB write"
                    )
                continue

            charge_service.run(
                charge_file=str(charge_file),
                run_date_str=run_date,
            )

    # -------------------------------------------------
    # DUST ANALYSIS
    # -------------------------------------------------
    if "dust" in modes:
        dust_basic_file, dust_chemical_file = _dust_source_files(
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            logger=logger,
        )
        if dust_basic_file or dust_chemical_file:
            DustService(logger).process(
                basic_file=str(dust_basic_file) if dust_basic_file else None,
                chemical_file=(
                    str(dust_chemical_file) if dust_chemical_file else None
                ),
                setting_cfg=cfg,
                run_dates=run_dates,
            )

    # -------------------------------------------------
    # ASH ANALYSIS
    # -------------------------------------------------
    if "ash" in modes:
        ash_files = _ash_source_files(
            skip_download=args.skip_download,
            download_dir=download_dir,
            download_result=download_result,
            run_dates=run_dates,
            filename_template=(cfg.get("portal_files") or {}).get(
                "ash",
                "ASH ANALYSIS {financial_year_short}",
            ),
            logger=logger,
        )
        if ash_files:
            AshService(logger).process(
                file_paths=[str(path) for path in ash_files],
                setting_cfg=cfg,
                run_dates=run_dates,
            )
    logger.info("Offline data automation completed successfully.")


if __name__ == "__main__":
    main()
