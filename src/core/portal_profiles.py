from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class PortalProfileConfigError(ValueError):
    """Raised when portal profile routing or credentials are incomplete."""


@dataclass(frozen=True)
class PortalProfileJob:
    name: str
    user: str
    password: str
    file_station_search_path: str
    modes: tuple[str, ...]


def _credential_env_name(profile_name: str, credential: str) -> str:
    normalized_name = re.sub(r"[^A-Z0-9]+", "_", profile_name.upper()).strip("_")
    return f"EML_{normalized_name}_{credential.upper()}"


def resolve_portal_profile_jobs(
    modes: list[str],
    eml_config: dict[str, Any],
) -> list[PortalProfileJob]:
    """Group requested modes into one authenticated download job per profile."""
    profiles = eml_config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise PortalProfileConfigError(
            "No portal profiles are configured under eml.profiles in base.yaml"
        )

    mode_owners: dict[str, str] = {}
    for profile_name, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            raise PortalProfileConfigError(
                f"Portal profile {profile_name!r} must be a mapping"
            )

        configured_modes = raw_profile.get("modes") or []
        if not isinstance(configured_modes, list):
            raise PortalProfileConfigError(
                f"Portal profile {profile_name!r} modes must be a list"
            )

        for raw_mode in configured_modes:
            mode = str(raw_mode).strip().lower()
            if not mode:
                continue
            existing_owner = mode_owners.get(mode)
            if existing_owner is not None:
                raise PortalProfileConfigError(
                    f"Portal mode {mode!r} is assigned to both "
                    f"{existing_owner!r} and {profile_name!r}"
                )
            mode_owners[mode] = str(profile_name)

    grouped_modes: dict[str, list[str]] = {}
    for mode in modes:
        profile_name = mode_owners.get(mode)
        if profile_name is None:
            raise PortalProfileConfigError(
                f"Portal mode {mode!r} is not assigned to a profile in base.yaml"
            )
        grouped_modes.setdefault(profile_name, []).append(mode)

    jobs = []
    for profile_name, profile_modes in grouped_modes.items():
        raw_profile = profiles[profile_name]
        user = str(raw_profile.get("user") or "").strip()
        password = str(raw_profile.get("password") or "").strip()
        file_station_search_path = str(
            raw_profile.get("file_station_search_path")
            or eml_config.get("file_station_search_path")
            or ""
        ).strip()

        missing_env_vars = []
        if not user:
            missing_env_vars.append(_credential_env_name(profile_name, "user"))
        if not password:
            missing_env_vars.append(_credential_env_name(profile_name, "password"))
        if missing_env_vars:
            raise PortalProfileConfigError(
                f"Missing credentials for portal profile {profile_name!r}. "
                f"Set {', '.join(missing_env_vars)} in .env"
            )
        if not file_station_search_path:
            raise PortalProfileConfigError(
                f"Missing file_station_search_path for portal profile "
                f"{profile_name!r} in base.yaml"
            )

        jobs.append(
            PortalProfileJob(
                name=profile_name,
                user=user,
                password=password,
                file_station_search_path=file_station_search_path,
                modes=tuple(profile_modes),
            )
        )

    return jobs
