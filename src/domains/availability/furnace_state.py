from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Literal, Sequence
from uuid import uuid4

from domains.availability.checker import AvailabilityResult


FurnaceStatus = Literal["NORMAL", "SHUTDOWN"]
ChargeObservation = Literal["AVAILABLE", "MISSING", "UNKNOWN"]


@dataclass(frozen=True)
class FurnaceState:
    status: FurnaceStatus = "NORMAL"
    consecutive_missing: int = 0
    consecutive_available: int = 0
    last_observation: ChargeObservation = "UNKNOWN"
    updated_at: str | None = None
    shutdown_notified: bool = False


def classify_charge_results(
    results: Sequence[AvailabilityResult],
) -> ChargeObservation:
    """Classify plant activity without mistaking DB failures for shutdown."""
    charge_results = [result for result in results if result.mode == "charge"]
    if any(result.status == "OK" for result in charge_results):
        return "AVAILABLE"
    if charge_results and all(result.status == "MISSING" for result in charge_results):
        return "MISSING"
    return "UNKNOWN"


def advance_furnace_state(
    state: FurnaceState,
    observation: ChargeObservation,
    *,
    missing_threshold: int,
    recovery_threshold: int,
    now: datetime,
) -> FurnaceState:
    """Apply one scheduled charge observation to the furnace state machine."""
    if missing_threshold < 1 or recovery_threshold < 1:
        raise ValueError("Furnace state thresholds must be positive integers")

    timestamp = now.isoformat()
    if state.status == "NORMAL":
        if observation == "MISSING":
            missing = state.consecutive_missing + 1
            if missing >= missing_threshold:
                return FurnaceState(
                    status="SHUTDOWN",
                    consecutive_missing=missing_threshold,
                    consecutive_available=0,
                    last_observation=observation,
                    updated_at=timestamp,
                    shutdown_notified=False,
                )
            return replace(
                state,
                consecutive_missing=missing,
                consecutive_available=0,
                last_observation=observation,
                updated_at=timestamp,
            )

        return replace(
            state,
            consecutive_missing=0,
            consecutive_available=0,
            last_observation=observation,
            updated_at=timestamp,
        )

    if observation == "AVAILABLE":
        available = state.consecutive_available + 1
        if available >= recovery_threshold:
            return FurnaceState(
                status="NORMAL",
                consecutive_missing=0,
                consecutive_available=0,
                last_observation=observation,
                updated_at=timestamp,
                shutdown_notified=False,
            )
        return replace(
            state,
            consecutive_available=available,
            last_observation=observation,
            updated_at=timestamp,
        )

    return replace(
        state,
        consecutive_available=0,
        last_observation=observation,
        updated_at=timestamp,
    )


def mark_shutdown_notified(state: FurnaceState) -> FurnaceState:
    if state.status != "SHUTDOWN":
        raise ValueError("Only a shutdown state can be marked as notified")
    return replace(state, shutdown_notified=True)


def load_furnace_state(path: str | Path) -> FurnaceState:
    state_path = Path(path)
    if not state_path.exists():
        return FurnaceState()
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        state = FurnaceState(**raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read availability state: {state_path}") from exc
    if state.status not in {"NORMAL", "SHUTDOWN"}:
        raise RuntimeError(f"Unsupported furnace status in {state_path}: {state.status}")
    return state


def save_furnace_state(path: str | Path, state: FurnaceState) -> None:
    """Persist state atomically so an interrupted oneshot cannot corrupt it."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_name(
        f"{state_path.name}.{uuid4().hex}.tmp"
    )
    try:
        with temporary_path.open("x", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, state_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
