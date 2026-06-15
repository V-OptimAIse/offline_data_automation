import pandas as pd
from openpyxl import load_workbook

from core.logging import log_file_read


class RMStockReader:
    def __init__(self, logger):
        self.logger = logger

    def _get_sheet_name(self, run_date: str) -> str:
        dt = pd.to_datetime(run_date, format="%d-%b-%Y")
        return dt.strftime("%d.%m")

    def _get_sinter_sheet_name(self, sheet_names: list[str], run_date: str) -> str:
        dt = pd.to_datetime(run_date, format="%d-%b-%Y")
        sept = dt.strftime("%b").upper().replace("SEP", "SEPT")
        candidates = [
            dt.strftime("%B-%Y").upper(),
            dt.strftime("%B %Y").upper(),
            f"{sept}-{dt.year}",
        ]
        sheets = {s.strip().upper(): s for s in sheet_names}
        for name in candidates:
            if name in sheets:
                return sheets[name]
        raise ValueError(f"Sinter DPR sheet not found for {run_date}")

    def read(self, file_path: str, run_date: str) -> tuple[pd.DataFrame, pd.Timestamp]:
        sheet_name = self._get_sheet_name(run_date)

        log_file_read(self.logger, file_path, domain="RM_STOCK", sheet=sheet_name)
        self.logger.info(f"Reading sheet: {sheet_name}")

        try:
            df = pd.read_excel(
                file_path,
                sheet_name=sheet_name,
                usecols="B,T",
                header=0,
            )
        except ValueError:
            raise ValueError(f"Sheet '{sheet_name}' not found in file")

        df.columns = ["material", "physical_stock"]

        # Clean
        df["material"] = df["material"].astype(str).str.strip()
        df["physical_stock"] = pd.to_numeric(df["physical_stock"], errors="coerce")
        df = df.dropna(subset=["material"])

        # Read timestamp FROM SAME SHEET
        ts = pd.read_excel(
            file_path,
            sheet_name=sheet_name,
            usecols="S",
            nrows=2,
            header=None,
        ).iloc[1, 0]

        return df, ts

    def read_sinter_stock(self, file_path: str, run_date: str) -> pd.DataFrame:
        wb = load_workbook(file_path, read_only=True, data_only=True)
        try:
            sheet_name = self._get_sinter_sheet_name(wb.sheetnames, run_date)
            rows = list(
                wb[sheet_name].iter_rows(
                    min_row=1,
                    max_row=80,
                    values_only=True,
                )
            )
        finally:
            wb.close()

        log_file_read(self.logger, file_path, domain="RM_STOCK_SINTER", sheet=sheet_name)
        self.logger.info(f"Reading sinter stock sheet: {sheet_name}")

        def clean(value) -> str:
            return str(value).strip().lower() if value is not None else ""

        stock_row = next(
            (idx for idx, row in enumerate(rows) if any(clean(v) == "stock qty" for v in row)),
            None,
        )
        if stock_row is None:
            raise ValueError(f"STOCK QTY row not found in sheet '{sheet_name}'")

        date_row = next(
            (idx for idx, row in enumerate(rows) if any(clean(v) == "date" for v in row)),
            4,
        )
        day = pd.to_datetime(run_date, format="%d-%b-%Y").day
        day_col = next(
            (
                idx
                for idx, value in enumerate(rows[date_row])
                if pd.to_numeric(value, errors="coerce") == day
            ),
            None,
        )
        if day_col is None:
            raise ValueError(f"Day {day} not found in sheet '{sheet_name}'")

        stock = pd.to_numeric(rows[stock_row][day_col], errors="coerce")
        if pd.isna(stock) or stock <= 0:
            self.logger.info(
                f"No positive sinter stock value for {run_date} in sheet '{sheet_name}'; "
                "skipping sinter stock row"
            )
            return pd.DataFrame(columns=["material", "physical_stock"])

        return pd.DataFrame(
            {"material": ["Sinter Stock at Yard"], "physical_stock": [stock]}
        )
