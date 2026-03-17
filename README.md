# BookaField Scheduler

This job reads a schedule CSV, validates each row, and creates BookaField reservations when run in live mode.

## Configuration

- Credentials come from environment variables, not from committed config.
- `BOOKAFIELD_USERNAME` and `BOOKAFIELD_PASSWORD` must be set in the runtime environment.
- `SCHEDULE_CSV_GCS_URI` can be used to point the runner at a CSV in GCS.
- `config/config.yaml` keeps `dry_run: true` by default. Use the CLI `--live` flag only for deliberate live execution.

## Resource Mapping Rules

- `resource` values in the input CSV must exist in [`docs/resource_mapping_example.csv`](docs/resource_mapping_example.csv).
- Unmapped resources fail validation explicitly.
- This is intentional. The scheduler must not fall back to sending raw placeholder values such as `DRY_RUN_FIELD_1` to BookaField.

## Logging

- Per-attempt API failures are emitted to stdout as `attempt_failure` events.
- Live submissions also emit `bookafield_request` and `bookafield_response` debug events.
- These logs stay in place to make Cloud Run executions auditable and to avoid silent live-run failures.

## Operating Modes

- Canary input example: [`docs/live_canary_single_row.csv`](docs/live_canary_single_row.csv)
- Default mapped schedule seed: [`docs/schedule_template.csv`](docs/schedule_template.csv)
- Placeholder input example: `gs://bookafield-schedule-inputs/schedule.csv`

Do not point the deployed job back at `gs://bookafield-schedule-inputs/schedule.csv` until that object contains real mapped BookaField resource names. The original placeholder file used values such as `DRY_RUN_FIELD_1` and failed validation by design.

## Recommended Workflow

1. Update the resource map with real BookaField names and IDs.
2. Upload or generate a schedule CSV that uses those mapped resource names.
3. Run a dry-run execution and verify the summary/logs.
4. Run a one-off live execution only after the dry-run is clean.
5. Review Cloud Run stdout logs for summary, `attempt_failure`, and BookaField request/response events.
