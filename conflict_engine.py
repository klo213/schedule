from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class ConflictResult:
    row_number_a: int
    row_number_b: int
    resource_id: str
    resource_name: str
    overlap_start: str
    overlap_end: str
    description: str


def detect_conflicts(rows: List) -> List[ConflictResult]:
    """
    O(n^2) pairwise overlap check on ValidatedRow objects.

    Two rows conflict when:
      - same resource_id  (same physical field)
      - time intervals overlap: A.start < B.end AND A.end > B.start
        (half-open interval logic; back-to-back bookings do NOT conflict)

    Returns a list of ConflictResult dataclasses; empty list = no conflicts.
    """
    conflicts: List[ConflictResult] = []

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a = rows[i]
            b = rows[j]

            if a.resource_id != b.resource_id:
                continue

            overlap_start = max(a.start_datetime, b.start_datetime)
            overlap_end = min(a.end_datetime, b.end_datetime)

            if overlap_start < overlap_end:
                conflicts.append(
                    ConflictResult(
                        row_number_a=a.row_number,
                        row_number_b=b.row_number,
                        resource_id=a.resource_id,
                        resource_name=a.resource_name,
                        overlap_start=overlap_start.isoformat(),
                        overlap_end=overlap_end.isoformat(),
                        description=(
                            f"Row {a.row_number} and Row {b.row_number} both book "
                            f"resource '{a.resource_name}' (id={a.resource_id}) "
                            f"with overlapping times"
                        ),
                    )
                )
                logger.warning(
                    "Conflict detected: row %d vs row %d, resource_id=%s, overlap %s to %s",
                    a.row_number,
                    b.row_number,
                    a.resource_id,
                    overlap_start.isoformat(),
                    overlap_end.isoformat(),
                )

    logger.info(
        "Conflict check complete: %d row(s) checked, %d conflict(s) found",
        len(rows),
        len(conflicts),
    )
    return conflicts
