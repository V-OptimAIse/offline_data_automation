# src/domains/rm/neon_mapper.py

import pandas as pd
from collections import defaultdict
from typing import Iterator


DEDICATED_SINTER_CATEGORIES = {
    "sinter sp 01 online",
    "sinter sp 02 online",
}


class RMNeonMapper:
    def __init__(
        self,
        material_codes: set[str],
        category_map: dict,
        schema: str | None = None,
        table_columns: dict[str, set[str]] | None = None,
        logger=None,
    ):
        self.material_codes = {code.lower() for code in (material_codes or set())}
        self.category_map = category_map
        self.schema = schema
        self.table_columns = table_columns or {}
        self.logger = logger

    def _parse(self, col: str) -> tuple[str, str] | None:
        col_l = col.lower().strip()
        for pattern in self.category_map:
            if col_l.startswith(pattern + " "):
                return pattern, col_l[len(pattern) + 1:]
        return None

    def _target_table(self, table: str) -> str:
        if self.schema and "." not in table:
            return f"{self.schema}.{table}"
        return table

    def _material_code(self, config: dict) -> str | None:
        return config.get("material_code")

    def _is_valid_prop(self, table: str, prop: str) -> bool:
        if not self.table_columns or table not in self.table_columns:
            return True
        return prop in self.table_columns[table]

    def iter_table_dfs(self, df: pd.DataFrame) -> Iterator[tuple[str, pd.DataFrame]]:
        groups: dict[tuple, list] = defaultdict(list)

        for col in (c for c in df.columns if c != "date"):
            parsed = self._parse(col)
            if not parsed:
                continue
            pattern, prop = parsed
            if pattern in DEDICATED_SINTER_CATEGORIES:
                continue
            m = self.category_map[pattern]
            material_code = self._material_code(m)
            if not material_code:
                if self.logger:
                    self.logger.warning(f"RM material_code missing for mapping: {pattern}")
                continue
            groups[(m["table"], material_code)].append((col, prop))

        for (table, material_code), cols_props in groups.items():
            if self.material_codes and material_code.lower() not in self.material_codes:
                if self.logger:
                    self.logger.warning(f"Material code not found in master table: {material_code}")
                continue

            valid_cols_props = []
            for col, prop in cols_props:
                if self._is_valid_prop(table, prop):
                    valid_cols_props.append((col, prop))
                elif self.logger:
                    self.logger.warning(
                        f"Skipping RM property '{prop}' for {table}; column not in target table"
                    )

            if not valid_cols_props:
                continue

            col_names = [c for c, _ in valid_cols_props]
            out = df[["date"] + col_names].copy()
            out = out.rename(columns={"date": "date_time", **{c: p for c, p in valid_cols_props}})
            out["material_code"] = material_code

            prop_cols = [c for c in out.columns if c not in {"date_time", "material_code"}]
            yield self._target_table(table), out[["date_time"] + prop_cols + ["material_code"]]
