from __future__ import annotations

import csv
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from twilio_coach_request_webhook import parse_coach_request_sms, process_incoming_twilio_coach_request


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            log_dir: {tmp_path.as_posix()}/logs
            coach_request_sync:
              output_csv: {tmp_path.as_posix()}/data/coach_requests_latest.csv
              state_file: {tmp_path.as_posix()}/data/coach_request_sync_state.json
              id_prefix: REQ
            twilio:
              validate_signature: false
              coach_request_allowed_numbers:
                - "+15551112222"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


class TwilioCoachRequestWebhookTests(unittest.TestCase):
    def test_parse_coach_request_sms_valid_message(self) -> None:
        parsed, error = parse_coach_request_sms(
            "name:Jane Coach; email:jane@example.com; team:12U Black; event:Game; "
            "date:2026-05-10; start:18:00; end:20:00; field:Fenway 1; "
            "umpire:yes; urgent:no; reason:Weather makeup"
        )
        self.assertIsNone(error)
        assert parsed is not None
        self.assertEqual(parsed["coach_name"], "Jane Coach")
        self.assertEqual(parsed["preferred_resource"], "Fenway 1")
        self.assertEqual(parsed["preferred_start_datetime"], "2026-05-10 18:00")
        self.assertEqual(parsed["needs_umpire"], "Yes")

    def test_parse_coach_request_sms_missing_field(self) -> None:
        parsed, error = parse_coach_request_sms(
            "name:Jane Coach; email:jane@example.com; team:12U Black; "
            "date:2026-05-10; start:18:00; end:20:00; field:Fenway 1; umpire:yes; reason:Weather makeup"
        )
        self.assertIsNone(parsed)
        self.assertEqual(error, "Missing required field(s): event_type")

    def test_process_incoming_twilio_coach_request_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_config(tmp_path)
            message = process_incoming_twilio_coach_request(
                config_path=config_path,
                from_number="+15551112222",
                body=(
                    "name:Jane Coach; email:jane@example.com; team:12U Black; event:Game; "
                    "date:2026-05-10; start:18:00; end:20:00; field:Fenway 1; "
                    "umpire:yes; urgent:no; reason:Weather makeup"
                ),
            )

            self.assertIn("REQ-0001", message)

            output_csv = tmp_path / "data" / "coach_requests_latest.csv"
            with output_csv.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["request_id"], "REQ-0001")
            self.assertEqual(rows[0]["coach_phone"], "+15551112222")
            self.assertEqual(rows[0]["preferred_resource"], "Fenway 1")
            self.assertEqual(rows[0]["status"], "received")

    def test_process_incoming_twilio_coach_request_rejects_unauthorized_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_config(tmp_path)
            message = process_incoming_twilio_coach_request(
                config_path=config_path,
                from_number="+15550000000",
                body=(
                    "name:Jane Coach; email:jane@example.com; team:12U Black; event:Game; "
                    "date:2026-05-10; start:18:00; end:20:00; field:Fenway 1; "
                    "umpire:yes; urgent:no; reason:Weather makeup"
                ),
            )

            self.assertIn("not authorized", message.lower())


if __name__ == "__main__":
    unittest.main()
