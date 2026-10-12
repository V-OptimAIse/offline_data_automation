from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from domains.download.service import (
    ash_expected_name,
    charge_expected_name,
    latest_matching_file,
)
from infrastructure.synology_api_client import RemoteFile


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    profile: str
    folder: str
    identifier: str
    modes: tuple[str, ...]
    expected_name: str | None = None


@dataclass(frozen=True)
class ResolvedSource:
    spec: SourceSpec
    remote: RemoteFile

    @property
    def fingerprint(self) -> tuple[str, int | None, int | None]:
        return self.remote.path, self.remote.mtime, self.remote.size

    def state_entry(self, *, status: str = "completed") -> dict[str, object]:
        return {
            "remote_path": self.remote.path,
            "mtime": self.remote.mtime,
            "size": self.remote.size,
            "status": status,
        }


def _profile_folder(cfg: dict, profile: str) -> str:
    value = cfg["eml"]["profiles"][profile]["file_station_search_path"]
    return str(value).rstrip("/") or "/"


def build_source_registry(cfg: dict, run_date: date) -> tuple[SourceSpec, ...]:
    portal_files = cfg.get("portal_files") or {}
    profile_1_folder = _profile_folder(cfg, "profile_1")
    profile_2_folder = _profile_folder(cfg, "profile_2")

    def identifier(key: str, fallback: str) -> str:
        return str(portal_files.get(key) or fallback)

    ash_identifier = identifier("ash", "ASH ANALYSIS")
    formatted_run_date = run_date.strftime("%d-%b-%Y")
    charge_name = charge_expected_name(formatted_run_date)
    return (
        SourceSpec(
            "bf02_bunker",
            "profile_2",
            profile_2_folder,
            identifier("rm", "BF-02 BUNKER"),
            ("rm", "fines_analysis", "dust"),
        ),
        SourceSpec(
            "bf01_bunker",
            "profile_2",
            profile_2_folder,
            identifier("rm_sinter", "BF-01 BUNKER"),
            ("rm",),
        ),
        SourceSpec(
            "dpr",
            "profile_1",
            profile_1_folder,
            identifier("dpr", "BF-02 DPR"),
            ("dpr",),
        ),
        SourceSpec(
            "hot_metal",
            "profile_2",
            profile_2_folder,
            identifier("hot_metal", "BF-02- HOT METAL, SLAG"),
            ("hot_metal",),
        ),
        SourceSpec(
            "rm_strength_coke",
            "profile_1",
            profile_1_folder,
            identifier("rm_strength_coke", "01 COKE OVEN"),
            ("rm_strength",),
        ),
        SourceSpec(
            "rm_strength_sinter",
            "profile_1",
            profile_1_folder,
            identifier("rm_strength_sinter", "02 SP-02 PRODUCT"),
            ("rm_strength",),
        ),
        SourceSpec(
            "rm_stock",
            "profile_1",
            profile_1_folder,
            identifier("rm_stock", "RM BULK STOCK"),
            ("rm_stock",),
        ),
        SourceSpec(
            "rm_stock_sinter",
            "profile_1",
            profile_1_folder,
            identifier("rm_stock_sinter", "DPR SP#2"),
            ("rm_stock",),
        ),
        SourceSpec(
            "dust_chemical",
            "profile_2",
            profile_2_folder,
            identifier("dust_chemical", "GCP DUST CATCHER"),
            ("dust",),
        ),
        SourceSpec(
            "ash",
            "profile_1",
            profile_1_folder,
            ash_identifier,
            ("ash",),
            expected_name=ash_expected_name(ash_identifier, formatted_run_date),
        ),
        SourceSpec(
            f"charge:{run_date.isoformat()}",
            "profile_1",
            str(cfg["eml"]["charge_folder_path"]),
            charge_name,
            ("charge",),
            expected_name=charge_name,
        ),
    )


def listing_keys(specs: Iterable[SourceSpec]) -> tuple[tuple[str, str], ...]:
    return tuple(dict.fromkeys((spec.profile, spec.folder) for spec in specs))


def resolve_sources(
    specs: Iterable[SourceSpec],
    listings: dict[tuple[str, str], list[RemoteFile]],
) -> dict[str, ResolvedSource]:
    resolved: dict[str, ResolvedSource] = {}
    for spec in specs:
        remote = latest_matching_file(
            listings.get((spec.profile, spec.folder), []),
            spec.identifier,
            expected_name=spec.expected_name,
        )
        if remote is not None:
            resolved[spec.source_id] = ResolvedSource(spec=spec, remote=remote)
    return resolved


def dependencies_for_modes(
    specs: Iterable[SourceSpec], modes: Iterable[str]
) -> dict[str, tuple[SourceSpec, ...]]:
    selected_modes = set(modes)
    result: dict[str, list[SourceSpec]] = {mode: [] for mode in selected_modes}
    for spec in specs:
        for mode in spec.modes:
            if mode in selected_modes:
                result[mode].append(spec)
    return {mode: tuple(values) for mode, values in result.items()}
