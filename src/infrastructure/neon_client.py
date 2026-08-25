# src/infrastructure/neon_client.py

import psycopg2
from psycopg2.extras import Json, execute_values
import pandas as pd
from uuid import uuid4


class NeonClient:
    def __init__(self, db_config: dict):
        db_url = (db_config or {}).get("url")
        if not db_url:
            raise ValueError("PostgreSQL DB url is missing. Check secrets.yaml and .env.")

        self.conn = psycopg2.connect(db_url)
        self.conn.autocommit = True

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        identifier = str(identifier).strip()
        if not identifier:
            raise ValueError("SQL identifier cannot be empty")
        if identifier.startswith('"') and identifier.endswith('"'):
            identifier = identifier[1:-1].replace('""', '"')
        return '"' + identifier.replace('"', '""') + '"'

    @classmethod
    def _quote_qualified_name(cls, name: str) -> str:
        return ".".join(cls._quote_identifier(part) for part in str(name).split("."))

    @classmethod
    def _quote_columns(cls, columns: list[str]) -> str:
        return ", ".join(cls._quote_identifier(col) for col in columns)

    @staticmethod
    def _sql_value(value):
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        if hasattr(value, "item"):
            return value.item()
        return value

    @staticmethod
    def _null_non_positive_payload_values(
        df: pd.DataFrame,
        exclude_cols: set[str],
    ) -> pd.DataFrame:
        """
        DB payload rule: values <= 0 are stored as NULL.

        Conflict/key columns are excluded so row identity is preserved.
        """
        for col in (c for c in df.columns if c not in exclude_cols):
            series = df[col]
            if (
                pd.api.types.is_bool_dtype(series)
                or pd.api.types.is_datetime64_any_dtype(series)
            ):
                continue

            numeric = pd.to_numeric(series, errors="coerce")
            mask = numeric.notna() & numeric.le(0)
            if mask.any():
                df.loc[mask, col] = pd.NA

        return df

    def _sync_serial_id_sequence(self, cur, table_name: str, table_sql: str) -> None:
        cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table_name,))
        sequence_name = cur.fetchone()[0]
        if not sequence_name:
            return

        cur.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence(%s, 'id'),
                (SELECT GREATEST(COALESCE(MAX(id), 0), 1) FROM {table_sql})
            )
            """,
            (table_name,),
        )

    # ------------------------------------------------------------------
    def insert_dataframe(
        self,
        df: pd.DataFrame,
        table_name: str,
        conflict_cols: list[str] | None = None,
        upsert_mode: str = "update_insert",
        null_non_positive_values: bool = True,
    ) -> int:
        """
        Upsert `df` into `table_name`.

        upsert_mode="update_insert" - updates matching rows, inserts missing rows,
                                      and never deletes existing data.
        upsert_mode="on_conflict"   - requires a UNIQUE/PK constraint on conflict_cols.
        upsert_mode="delete_insert" - deletes matching rows, then inserts.
        null_non_positive_values=False preserves valid zero/negative payload values.
        Returns the number of rows processed.
        """
        # update_insert is the preferred rerun mode for ingestion jobs: it
        # updates existing keys and inserts missing keys without deleting rows.
        if df.empty:
            return 0

        valid_upsert_modes = {"update_insert", "on_conflict", "delete_insert"}
        if upsert_mode not in valid_upsert_modes:
            raise ValueError(
                f"Unsupported upsert_mode for {table_name}: {upsert_mode!r}"
            )

        df = df.copy()
        conflict_cols = conflict_cols or ["material_id", "date_time"]

        missing_conflict_cols = [c for c in conflict_cols if c not in df.columns]
        if missing_conflict_cols:
            raise ValueError(
                f"Missing conflict columns for {table_name}: {missing_conflict_cols}"
            )

        if "date_time" in df.columns:
            df["date_time"] = pd.to_datetime(df["date_time"], errors="coerce")
            if df["date_time"].dt.tz is None:
                df["date_time"] = df["date_time"].dt.tz_localize("Asia/Kolkata")
            df["date_time"] = df["date_time"].dt.tz_convert("UTC")

        df = df.dropna(subset=conflict_cols)
        payload_cols = [c for c in df.columns if c not in conflict_cols]
        if payload_cols:
            df = df.dropna(subset=payload_cols, how="all")
        if df.empty:
            return 0

        if null_non_positive_values:
            df = self._null_non_positive_payload_values(
                df=df,
                exclude_cols=set(conflict_cols),
            )

        cols = list(df.columns)
        table_sql = self._quote_qualified_name(table_name)
        cols_sql = self._quote_columns(cols)
        conflict_cols_sql = self._quote_columns(conflict_cols)
        values = [
            tuple(self._sql_value(value) for value in row)
            for row in df.itertuples(index=False, name=None)
        ]

        if upsert_mode == "update_insert":
            df = df.drop_duplicates(subset=conflict_cols, keep="last")
            cols = list(df.columns)
            cols_sql = self._quote_columns(cols)
            values = [
                tuple(self._sql_value(value) for value in row)
                for row in df.itertuples(index=False, name=None)
            ]

            temp_table = self._quote_identifier(f"_neon_upsert_{uuid4().hex}")
            match_conditions = " AND ".join(
                f"t.{self._quote_identifier(c)} = s.{self._quote_identifier(c)}"
                for c in conflict_cols
            )
            update_cols = [c for c in cols if c not in conflict_cols]

            if update_cols:
                update_sql = ", ".join(
                    f"{self._quote_identifier(c)} = s.{self._quote_identifier(c)}"
                    for c in update_cols
                )
                update_query = f"""
                    UPDATE {table_sql} AS t
                    SET {update_sql}
                    FROM {temp_table} AS s
                    WHERE {match_conditions}
                """
            else:
                update_query = None

            select_cols_sql = ", ".join(
                f"s.{self._quote_identifier(c)}" for c in cols
            )
            insert_query = f"""
                INSERT INTO {table_sql} ({cols_sql})
                SELECT {select_cols_sql}
                FROM {temp_table} AS s
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM {table_sql} AS t
                    WHERE {match_conditions}
                )
            """
            temp_insert_query = f"INSERT INTO {temp_table} ({cols_sql}) VALUES %s"

            with self.conn.cursor() as cur:
                cur.execute("BEGIN")
                try:
                    select_typed_cols_sql = ", ".join(
                        f"{self._quote_identifier(c)}" for c in cols
                    )
                    cur.execute(
                        f"""
                        CREATE TEMP TABLE {temp_table} ON COMMIT DROP AS
                        SELECT {select_typed_cols_sql}
                        FROM {table_sql}
                        WHERE FALSE
                        """
                    )
                    execute_values(cur, temp_insert_query, values)
                    if update_query:
                        cur.execute(update_query)
                    self._sync_serial_id_sequence(cur, table_name, table_sql)
                    cur.execute(insert_query)
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise

            return len(values)

        if upsert_mode == "delete_insert":
            delete_keys = [
                tuple(self._sql_value(v) for v in row)
                for row in df[conflict_cols].drop_duplicates().itertuples(index=False, name=None)
            ]

            # Explicit ::timestamptz cast prevents PostgreSQL from treating the
            # date_time value as text in the VALUES derived table, which would
            # cause the WHERE clause to silently match nothing.
            delete_template = "({})".format(
                ", ".join(
                    "%s::timestamptz" if col == "date_time" else "%s"
                    for col in conflict_cols
                )
            )
            delete_conditions = " AND ".join(
                f"t.{self._quote_identifier(c)} = v.{self._quote_identifier(c)}"
                for c in conflict_cols
            )
            delete_sql = f"""
                DELETE FROM {table_sql} AS t
                USING (VALUES %s) AS v ({conflict_cols_sql})
                WHERE {delete_conditions}
            """
            insert_sql = f"INSERT INTO {table_sql} ({cols_sql}) VALUES %s"

            # DELETE + INSERT must be atomic: a failed INSERT after a successful
            # DELETE would otherwise leave the table with missing rows.
            with self.conn.cursor() as cur:
                cur.execute("BEGIN")
                try:
                    if delete_keys:
                        execute_values(cur, delete_sql, delete_keys, template=delete_template)

                    self._sync_serial_id_sequence(cur, table_name, table_sql)
                    execute_values(cur, insert_sql, values)
                    cur.execute("COMMIT")
                except Exception:
                    cur.execute("ROLLBACK")
                    raise

            return len(values)

        # on_conflict mode (requires UNIQUE/PK constraint on conflict_cols)
        update_cols = [c for c in cols if c not in conflict_cols]
        if not update_cols:
            query = f"""
                INSERT INTO {table_sql} ({cols_sql})
                VALUES %s
                ON CONFLICT ({conflict_cols_sql}) DO NOTHING
            """
        else:
            update_sql = ", ".join(
                f"{self._quote_identifier(c)} = EXCLUDED.{self._quote_identifier(c)}"
                for c in update_cols
            )
            query = f"""
                INSERT INTO {table_sql} ({cols_sql})
                VALUES %s
                ON CONFLICT ({conflict_cols_sql})
                DO UPDATE SET
                {update_sql}
            """

        with self.conn.cursor() as cur:
            execute_values(cur, query, values)

        return len(values)

    def delete_dataframe_keys(
        self,
        df: pd.DataFrame,
        table_name: str,
        conflict_cols: list[str],
    ) -> int:
        """
        Delete rows from `table_name` using exact key rows from `df`.
        """
        if df.empty:
            return 0

        df = df.copy()
        missing_conflict_cols = [c for c in conflict_cols if c not in df.columns]
        if missing_conflict_cols:
            raise ValueError(
                f"Missing conflict columns for {table_name}: {missing_conflict_cols}"
            )

        if "date_time" in df.columns:
            df["date_time"] = pd.to_datetime(df["date_time"], errors="coerce")
            if df["date_time"].dt.tz is None:
                df["date_time"] = df["date_time"].dt.tz_localize("Asia/Kolkata")
            df["date_time"] = df["date_time"].dt.tz_convert("UTC")

        df = df.dropna(subset=conflict_cols)
        df = df.drop_duplicates(subset=conflict_cols)
        if df.empty:
            return 0

        table_sql = self._quote_qualified_name(table_name)
        conflict_cols_sql = self._quote_columns(conflict_cols)
        delete_conditions = " AND ".join(
            f"t.{self._quote_identifier(c)} = v.{self._quote_identifier(c)}"
            for c in conflict_cols
        )
        delete_template = "({})".format(
            ", ".join(
                "%s::timestamptz" if col == "date_time" else "%s"
                for col in conflict_cols
            )
        )
        delete_sql = f"""
            DELETE FROM {table_sql} AS t
            USING (VALUES %s) AS v ({conflict_cols_sql})
            WHERE {delete_conditions}
        """
        values = [
            tuple(self._sql_value(value) for value in row)
            for row in df[conflict_cols].itertuples(index=False, name=None)
        ]

        with self.conn.cursor() as cur:
            execute_values(cur, delete_sql, values, template=delete_template)
            return max(cur.rowcount, 0)
    

    def write_hotmetal_dataframe(self, df: pd.DataFrame, source_file: str):
        df = df.copy()

        # Align column name with DB logic
        if "date" in df.columns:
            df = df.rename(columns={"date": "date_time"})

        return self.insert_dataframe(
            df=df,
            table_name="hot_metal_chemistry",
            conflict_cols=["lab_sample_id", "date_time"],
            upsert_mode="on_conflict"
        )

    def fetch_material_codes(
        self,
        schema: str = "plant_master",
        table: str = "materials",
        code_column: str = "material_code",
        active_column: str | None = "is_active",
    ) -> set[str]:
        table_sql = self._quote_qualified_name(f"{schema}.{table}")
        code_sql = self._quote_identifier(code_column)

        where = f"WHERE {code_sql} IS NOT NULL"
        if active_column:
            active_sql = self._quote_identifier(active_column)
            where += f" AND COALESCE({active_sql}, TRUE)"

        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {code_sql} FROM {table_sql} {where}")
            return {row[0] for row in cur.fetchall()}

    def fetch_table_columns(
        self,
        schema: str,
        table_names: list[str] | set[str],
    ) -> dict[str, set[str]]:
        names = sorted({str(name).split(".")[-1] for name in table_names})
        if not names:
            return {}

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = ANY(%s)
                """,
                (schema, names),
            )
            out: dict[str, set[str]] = {name: set() for name in names}
            for table_name, column_name in cur.fetchall():
                out.setdefault(table_name, set()).add(column_name)
            return out

    def create_import_batch(
        self,
        import_batch_id: str,
        source_type: str,
        domain: str,
        parser_name: str | None = None,
        source_filename: str | None = None,
        source_path: str | None = None,
        file_sha256: str | None = None,
        row_count: int | None = None,
        metadata: dict | None = None,
        schema: str = "ingest",
        table: str = "import_batches",
    ) -> None:
        table_sql = self._quote_qualified_name(f"{schema}.{table}")
        query = f"""
            INSERT INTO {table_sql} (
                import_batch_id, source_type, domain, parser_name, source_filename,
                source_path, file_sha256, status, row_count, error_count, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s, 0, %s)
            ON CONFLICT (import_batch_id) DO UPDATE SET
                source_type = EXCLUDED.source_type,
                domain = EXCLUDED.domain,
                parser_name = EXCLUDED.parser_name,
                source_filename = EXCLUDED.source_filename,
                source_path = EXCLUDED.source_path,
                file_sha256 = EXCLUDED.file_sha256,
                status = 'running',
                row_count = EXCLUDED.row_count,
                error_count = 0,
                metadata = EXCLUDED.metadata
        """
        with self.conn.cursor() as cur:
            cur.execute(
                query,
                (
                    import_batch_id,
                    source_type,
                    domain,
                    parser_name,
                    source_filename,
                    source_path,
                    file_sha256,
                    row_count,
                    Json(metadata or {}),
                ),
            )

    def update_import_batch(
        self,
        import_batch_id: str,
        status: str,
        row_count: int | None = None,
        error_count: int | None = None,
        metadata: dict | None = None,
        schema: str = "ingest",
        table: str = "import_batches",
    ) -> None:
        table_sql = self._quote_qualified_name(f"{schema}.{table}")
        query = f"""
            UPDATE {table_sql}
            SET status = %s,
                completed_at = CASE
                    WHEN %s IN ('succeeded', 'failed', 'partial', 'skipped')
                    THEN now()
                    ELSE completed_at
                END,
                row_count = COALESCE(%s, row_count),
                error_count = COALESCE(%s, error_count),
                metadata = metadata || %s
            WHERE import_batch_id = %s
        """
        with self.conn.cursor() as cur:
            cur.execute(
                query,
                (
                    status,
                    status,
                    row_count,
                    error_count,
                    Json(metadata or {}),
                    import_batch_id,
                ),
            )


    def close(self):
        self.conn.close()
