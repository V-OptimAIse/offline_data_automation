from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from infrastructure.neon_client import NeonClient


DEFAULT_WRITE_DBS = ("neon_db", "influx_db", "pi_db")
WRITE_DB_KEY = "write_db"
DATABASE_CONFIG_KEY = "database"
DATABASE_WRITE_TARGETS_KEY = "write_targets"

POSTGRES_WRITE_DB_TO_CONFIG_KEY = {
    "neon_db": "neon_developer",
    "pi_db": "pi_db",
}
WRITE_DB_ALIASES = {
    "neon": "neon_db",
    "neon_db": "neon_db",
    "neondb": "neon_db",
    "neon_developer": "neon_db",
    "influx": "influx_db",
    "influx_db": "influx_db",
    "influxdb": "influx_db",
    "pi": "pi_db",
    "pi_db": "pi_db",
    "pidb": "pi_db",
}
DEFAULT_DATABASE_TARGET_KEYS = tuple(
    POSTGRES_WRITE_DB_TO_CONFIG_KEY[db]
    for db in DEFAULT_WRITE_DBS
    if db in POSTGRES_WRITE_DB_TO_CONFIG_KEY
)
DATABASE_TARGET_LABELS = {
    "neon_developer": "NeonDB developer",
    "pi_db": "PI_DB",
}


@dataclass(frozen=True)
class DatabaseTarget:
    key: str
    label: str
    config: dict[str, Any]


class DatabaseTargetSyncError(RuntimeError):
    def __init__(self, domain: str, failures: list[tuple[DatabaseTarget, Exception]]):
        labels = ", ".join(target.label for target, _ in failures)
        super().__init__(f"{domain} DB push failed for: {labels}")
        self.failures = failures


def _parse_write_db_value(raw_targets: Any) -> tuple[str, ...]:
    if raw_targets is None:
        return DEFAULT_WRITE_DBS

    if isinstance(raw_targets, str):
        raw_value = raw_targets.strip()
        if not raw_value:
            return ()
        if raw_value.lower() in {"all", "default"}:
            return DEFAULT_WRITE_DBS
        if raw_value.lower() in {"none", "off", "disabled"}:
            return ()
        raw_targets = [part.strip() for part in raw_value.split(",")]

    write_dbs = []
    unsupported = []
    for target in raw_targets:
        raw_target = str(target).strip()
        if not raw_target:
            continue

        normalized = WRITE_DB_ALIASES.get(raw_target.lower())
        if normalized is None:
            unsupported.append(raw_target)
            continue
        write_dbs.append(normalized)

    if unsupported:
        supported = ", ".join(sorted({"neon_db", "influx_db", "pi_db"}))
        raise ValueError(
            f"Unsupported write_db target(s): {unsupported}. "
            f"Supported targets: {supported}"
        )

    return tuple(dict.fromkeys(write_dbs))


def configured_write_dbs(setting_cfg: dict[str, Any]) -> tuple[str, ...]:
    raw_targets = setting_cfg.get(WRITE_DB_KEY)
    if raw_targets is None:
        database_cfg = setting_cfg.get(DATABASE_CONFIG_KEY) or {}
        raw_targets = database_cfg.get(DATABASE_WRITE_TARGETS_KEY)

    return _parse_write_db_value(raw_targets)


def influx_write_enabled(setting_cfg: dict[str, Any]) -> bool:
    return "influx_db" in configured_write_dbs(setting_cfg)


def configured_database_target_keys(
    setting_cfg: dict[str, Any],
    keys: Sequence[str] | None = None,
) -> tuple[str, ...]:
    if keys is not None:
        raw_keys = []
        for key in keys:
            raw_key = str(key).strip()
            if not raw_key:
                continue
            if raw_key in DATABASE_TARGET_LABELS:
                raw_keys.append(raw_key)
                continue
            write_db = WRITE_DB_ALIASES.get(raw_key.lower())
            if write_db in POSTGRES_WRITE_DB_TO_CONFIG_KEY:
                raw_keys.append(POSTGRES_WRITE_DB_TO_CONFIG_KEY[write_db])
            else:
                raw_keys.append(raw_key)
        target_keys = tuple(dict.fromkeys(raw_keys))
    else:
        target_keys = tuple(
            POSTGRES_WRITE_DB_TO_CONFIG_KEY[write_db]
            for write_db in configured_write_dbs(setting_cfg)
            if write_db in POSTGRES_WRITE_DB_TO_CONFIG_KEY
        )

    unsupported = [
        target for target in target_keys if target not in DATABASE_TARGET_LABELS
    ]
    if unsupported:
        supported = ", ".join(sorted(DATABASE_TARGET_LABELS))
        raise ValueError(
            f"Unsupported database write target(s): {unsupported}. "
            f"Supported targets: {supported}"
        )

    return target_keys


def iter_database_targets(
    setting_cfg: dict[str, Any],
    keys: Sequence[str] | None = None,
) -> list[DatabaseTarget]:
    targets: list[DatabaseTarget] = []
    target_keys = configured_database_target_keys(setting_cfg, keys)
    for key in target_keys:
        db_cfg = dict(setting_cfg.get(key) or {})
        db_url = str(db_cfg.get("url") or "").strip()
        if not db_url:
            continue

        db_cfg["url"] = db_url
        targets.append(
            DatabaseTarget(
                key=key,
                label=DATABASE_TARGET_LABELS.get(key, key),
                config=db_cfg,
            )
        )

    return targets


def write_to_database_targets(
    setting_cfg: dict[str, Any],
    logger,
    domain: str,
    writer: Callable[[NeonClient, DatabaseTarget], Any],
    keys: Sequence[str] | None = None,
    raise_errors: bool = True,
) -> dict[str, Any]:
    target_keys = configured_database_target_keys(setting_cfg, keys)
    targets = iter_database_targets(setting_cfg, keys=target_keys)
    configured_keys = {target.key for target in targets}

    for key in target_keys:
        if key not in configured_keys:
            label = DATABASE_TARGET_LABELS.get(key, key)
            logger.warning(f"{label} config missing or empty; skipping {domain} DB push")

    if not targets:
        return {}

    results: dict[str, Any] = {}
    failures: list[tuple[DatabaseTarget, Exception]] = []

    for target in targets:
        logger.info(f"Pushing {domain} data to {target.label}...")
        client = None
        try:
            client = NeonClient(target.config)
            results[target.key] = writer(client, target)
        except Exception as exc:
            logger.exception(f"{domain} push to {target.label} failed")
            failures.append((target, exc))
        finally:
            if client is not None:
                client.close()

    if failures and raise_errors:
        raise DatabaseTargetSyncError(domain, failures)

    return results

