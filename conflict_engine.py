import logging
from typing import List

logger = logging.getLogger(__name__)


def detect_conflicts(rows: List) -> List:
    """
    Stub: accepts list of ValidatedRow objects, returns no conflicts.
    Real overlap-detection logic deferred until after canary success.
    """
    logger.info("Conflict check: %d rows, returning 0 conflicts (stub)", len(rows))
    return []
