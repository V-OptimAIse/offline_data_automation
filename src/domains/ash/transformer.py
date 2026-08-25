from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


class AshTransformer:
    def __init__(self, logger):
        self.logger = logger

    def transform(
        self,
        df: pd.DataFrame,
        cfg: dict[str, Any],
        run_dates: list[date],
    ) -> pd.DataFrame:
        fields = list(cfg.get("reader", {}).get("column_map", {}))
        if "date" not in fields:
            raise ValueError("Ash reader.column_map must define the date column")

        measurement_fields = [field for field in fields if field != "date"]
        output_columns = ["material_type", "date", *measurement_fields]
        if df.empty:
            return pd.DataFrame(columns=output_columns)

        out = df.reindex(columns=output_columns).copy()
        out["date"] = pd.to_datetime(
            out["date"],
            errors="coerce",
            dayfirst=True,
            format="mixed",
        ).dt.date
        out["material_type"] = out["material_type"].astype("string").str.strip()

        for field in measurement_fields:
            numeric = pd.to_numeric(out[field], errors="coerce")
            out[field] = numeric.mask(numeric.lt(0))

        out = out[
            out["date"].isin(set(run_dates))
            & out["material_type"].notna()
            & out["material_type"].ne("")
        ]
        out = out.dropna(subset=measurement_fields, how="all")
        if out.empty:
            return pd.DataFrame(columns=output_columns)

        before = len(out)
        out = (
            out.groupby(["material_type", "date"], as_index=False, dropna=False)[
                measurement_fields
            ]
            .mean()
            .reindex(columns=output_columns)
        )
        collapsed = before - len(out)
        if collapsed:
            self.logger.info(
                f"Ash analysis: averaged {collapsed} duplicate same-day sample(s)"
            )

        out[measurement_fields] = out[measurement_fields].round(4)
        return out.sort_values(["date", "material_type"]).reset_index(drop=True)
