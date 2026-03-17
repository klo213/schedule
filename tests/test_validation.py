from __future__ import annotations

from validation import validate_row


def test_validate_row_uses_mapped_resource_id() -> None:
    row = {
        "event_type": "Practice",
        "team": "Team A",
        "resource": "Field A",
        "start_datetime": "2099-01-01T09:00:00",
        "end_datetime": "2099-01-01T10:00:00",
    }

    validated_row, error = validate_row(
        row=row,
        row_number=2,
        resource_map={"Field A": "101"},
        seen_fingerprints=set(),
    )

    assert error is None
    assert validated_row is not None
    assert validated_row.resource_id == "101"
    assert validated_row.resource_name == "Field A"


def test_validate_row_rejects_unmapped_resource() -> None:
    row = {
        "event_type": "Practice",
        "team": "Team A",
        "resource": "DRY_RUN_FIELD_1",
        "start_datetime": "2099-01-01T09:00:00",
        "end_datetime": "2099-01-01T10:00:00",
    }

    validated_row, error = validate_row(
        row=row,
        row_number=2,
        resource_map={"Field A": "101"},
        seen_fingerprints=set(),
    )

    assert validated_row is None
    assert error == "Row 2: resource 'DRY_RUN_FIELD_1' was not found in the resource map"
