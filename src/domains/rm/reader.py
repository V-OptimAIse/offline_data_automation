# src/domains/rm/reader.py

import pandas as pd
from typing import Dict, Any, List, Tuple

from core.logging import log_file_read


class RMReader:
    def __init__(self, logger):
        self.logger = logger

    @staticmethod
    def _normalize_sheet_name(value: str) -> str:
        return " ".join(str(value).casefold().split())

    def _resolve_sheet_name(
        self,
        sheet_names: List[str],
        configured_name: str,
    ) -> str | None:
        if configured_name in sheet_names:
            return configured_name

        normalized_name = self._normalize_sheet_name(configured_name)
        for sheet_name in sheet_names:
            if self._normalize_sheet_name(sheet_name) == normalized_name:
                return sheet_name

        return None

    def read(
        self,
        file_path: str,
        sheet_config: Dict[str, Any],
    ) -> List[Tuple[pd.DataFrame, str, str]]:
        """
        Returns list of (df, prefix, sheet_name)
        """
        log_file_read(self.logger, file_path, domain="RM")
        xls = pd.ExcelFile(file_path)
        frames = []

        for key, cfg in sheet_config.items():
            sheet = cfg["sheet_name"]
            cols = cfg["columns"]
            header = cfg["header_row"] - 1
            prefix = cfg.get("col_prefix", "")

            actual_sheet = self._resolve_sheet_name(xls.sheet_names, sheet)
            if actual_sheet is None:
                self.logger.warning(f"RM sheet missing: {sheet}")
                continue

            if actual_sheet != sheet:
                self.logger.info(
                    f"RM sheet matched: configured={sheet!r}, actual={actual_sheet!r}"
                )

            df = pd.read_excel(
                xls,
                sheet_name=actual_sheet,
                usecols=cols,
                header=header,
            ).dropna(how="all")

            df.columns = [str(c).strip().upper() for c in df.columns]
            frames.append((df.reset_index(drop=True), prefix, sheet))

        return frames
