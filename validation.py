from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"event_type", "team", "resource", "start_datetime", "end_datetime"}


@dataclass
class ValidatedRow:
    """A fully-validated row from the schedule CSV."""

    row_number: int
    event_type: str
    team: str
    resource: str
    resource_id: str
    resource_name: str
    start_datetime: datetime
    end_datetime: datetime
    raw: Dict[str, Any]


def validate_row(
    row: Dict[str, Any],
    row_number: int,
    resource_map: Dict[str, str],
    seen_fingerprints: Set[str],
) -> Tuple[Optional["ValidatedRow"], Optional[str]]:
    """Validate a single CSV row dict.

    Returns (ValidatedRow, None) on success, or (None, error_message) on failure.
    """
    for col in REQUIRED_COLUMNS:
        if col not in row or not str(row[col]).strip():
            return None, f"Row {row_number}: missing or empty column '{col}'"

    event_type = str(row["event_type"]).strip()
    team = str(row["team"]).strip()
    resource = str(row["resource"]).strip()

    start_raw = str(row["start_datetime"]).strip()
    end_raw = str(row["end_datetime"]).strip()

    try:
        start_dt = _parse_datetime(start_raw)
    except ValueError as exc:
        return None, f"Row {row_number}: invalid start_datetime '{start_raw}': {exc}"

    try:
        end_dt = _parse_datetime(end_raw)
    except ValueError as exc:
        return None, f"Row {row_number}: invalid end_datetime '{end_raw}': {exc}"

    if end_dt <= start_dt:
        return None, f"Row {row_number}: end_datetime must be after start_datetime"

    resource_id = resource_map.get(resource, resource)
    resource_name = resource

    fingerprint = f"{resource_id}|{start_raw}|{end_raw}|{team}"
    if fingerprint in seen_fingerprints:
        return None, (
            f"Row {row_number}: duplicate entry "
            f"(resource={resource}, start={start_raw}, team={team})"
        )
    seen_fingerprints.add(fingerprint)

    return (
        ValidatedRow(
            row_number=row_number,
            event_type=event_type,
            team=team,
            resource=resource,
            resource_id=resource_id,
            resource_name=resource_name,
            start_datetime=start_dt,
            end_datetime=end_dt,
            raw=dict(row),
        ),
        None,
    )


def _parse_datetime(value: str) -> datetime:
    """Parse a datetime string in common formats."""
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse datetime: {value!r}. "
        "Expected formats like 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD HH:MM'."
    )
