from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

try:
    import yaml
except ImportError as exc:
    raise RuntimeError("PyYAML is required. Install with: pip install pyyaml") from exc

try:
    from google.cloud import storage as gcs_storage
except ImportError:
    gcs_storage = None


CANONICAL_FIELDS = {
    "name",
    "email",
    "team",
    "event_type",
    "date",
    "start",
    "end",
    "field",
    "umpire",
    "reason",
    "opponent",
    "urgent",
}

FIELD_ALIASES = {
    "name": "name",
    "coach": "name",
    "coach_name": "name",
    "email": "email",
    "coach_email": "email",
    "team": "team",
    "event": "event_type",
    "event_type": "event_type",
    "type": "event_type",
    "date": "date",
    "requested_date": "date",
    "start": "start",
    "start_time": "start",
    "requested_start_time": "start",
    "end": "end",
    "end_time": "end",
    "requested_end_time": "end",
    "field": "field",
    "preferred_field": "field",
    "resource": "field",
    "umpire": "umpire",
    "umpire_required": "umpire",
    "reason": "reason",
    "notes": "reason",
    "opponent": "opponent",
    "opponent_team": "opponent",
    "urgent": "urgent",
}

OUTPUT_COLUMNS = [
    "request_id",
    "source",
    "received_at",
    "coach_name",
    "coach_phone",
    "coach_email",
    "team",
    "event_type",
    "opponent_team",
    "preferred_resource",
    "preferred_start_datetime",
    "preferred_end_datetime",
    "needs_umpire",
    "urgent",
    "reason",
    "status",
    "raw_message",
]

REQUIRED_FIELDS = {"name", "email", "team", "event_type", "date", "start", "end", "field", "umpire", "reason"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REQUEST_ID_PATTERN = re.compile(r"\b(REQ-[A-Za-z0-9_-]+)\b", flags=re.IGNORECASE)


def _load_config(config_path: Path) -> Dict[str, object]:
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping/object")
    return loaded


def _twiml_message(message: str) -> str:
    safe = html.escape(message)
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Message>{safe}</Message></Response>"


def _resolve_config_value(value: object) -> str:
    raw = str(value or "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        env_name = raw[2:-1].strip()
        return os.environ.get(env_name, "").strip()
    return raw


def _normalize_allowed_numbers(raw_numbers: Iterable[object]) -> set[str]:
    allowed = set()
    for value in raw_numbers:
        raw = str(value or "").strip()
        if raw:
            allowed.add(raw)
    return allowed


def _write_inbound_diagnostic(config_path: Path, payload: Dict[str, object]) -> None:
    try:
        config = _load_config(config_path)
        logs_dir = Path(_resolve_config_value(config.get("log_dir") or "logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        path = logs_dir / f"twilio_inbound_{now.strftime('%Y%m%d')}.jsonl"
        row = dict(payload)
        row["timestamp"] = now.isoformat().replace("+00:00", "Z")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        return


def _is_gcs_uri(value: str) -> bool:
    return str(value or "").startswith("gs://")


def _gcs_blob(uri: str):
    if gcs_storage is None:
        raise RuntimeError(
            "google-cloud-storage is required for GCS persistence. "
            "Install with: pip install google-cloud-storage"
        )
    without_prefix = uri[5:]
    bucket_name, _, blob_name = without_prefix.partition("/")
    if not bucket_name or not blob_name:
        raise ValueError(f"Invalid GCS URI: {uri}")
    client = gcs_storage.Client()
    bucket = client.bucket(bucket_name)
    return bucket.blob(blob_name)


def _read_text(location: str) -> Optional[str]:
    if _is_gcs_uri(location):
        blob = _gcs_blob(location)
        if not blob.exists():
            return None
        return blob.download_as_text(encoding="utf-8")

    path = Path(location)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _write_text(location: str, text: str) -> None:
    if _is_gcs_uri(location):
        blob = _gcs_blob(location)
        blob.upload_from_string(text, content_type="application/json; charset=utf-8")
        return

    path = Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _csv_exists(location: str) -> bool:
    if _is_gcs_uri(location):
        return bool(_gcs_blob(location).exists())
    return Path(location).exists()


def _append_csv_row_to_location(location: str, row: Dict[str, str]) -> None:
    existing = _read_text(location) or ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=OUTPUT_COLUMNS)
    if not existing.strip():
        writer.writeheader()
    else:
        buffer.write(existing)
        if existing and not existing.endswith("\n"):
            buffer.write("\n")
    writer.writerow({key: row.get(key, "") for key in OUTPUT_COLUMNS})

    if _is_gcs_uri(location):
        blob = _gcs_blob(location)
        blob.upload_from_string(buffer.getvalue(), content_type="text/csv; charset=utf-8")
        return

    path = Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def _coach_request_output_location(config: Dict[str, object]) -> str:
    coach_cfg = config.get("coach_request_sync") or {}
    if not isinstance(coach_cfg, dict):
        raise ValueError("coach_request_sync must be an object in config")
    return (
        os.environ.get("COACH_REQUEST_OUTPUT_CSV", "").strip()
        or _resolve_config_value(coach_cfg.get("output_csv") or "data/coach_requests_latest.csv")
    )


def _coach_request_state_location(config: Dict[str, object]) -> str:
    coach_cfg = config.get("coach_request_sync") or {}
    if not isinstance(coach_cfg, dict):
        raise ValueError("coach_request_sync must be an object in config")
    return (
        os.environ.get("COACH_REQUEST_STATE_FILE", "").strip()
        or _resolve_config_value(coach_cfg.get("state_file") or "data/coach_request_sync_state.json")
    )


def _pending_umpire_assignments_location(config: Dict[str, object]) -> str:
    umpire_cfg = config.get("umpire_assignment") or {}
    if not isinstance(umpire_cfg, dict):
        raise ValueError("umpire_assignment must be an object in config")
    return (
        os.environ.get("PENDING_UMPIRE_ASSIGNMENTS_FILE", "").strip()
        or _resolve_config_value(umpire_cfg.get("pending_file") or "data/pending_umpire_assignments.json")
    )


def _parse_key_value_body(body: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    text = (body or "").strip()
    if not text:
        return None, "Empty message."

    raw_segments = []
    for chunk in text.replace("\r", "\n").split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        raw_segments.extend(part.strip() for part in chunk.split(";") if part.strip())

    parsed: Dict[str, str] = {}
    unknown_keys: List[str] = []
    for segment in raw_segments:
        if ":" in segment:
            key, value = segment.split(":", 1)
        elif "=" in segment:
            key, value = segment.split("=", 1)
        else:
            return None, (
                "Invalid format. Use key:value pairs separated by semicolons. "
                "Example: name:Jane Coach; email:jane@example.com; team:12U Black; "
                "event:Game; date:2026-05-10; start:18:00; end:20:00; field:Fenway 1; "
                "umpire:yes; reason:Weather makeup"
            )

        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
        canonical = FIELD_ALIASES.get(normalized_key)
        if not canonical:
            unknown_keys.append(key.strip())
            continue

        parsed[canonical] = value.strip()

    if unknown_keys:
        return None, f"Unknown field(s): {', '.join(unknown_keys)}"

    missing = [field for field in sorted(REQUIRED_FIELDS) if not parsed.get(field)]
    if missing:
        return None, f"Missing required field(s): {', '.join(missing)}"

    return parsed, None


def _parse_date(value: str) -> datetime:
    formats = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError("Use date as YYYY-MM-DD or MM/DD/YYYY")


def _parse_time(value: str) -> datetime:
    formats = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError("Use time as HH:MM or h:MM AM/PM")


def _normalize_yes_no(value: str, *, default: str = "No") -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return default
    if lowered in {"yes", "y", "true", "1"}:
        return "Yes"
    if lowered in {"no", "n", "false", "0"}:
        return "No"
    raise ValueError(f"Use Yes/No for value '{value}'")


def parse_coach_request_sms(body: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    fields, error = _parse_key_value_body(body)
    if error:
        return None, error
    assert fields is not None

    email = fields["email"].strip()
    if not EMAIL_PATTERN.match(email):
        return None, "Invalid email format."

    try:
        requested_date = _parse_date(fields["date"])
        requested_start = _parse_time(fields["start"])
        requested_end = _parse_time(fields["end"])
        needs_umpire = _normalize_yes_no(fields["umpire"])
        urgent = _normalize_yes_no(fields.get("urgent") or "", default="No")
    except ValueError as exc:
        return None, str(exc)

    start_dt = requested_date.replace(
        hour=requested_start.hour,
        minute=requested_start.minute,
        second=0,
        microsecond=0,
    )
    end_dt = requested_date.replace(
        hour=requested_end.hour,
        minute=requested_end.minute,
        second=0,
        microsecond=0,
    )
    if end_dt <= start_dt:
        return None, "End time must be after start time."

    parsed = {
        "coach_name": fields["name"].strip(),
        "coach_email": email,
        "team": fields["team"].strip(),
        "event_type": fields["event_type"].strip(),
        "opponent_team": fields.get("opponent", "").strip(),
        "preferred_resource": fields["field"].strip(),
        "preferred_start_datetime": start_dt.strftime("%Y-%m-%d %H:%M"),
        "preferred_end_datetime": end_dt.strftime("%Y-%m-%d %H:%M"),
        "needs_umpire": needs_umpire,
        "urgent": urgent,
        "reason": fields["reason"].strip(),
    }
    return parsed, None


def _load_state(state_location: str) -> Dict[str, object]:
    existing = _read_text(state_location)
    if not existing:
        return {"next_sequence": 1}
    loaded = json.loads(existing)
    if not isinstance(loaded, dict):
        raise ValueError("Coach request state file must contain a JSON object")
    return loaded


def _store_state(state_location: str, state: Dict[str, object]) -> None:
    _write_text(state_location, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _next_request_id(config: Dict[str, object]) -> str:
    coach_cfg = config.get("coach_request_sync") or {}
    if not isinstance(coach_cfg, dict):
        raise ValueError("coach_request_sync must be an object in config")
    state_location = _coach_request_state_location(config)
    id_prefix = str(coach_cfg.get("id_prefix") or "REQ").strip() or "REQ"
    state = _load_state(state_location)
    next_sequence = int(state.get("next_sequence") or 1)
    request_id = f"{id_prefix}-{next_sequence:04d}"
    state["next_sequence"] = next_sequence + 1
    _store_state(state_location, state)
    return request_id


def process_incoming_twilio_coach_request(config_path: Path, from_number: str, body: str) -> str:
    config = _load_config(config_path)
    twilio_cfg = config.get("twilio") or {}
    if twilio_cfg and not isinstance(twilio_cfg, dict):
        raise ValueError("twilio must be an object in config")

    allowed_numbers = _normalize_allowed_numbers((twilio_cfg or {}).get("coach_request_allowed_numbers") or [])
    if allowed_numbers and from_number not in allowed_numbers:
        message = "This number is not authorized for coach request intake."
        _write_inbound_diagnostic(
            config_path,
            {"from": from_number, "body": body, "result": "unauthorized_number", "message": message},
        )
        return message

    parsed, error = parse_coach_request_sms(body)
    if error:
        _write_inbound_diagnostic(
            config_path,
            {"from": from_number, "body": body, "result": "parse_error", "message": error},
        )
        return (
            f"{error} Reply with: "
            "name:...; email:...; team:...; event:Game; date:YYYY-MM-DD; start:18:00; end:20:00; "
            "field:...; umpire:yes/no; reason:..."
        )
    assert parsed is not None

    coach_cfg = config.get("coach_request_sync") or {}
    if not isinstance(coach_cfg, dict):
        raise ValueError("coach_request_sync must be an object in config")

    output_location = _coach_request_output_location(config)
    request_id = _next_request_id(config)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    record = {
        "request_id": request_id,
        "source": f"twilio:{from_number}",
        "received_at": now,
        "coach_name": parsed["coach_name"],
        "coach_phone": from_number,
        "coach_email": parsed["coach_email"],
        "team": parsed["team"],
        "event_type": parsed["event_type"],
        "opponent_team": parsed["opponent_team"],
        "preferred_resource": parsed["preferred_resource"],
        "preferred_start_datetime": parsed["preferred_start_datetime"],
        "preferred_end_datetime": parsed["preferred_end_datetime"],
        "needs_umpire": parsed["needs_umpire"],
        "urgent": parsed["urgent"],
        "reason": parsed["reason"],
        "status": "received",
        "raw_message": body.strip(),
    }
    _append_csv_row_to_location(output_location, record)
    _write_inbound_diagnostic(
        config_path,
        {"from": from_number, "body": body, "result": "accepted", "request_id": request_id, "record": record},
    )

    return (
        f"Received {request_id} for {parsed['team']} on {parsed['preferred_start_datetime']} at "
        f"{parsed['preferred_resource']}. We will review it shortly."
    )


def parse_umpire_sms_command(body: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    text = " ".join((body or "").strip().split())
    if not text:
        return None, None, None, "Empty message. Include request ID and status."

    request_match = REQUEST_ID_PATTERN.search(text)
    if not request_match:
        return None, None, None, "Missing request ID. Example: REQ-123 Looking"
    request_id = request_match.group(1).upper()

    lowered = text.lower()
    decision: Optional[str] = None
    if "looking" in lowered or "search" in lowered or "pending" in lowered:
        decision = "looking_for_umpire_assignment"
    elif "assigned" in lowered:
        decision = "umpire_assigned"

    if not decision:
        return request_id, None, None, "Missing decision. Use 'Umpire Assigned' or 'Looking for umpire assignment'."

    assigned_name: Optional[str] = None
    if decision == "umpire_assigned":
        assigned_match = re.search(
            r"(?:umpire\s+assigned|assigned)\s*[:\-]?\s*(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if assigned_match:
            candidate = assigned_match.group(1).strip()
            if candidate and not REQUEST_ID_PATTERN.search(candidate):
                assigned_name = candidate

    return request_id, decision, assigned_name, None


def _load_pending_assignments(config: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    location = _pending_umpire_assignments_location(config)
    payload = _read_text(location)
    if not payload:
        return {}
    loaded = json.loads(payload)
    if not isinstance(loaded, list):
        return {}
    pending: Dict[str, Dict[str, object]] = {}
    for row in loaded:
        if not isinstance(row, dict):
            continue
        request_id = str(row.get("request_id") or "").strip()
        if request_id:
            pending[request_id] = row
    return pending


def _store_pending_assignments(config: Dict[str, object], pending: Dict[str, Dict[str, object]]) -> None:
    location = _pending_umpire_assignments_location(config)
    rows = sorted(pending.values(), key=lambda item: str(item.get("request_id") or ""))
    _write_text(location, json.dumps(rows, indent=2, sort_keys=True) + "\n")


def process_incoming_twilio_umpire_reply(config_path: Path, from_number: str, body: str) -> str:
    config = _load_config(config_path)
    twilio_cfg = config.get("twilio") or {}
    if twilio_cfg and not isinstance(twilio_cfg, dict):
        raise ValueError("twilio must be an object in config")

    allowed_numbers = _normalize_allowed_numbers((twilio_cfg or {}).get("umpire_coordinator_numbers") or [])
    if allowed_numbers and from_number not in allowed_numbers:
        message = "This number is not authorized for umpire coordinator updates."
        _write_inbound_diagnostic(
            config_path,
            {"from": from_number, "body": body, "result": "unauthorized_umpire_number", "message": message},
        )
        return message

    request_id, decision, assigned_name, parse_error = parse_umpire_sms_command(body)
    if parse_error:
        _write_inbound_diagnostic(
            config_path,
            {
                "from": from_number,
                "body": body,
                "request_id": request_id,
                "result": "umpire_parse_error",
                "message": parse_error,
            },
        )
        return parse_error

    pending = _load_pending_assignments(config)
    pending_row = pending.get(request_id or "")
    if not pending_row:
        message = f"Update not applied for {request_id}: Not found in pending assignments"
        _write_inbound_diagnostic(
            config_path,
            {"from": from_number, "body": body, "request_id": request_id, "result": "umpire_unmatched", "message": message},
        )
        return message

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    pending_row["last_coordinator_response"] = body.strip()
    pending_row["last_coordinator_response_at"] = now_iso
    pending_row["responded_by"] = from_number

    if decision == "umpire_assigned":
        if not assigned_name:
            return "assigned_umpire_name is required when response is assigned"
        pending_row["status"] = "complete"
        pending_row["assigned_umpire_name"] = assigned_name
        pending_row["assigned_at"] = now_iso
        pending_row["assigned_by"] = from_number
        pending_row["umpire_assignment_status"] = "umpire_assigned"
        message = f"Received. {request_id} marked complete with umpire assigned."
    else:
        pending_row["status"] = "pending_umpire_assignment"
        pending_row["umpire_assignment_status"] = "looking_for_umpire_assignment"
        message = f"Received. {request_id} remains pending while umpire assignment is in progress."

    pending[request_id or ""] = pending_row
    _store_pending_assignments(config, pending)
    _write_inbound_diagnostic(
        config_path,
        {
            "from": from_number,
            "body": body,
            "request_id": request_id,
            "decision": decision,
            "assigned_umpire_name": assigned_name,
            "result": "umpire_updated",
            "message": message,
        },
    )
    return message


def _request_url(handler: BaseHTTPRequestHandler) -> str:
    parsed = urlparse(handler.path)
    proto = handler.headers.get("X-Forwarded-Proto") or "http"
    host = handler.headers.get("Host") or "localhost"
    if parsed.query:
        return f"{proto}://{host}{parsed.path}?{parsed.query}"
    return f"{proto}://{host}{parsed.path}"


def _validate_twilio_signature(config_path: Path, handler: BaseHTTPRequestHandler, form: Dict[str, List[str]]) -> bool:
    config = _load_config(config_path)
    twilio_cfg = config.get("twilio") or {}
    if twilio_cfg and not isinstance(twilio_cfg, dict):
        raise ValueError("twilio must be an object in config")
    validate_signature = bool((twilio_cfg or {}).get("validate_signature", True))
    auth_token_env = str((twilio_cfg or {}).get("auth_token_env") or "TWILIO_AUTH_TOKEN").strip() or "TWILIO_AUTH_TOKEN"
    if not validate_signature:
        return True

    auth_token = os.environ.get(auth_token_env, "").strip()
    if not auth_token:
        raise ValueError(f"Missing Twilio auth token env var: {auth_token_env}")

    signature = handler.headers.get("X-Twilio-Signature", "").strip()
    if not signature:
        return False

    try:
        from twilio.request_validator import RequestValidator
    except ImportError as exc:
        raise RuntimeError("twilio is required. Install with: pip install twilio") from exc

    payload = {key: values[0] if values else "" for key, values in form.items()}
    validator = RequestValidator(auth_token)
    return bool(validator.validate(_request_url(handler), payload, signature))


class TwilioCoachRequestWebhookHandler(BaseHTTPRequestHandler):
    config_path: Path

    def _write_xml(self, status: HTTPStatus, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/twilio/coach-request", "/twilio/umpire-reply"}:
            self._write_xml(HTTPStatus.NOT_FOUND, _twiml_message("Unknown endpoint."))
            return

        content_length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(content_length).decode("utf-8", errors="replace")
        form = parse_qs(raw)

        try:
            if not _validate_twilio_signature(self.config_path, self, form):
                self._write_xml(HTTPStatus.FORBIDDEN, _twiml_message("Invalid Twilio signature."))
                return
        except Exception as exc:
            self._write_xml(HTTPStatus.INTERNAL_SERVER_ERROR, _twiml_message(f"Signature validation failed: {exc}"))
            return

        from_number = (form.get("From") or [""])[0].strip()
        message_body = (form.get("Body") or [""])[0].strip()

        try:
            if parsed.path == "/twilio/umpire-reply":
                message = process_incoming_twilio_umpire_reply(
                    config_path=self.config_path,
                    from_number=from_number,
                    body=message_body,
                )
            else:
                message = process_incoming_twilio_coach_request(
                    config_path=self.config_path,
                    from_number=from_number,
                    body=message_body,
                )
            self._write_xml(HTTPStatus.OK, _twiml_message(message))
        except Exception as exc:  # pragma: no cover
            self._write_xml(HTTPStatus.INTERNAL_SERVER_ERROR, _twiml_message(f"Server error: {exc}"))

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        payload = {
            "service": "twilio_coach_request_webhook",
            "status": "ok",
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "endpoint": parsed.path,
        }
        self.wfile.write(json.dumps(payload).encode("utf-8"))


def run_server(config_path: Path, host: str, port: int, endpoint_path: str) -> None:
    TwilioCoachRequestWebhookHandler.config_path = config_path
    server = ThreadingHTTPServer((host, port), TwilioCoachRequestWebhookHandler)
    print(f"Twilio webhook service listening on http://{host}:{port}{endpoint_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Twilio webhook for coach change requests")
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"), help="Path to config YAML")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind")
    parser.add_argument("--path", default="/twilio/coach-request", help="Webhook path")
    args = parser.parse_args()
    run_server(config_path=args.config, host=args.host, port=args.port, endpoint_path=args.path)


if __name__ == "__main__":
    main()
