from __future__ import annotations

import csv
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))


class _FakeLoc:
    def __init__(self, rows: dict[str, dict[str, str]]) -> None:
        self._rows = rows

    def __getitem__(self, key: str) -> dict[str, str]:
        return self._rows[key]


class _FakeDataFrame:
    def __init__(self, rows: dict[str, dict[str, str]]) -> None:
        self._rows = rows
        self.index = rows.keys()
        self.loc = _FakeLoc(rows)


def _fake_read_csv(path: str | Path, index_col: str) -> _FakeDataFrame:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = {str(row[index_col]): row for row in reader}
    return _FakeDataFrame(rows)


sys.modules.setdefault("pandas", SimpleNamespace(read_csv=_fake_read_csv))

from bookafield_client import BookaFieldClient
from run_scheduler import run_scheduler


class RuntimeConsistencyTests(unittest.TestCase):
    def test_bookafield_client_uses_configured_resource_map_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            resource_map = tmp_path / "custom_resource_map.csv"
            resource_map.write_text(
                "resource_name,resource_id\nCustom Field,999\n",
                encoding="utf-8",
            )

            config = {
                "base_url": "https://example.test",
                "resource_map_path": str(resource_map),
                "auth": {
                    "endpoint": "/login",
                    "username": "user",
                    "password": "pass",
                },
            }

            with patch.object(BookaFieldClient, "authenticate", return_value=None):
                client = BookaFieldClient(config=config)

            self.assertIn("Custom Field", client._resource_mapping.index)
            self.assertEqual(str(client._resource_mapping.loc["Custom Field"]["resource_id"]), "999")

    def test_run_scheduler_does_not_authenticate_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schedule_path = tmp_path / "schedule.csv"
            schedule_path.write_text(
                (
                    "event_type,team,resource,start_datetime,end_datetime\n"
                    "Practice,Team A,Field A,2099-01-01 09:00,2099-01-01 10:00\n"
                ),
                encoding="utf-8",
            )

            resource_map_path = tmp_path / "resource_map.csv"
            resource_map_path.write_text(
                "resource_name,resource_id\nField A,101\n",
                encoding="utf-8",
            )

            config_path = tmp_path / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    base_url: https://example.test
                    dry_run: false
                    log_dir: {tmp_path.as_posix()}/logs
                    resource_map_path: {resource_map_path.as_posix()}
                    auth:
                      endpoint: /login
                      username: user
                      password: pass
                    reservation:
                      endpoint: /api/reservations
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with patch("run_scheduler.BookaFieldClient") as client_cls:
                client = client_cls.return_value
                client.create_reservation.return_value = ("res-123", {})

                summary = run_scheduler(
                    schedule_csv_path=schedule_path,
                    config_path=config_path,
                    force_live=True,
                )

            client.authenticate.assert_not_called()
            self.assertEqual(summary["counts"]["created"], 1)
            self.assertEqual(summary["counts"]["api_failures"], 0)


if __name__ == "__main__":
    unittest.main()
