from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import pandas as pd
from collections import defaultdict
import pytz
import logging


logger = logging.getLogger(__name__)


class InfluxClient:
    def __init__(self, influx_config: dict):
        self.url = influx_config["url"]
        self.token = influx_config["token"]
        self.org = influx_config["org"]
        self.bucket = influx_config["bucket"]
        self.timestamp_col = influx_config.get("timestamp_col", "date")

        #  IMPORTANT: increase timeout for Cloud
        self.client = InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=30_000,   # 30 seconds
        )

        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

        self.ist = pytz.timezone("Asia/Kolkata")
        self.utc = pytz.UTC

    def close(self):
        self.client.close()

    def write_dataframe(
        self,
        df: pd.DataFrame,
        measurement: str,
        field_mapping: dict | None = None,
        tag_keys: list[str] | None = None,
    ) -> int:
        ts_col = self.timestamp_col
        if ts_col not in df.columns:
            logger.error(f"Timestamp column '{ts_col}' not found. Influx write skipped.")
            return 0

        # Remove duplicate columns
        df = df.loc[:, ~df.columns.duplicated()].copy()

        # Reindex once to required columns (no fragmentation)
        if field_mapping:
            required_cols = set(field_mapping.values())
            required_cols.add(ts_col)
            df = df.reindex(columns=sorted(required_cols), fill_value=pd.NA)

        tag_keys = set(tag_keys or [])
        field_cols = [c for c in df.columns if c not in tag_keys and c != ts_col]
        tag_cols = [c for c in tag_keys if c in df.columns]

        points = []
        write_success = defaultdict(int)
        write_skipped = defaultdict(int)

        for _, row in df.iterrows():
            t = row.get(ts_col)
            if pd.isna(t):
                continue

            ts = pd.to_datetime(t)
            if ts.tzinfo is None:
                ts = self.ist.localize(ts)
            ts = ts.astimezone(self.utc)

            point = Point(measurement).time(ts, WritePrecision.NS)

            # Tags
            for tc in tag_cols:
                v = row.get(tc)
                if pd.notna(v):
                    point = point.tag(tc, str(v))

            has_field = False
            for fc in field_cols:
                v = row.get(fc)
                if pd.isna(v):
                    write_skipped[fc] += 1
                    continue
                try:
                    point = point.field(fc, float(v))
                    write_success[fc] += 1
                    has_field = True
                except Exception:
                    write_skipped[fc] += 1

            if has_field:
                points.append(point)

        if not points:
            logger.warning("No valid points to write to InfluxDB")
            return 0

        #  SINGLE BATCH WRITE (FIXES TIMEOUT)
        self.write_api.write(
            bucket=self.bucket,
            record=points,
        )

        # Summary
        logger.info("InfluxDB write summary:")
        for k in sorted(write_success):
            logger.info(f"  {k}: {write_success[k]} writes")

        skipped = [k for k in field_cols if write_skipped[k] > 0 and write_success[k] == 0]
        if skipped:
            logger.warning("Skipped columns:")
            for k in skipped:
                logger.warning(f"  - {k}: all values NaN or invalid")

        return len(points)
