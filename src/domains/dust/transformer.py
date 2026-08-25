from __future__ import annotations

from datetime import date

import pandas as pd


class DustTransformer:
    def __init__(self, logger):
        self.logger = logger

    def transform(
        self,
        df: pd.DataFrame,
        analysis_type: str,
        run_dates: list[date],
        record_columns: dict[str, str],
    ) -> pd.DataFrame:
        material_field = record_columns["material"]
        date_field = record_columns["date"]
        identity_fields = {material_field, date_field}
        fields = [column for column in df.columns if column not in identity_fields]
        output_columns = [material_field, date_field, *fields]
        if df.empty:
            return pd.DataFrame(columns=output_columns)
        if not fields:
            self.logger.warning(
                f"Dust {analysis_type}: no YAML-mapped measurement columns found"
            )
            return pd.DataFrame(columns=output_columns)

        out = df.reindex(columns=output_columns).copy()
        out[date_field] = pd.to_datetime(
            out[date_field],
            errors="coerce",
            dayfirst=True,
            format="mixed",
        ).dt.date
        out[material_field] = out[material_field].astype("string").str.strip()

        for field in fields:
            numeric = pd.to_numeric(out[field], errors="coerce")
            out[field] = numeric.mask(numeric.lt(0))

        out = out[
            out[date_field].isin(set(run_dates))
            & out[material_field].notna()
        ]
        out = out.dropna(subset=list(fields), how="all")
        if out.empty:
            return pd.DataFrame(columns=output_columns)

        before = len(out)
        out = (
            out.groupby(
                [material_field, date_field],
                as_index=False,
                dropna=False,
            )[list(fields)]
            .mean()
            .reindex(columns=output_columns)
        )
        collapsed = before - len(out)
        if collapsed:
            self.logger.info(
                f"Dust {analysis_type}: averaged {collapsed} duplicate same-day sample(s)"
            )

        out[list(fields)] = out[list(fields)].round(4)
        return out.sort_values([date_field, material_field]).reset_index(drop=True)
