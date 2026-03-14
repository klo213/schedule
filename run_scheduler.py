import argparse
import csv
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except ImportError as exc:
    raise RuntimeError("PyYAML is required. Install with: pip install pyyaml") from exc

try:
    from google.cloud import storage as gcs_storage
except ImportError:
    gcs_storage = None

from bookafield_client import APIError, AuthenticationError, BookaFieldClient, ConfigurationError
from conflict_engine import detect_conflicts
from validation import ValidatedRow, validate_row


def _load_config(config_path: Path) -> Dict[str, object]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping/object")
    return loaded


def _resolve_config_value(value: object) -> str:
    raw = str(value or "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        env_name = raw[2:-1].strip()
        return os.environ.get(env_name, "").strip()
    return raw


def _download_gcs_csv(gcs_uri: str, local_path: Path) -> None:
    """Download a schedule CSV from a GCS URI (gs://bucket/path) to a local path."""
    if gcs_storage is None:
        raise RuntimeError(
            "google-cloud-storage is required for GCS support. "
            "Install with: pip install google-cloud-storage"
        )
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): {gcs_uri}")

    without_prefix = gcs_uri[5:]  # strip "gs://"
    bucket_name, _, blob_name = without_prefix.partition("/")
    if not bucket_name or not blob_name:
        raise ValueError(
            f"Invalid GCS URI — expected gs://bucket-name/path/to/file.csv, got: {gcs_uri}"
        )

    client = gcs_storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.download_to_filename(str(local_path))


def _load_resource_map(resource_map_path: Path) -> Dict[str, str]:
    if not resource_map_path.exists():
        raise FileNotFoundError(f"Resource map file not found: {resource_map_path}")

    resource_map: Dict[str, str] = {}
    with resource_map_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = {"resource_name", "resource_id"}
        if not reader.fieldnames or not required_columns.issubset(set(reader.fieldnames)):
            raise ValueError("Resource map CSV must include columns: resource_name, resource_id")
        for row in reader:
            name = (row.get("resource_name") or "").strip()
            resource_id = (row.get("resource_id") or "").strip()
            if not name or not resource_id:
                continue
            resource_map[name] = resource_id

    if not resource_map:
        raise ValueError("Resource map is empty after parsing")
    return resource_map


def _build_attempt_record(row_number: int, row: Dict[str, str], dry_run: bool) -> Dict[str, object]:
    return {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "row_number": row_number,
        "event_type": row.get("event_type"),
        "team": row.get("team"),
        "resource_name": row.get("resource"),
        "resource_id": None,
        "start_datetime": row.get("start_datetime"),
        "end_datetime": row.get("end_datetime"),
        "dry_run": dry_run,
        "validation_result": "pending",
        "conflict_result": "pending",
        "api_result": "not_attempted",
        "status": "pending",
        "reservation_id": None,
        "failure_reason": None,
    }


def _serialize_validated_row(row: ValidatedRow) -> Dict[str, object]:
    serialized = asdict(row)
    serialized["start_datetime"] = row.start_datetime.isoformat()
    serialized["end_datetime"] = row.end_datetime.isoformat()
    return serialized


def _write_audit_outputs(log_dir: Path, run_id: str, attempts: List[Dict[str, object]], summary: Dict[str, object]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = log_dir / f"run_{run_id}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in attempts:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    summary_path = log_dir / f"run_{run_id}_summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)


def _weekday_repeat_key(dt: datetime) -> str:
    return f"repeat{dt.strftime('%A')}"


def _bookafield_web_payload(row: ValidatedRow, config: Dict[str, object]) -> Dict[str, object]:
    reservation_cfg = config.get("reservation") or {}
    if not isinstance(reservation_cfg, dict):
        raise ConfigurationError("reservation must be an object in config")

    schedule_id = _resolve_config_value(reservation_cfg.get("schedule_id"))
    user_id = _resolve_config_value(reservation_cfg.get("user_id"))

    if not schedule_id:
        raise ConfigurationError("Missing config value: reservation.schedule_id for BookaField web mode")
    if not user_id:
        raise ConfigurationError("Missing config value: reservation.user_id for BookaField web mode")

    csrf_cfg = config.get("csrf") or {}
    csrf_token = ""
    if isinstance(csrf_cfg, dict):
        csrf_token = _resolve_config_value(csrf_cfg.get("static_token"))
    if not csrf_token:
        csrf_token = _resolve_config_value(reservation_cfg.get("csrf_token"))

    title_template = str(reservation_cfg.get("title_template") or "{event_type} {team}").strip()
    reservation_title = title_template.format(event_type=row.event_type, team=row.team).strip()

    description_template = str(reservation_cfg.get("description_template") or "{team}").strip()
    reservation_description = description_template.format(event_type=row.event_type, team=row.team).strip()

    event_type_field = str(reservation_cfg.get("event_type_field") or "psiattribute[1]").strip()
    division_field = str(reservation_cfg.get("division_field") or "psiattribute[3]").strip()

    payload: Dict[str, object] = {
        "userId": user_id,
        "beginDate": row.start_datetime.strftime("%Y-%m-%d"),
        "beginPeriod": row.start_datetime.strftime("%H:%M:%S"),
        "endDate": row.end_datetime.strftime("%Y-%m-%d"),
        "endPeriod": row.end_datetime.strftime("%H:%M:%S"),
        "repeatOptions": "none",
        "repeatEvery": "1",
        "repeatMonthlyType": "dayOfMonth",
        "endRepeatDate": row.start_datetime.strftime("%Y-%m-%d"),
        "scheduleId": schedule_id,
        "resourceId": row.resource_id,
        "reservationTitle": reservation_title,
        "reservationDescription": reservation_description,
        "reservationId": "",
        "referenceNumber": "",
        "reservationAction": "create",
        "DELETE_REASON": "",
        "seriesUpdateScope": "full",
        event_type_field: row.event_type,
        division_field: row.team,
        _weekday_repeat_key(row.start_datetime): "on",
    }

    static_form_fields = reservation_cfg.get("static_form_fields") or {}
    if not isinstance(static_form_fields, dict):
        raise ConfigurationError("reservation.static_form_fields must be an object in config")
    for key, value in static_form_fields.items():
        payload[str(key)] = _resolve_config_value(value)

    if csrf_token:
        csrf_field_name = str((config.get("csrf") or {}).get("token_field") or "CSRF_TOKEN")
        payload[csrf_field_name] = csrf_token

    return payload


def _extract_bookafield_error(response_body: Dict[str, object]) -> str:
    raw_text = str(response_body.get("raw_text") or "")
    if not raw_text:
        return ""

    message_match = re.search(r'<div id="failed-message"[^>]*>(.*?)</div>', raw_text, flags=re.IGNORECASE | re.DOTALL)
    detail_matches = re.findall(r'<div class="error">(.*?)</div>', raw_text, flags=re.IGNORECASE | re.DOTALL)

    chunks = []
    if message_match:
        chunks.append(message_match.group(1))
    for detail in detail_matches:
        chunks.append(detail)

    if not chunks:
        return ""

    cleaned = []
    for chunk in chunks:
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            cleaned.append(text)

    return ' | '.join(cleaned)


def _reservation_payload(row: ValidatedRow, config: Dict[str, object]) -> Dict[str, object]:
    reservation_cfg = config.get("reservation") or {}
    endpoint = str(reservation_cfg.get("endpoint") or "")

    if endpoint.endswith("reservation_save.php"):
        return _bookafield_web_payload(row, config)

    return {
        "event_type": row.event_type,
        "team": row.team,
        "resource_id": row.resource_id,
        "resource_name": row.resource_name,
        "start_datetime": row.start_datetime.isoformat(),
        "end_datetime": row.end_datetime.isoformat(),
    }


def run_scheduler(schedule_csv_path: Path, config_path: Path, dry_run_override: bool = False, force_live: bool = False) -> Dict[str, object]:
    config = _load_config(config_path)

    config_dry_run = bool(config.get("dry_run", True))
    dry_run = True if dry_run_override else config_dry_run
    if force_live:
        dry_run = False

    resource_map_path = config.get("resource_map_path") or "docs/resource_mapping_example.csv"
    resource_map = _load_resource_map(Path(resource_map_path))

    if not schedule_csv_path.exists():
        raise FileNotFoundError(f"Schedule CSV file not found: {schedule_csv_path}")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    attempts: List[Dict[str, object]] = []
    valid_rows: List[ValidatedRow] = []
    attempt_by_row_number: Dict[int, Dict[str, object]] = {}
    seen_fingerprints = set()

    with schedule_csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = {"event_type", "team", "resource", "start_datetime", "end_datetime"}
        if not reader.fieldnames or not required_columns.issubset(set(reader.fieldnames)):
            raise ValueError(
                "Schedule CSV must include columns: event_type, team, resource, start_datetime, end_datetime"
            )

        for row_number, row in enumerate(reader, start=2):
            attempt = _build_attempt_record(row_number=row_number, row=row, dry_run=dry_run)
            attempts.append(attempt)
            attempt_by_row_number[row_number] = attempt

            validated_row, error = validate_row(row, row_number, resource_map, seen_fingerprints)
            if error:
                attempt["validation_result"] = "failed"
                attempt["conflict_result"] = "not_checked"
                attempt["status"] = "validation_failure"
                attempt["failure_reason"] = error
                continue

            attempt["resource_id"] = validated_row.resource_id
            attempt["validation_result"] = "passed"
            valid_rows.append(validated_row)

    conflicts = detect_conflicts(valid_rows)
    conflicted_row_numbers = set()
    for conflict in conflicts:
        conflicted_row_numbers.add(conflict.row_number_a)
        conflicted_row_numbers.add(conflict.row_number_b)

    for row_number in conflicted_row_numbers:
        attempt = attempt_by_row_number[row_number]
        attempt["conflict_result"] = "failed"
        attempt["status"] = "conflict_failure"
        attempt["failure_reason"] = "Overlapping reservations for the same resource"

    api_client = None
    auth_failure_reason = None
    if not dry_run:
        try:
            api_client = BookaFieldClient(config=config)
            api_client.authenticate()
        except (AuthenticationError, ConfigurationError, APIError) as exc:
            auth_failure_reason = str(exc)

    for row in valid_rows:
        attempt = attempt_by_row_number[row.row_number]
        if attempt["status"] == "conflict_failure":
            continue

        attempt["conflict_result"] = "passed"

        if dry_run:
            attempt["status"] = "skipped"
            attempt["api_result"] = "dry_run"
            continue

        if auth_failure_reason:
            attempt["status"] = "api_failure"
            attempt["api_result"] = "auth_failed"
            attempt["failure_reason"] = auth_failure_reason
            continue

        payload = _reservation_payload(row, config)
        try:
            reservation_id, response_body = api_client.create_reservation(payload)
            if reservation_id:
                attempt["status"] = "created"
                attempt["api_result"] = "success"
                attempt["reservation_id"] = reservation_id
            else:
                attempt["status"] = "api_failure"
                attempt["api_result"] = "failed"
                parsed_error = _extract_bookafield_error(response_body)
                if parsed_error:
                    attempt["failure_reason"] = parsed_error
                else:
                    attempt["failure_reason"] = (
                        "Reservation created response did not include reservation id. "
                        f"Body keys: {list(response_body.keys())}"
                    )
        except (ConfigurationError, APIError) as exc:
            attempt["status"] = "api_failure"
            attempt["api_result"] = "failed"
            attempt["failure_reason"] = str(exc)

    summary = {
        "run_id": run_id,
        "schedule_csv_path": str(schedule_csv_path),
        "config_path": str(config_path),
        "dry_run": dry_run,
        "counts": {
            "created": sum(1 for a in attempts if a["status"] == "created"),
            "skipped": sum(1 for a in attempts if a["status"] == "skipped"),
            "validation_failures": sum(1 for a in attempts if a["status"] == "validation_failure"),
            "conflict_failures": sum(1 for a in attempts if a["status"] == "conflict_failure"),
            "api_failures": sum(1 for a in attempts if a["status"] == "api_failure"),
            "total_rows": len(attempts),
        },
        "conflicts": [asdict(c) for c in conflicts],
        "valid_rows": [_serialize_validated_row(r) for r in valid_rows],
    }

    log_dir = Path(str(config.get("log_dir") or "logs"))
    _write_audit_outputs(log_dir=log_dir, run_id=run_id, attempts=attempts, summary=summary)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BookaField scheduler automation",
        epilog="Schedule CSV source: provide schedule_csv argument or set SCHEDULE_CSV_GCS_URI env var.",
    )
    parser.add_argument(
        "schedule_csv",
        type=Path,
        nargs="?",
        help="Path to schedule CSV input (optional if SCHEDULE_CSV_GCS_URI is set)",
    )
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"), help="Path to YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    parser.add_argument("--live", action="store_true", help="Force live submission mode")

    args = parser.parse_args()

    gcs_uri = os.environ.get("SCHEDULE_CSV_GCS_URI", "").strip()
    tmp_path: Optional[Path] = None

    if gcs_uri:
        # Download CSV from GCS to a temp file, clean up after run
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".csv")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            _download_gcs_csv(gcs_uri, tmp_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        schedule_csv_path = tmp_path
    elif args.schedule_csv:
        schedule_csv_path = args.schedule_csv
    else:
        parser.error(
            "No schedule CSV provided. Either pass schedule_csv as a positional argument "
            "or set the SCHEDULE_CSV_GCS_URI environment variable (e.g. gs://my-bucket/schedule.csv)."
        )
        return  # unreachable, parser.error exits — keeps type checker happy

    try:
        summary = run_scheduler(
            schedule_csv_path=schedule_csv_path,
            config_path=args.config,
            dry_run_override=args.dry_run,
            force_live=args.live,
        )
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    print("Run complete")
    print(json.dumps(summary["counts"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
