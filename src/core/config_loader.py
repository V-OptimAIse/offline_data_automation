from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import os
import yaml
import re
from dotenv import load_dotenv


_ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        m = _ENV_PATTERN.match(value.strip())
        if m:
            return os.getenv(m.group(1), "")
    return value


def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = Path(path).resolve()
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_env(env_path: str | Path) -> None:
    p = Path(env_path)
    if p.is_absolute():
        load_dotenv(p, override=False)
        return

    project_root = Path(__file__).resolve().parents[2]
    for candidate in (Path.cwd() / p, project_root / p):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return

    load_dotenv(p.resolve(), override=False)


def _env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            value = value.strip().strip("\"'")
            if value:
                return value
    return ""


def _apply_database_url_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    overrides = {
        "neondb": ("NEON_DB_URL",),
        "neon_prod": ("NEON_PROD_URL",),
        "neon_developer": ("NEON_DEVELOPER_URL", "NEON_DB_URL", "DATABASE_URL"),
        "pi_db": ("PI_DB_URL",),
    }

    for cfg_key, env_names in overrides.items():
        database_url = _env_value(*env_names)
        if not database_url:
            continue

        db_cfg = dict(out.get(cfg_key) or {})
        db_cfg["url"] = database_url
        out[cfg_key] = db_cfg

    return out


def load_config(
    base_path: str = "src/config/base.yaml",
    secrets_path: str = "src/config/secrets.yaml",
    rm_path: str = "src/config/rm.yaml",
    fines_analysis_path: str = "src/config/fines_analysis.yaml",
    dpr_path: str = "src/config/dpr.yaml",
    hot_metal_path: str = "src/config/hot_metal.yaml",
    rm_hm_path: str = "src/config/rm_hm.yaml",
    env_path: str | Path = ".env",
):
    _load_env(env_path)

    base = load_yaml(base_path)
    secrets = load_yaml(secrets_path)
    rm_file_cfg = load_yaml(rm_path)
    fines_analysis_file_cfg = load_yaml(fines_analysis_path)
    dpr_file_cfg = load_yaml(dpr_path)
    hm_file_cfg = load_yaml(hot_metal_path)
    rm_hm_file_cfg = load_yaml(rm_hm_path)

    # Merge base + secrets
    merged = _deep_merge(base, secrets)

    # -----------------------------
    # RM CONFIG
    # -----------------------------
    merged["rm"] = rm_file_cfg["rm"] if "rm" in rm_file_cfg else rm_file_cfg

    # -----------------------------
    # FINES ANALYSIS CONFIG
    # -----------------------------
    merged["fines_analysis"] = (
        fines_analysis_file_cfg["fines_analysis"]
        if "fines_analysis" in fines_analysis_file_cfg
        else fines_analysis_file_cfg
    )

    # -----------------------------
    # DPR CONFIG
    # -----------------------------
    merged["dpr"] = dpr_file_cfg["dpr"] if "dpr" in dpr_file_cfg else dpr_file_cfg

    # -----------------------------
    # HOT METAL CONFIG
    # -----------------------------
    merged["hot_metal"] = (
        hm_file_cfg["hot_metal"] if "hot_metal" in hm_file_cfg else hm_file_cfg
    )

    # -----------------------------
    # RM_HM CONFIG
    # -----------------------------
    merged["rm_hm"] = rm_hm_file_cfg.get("rm_hm", {})
    merged["rm_hm_fields"] = rm_hm_file_cfg.get("rm_hm_fields", {})

    return _apply_database_url_overrides(_expand_env(merged))
