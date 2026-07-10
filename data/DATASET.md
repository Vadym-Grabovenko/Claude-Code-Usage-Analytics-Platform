# Claude Code Telemetry Dataset

Synthetic telemetry data for Claude Code — Anthropic's CLI tool for AI-assisted software engineering.

## Quick Start

No dependencies required — uses Python standard library only.

```bash
python3 generate_fake_data.py
```

For a realistic dataset, generate at least 100 engineers over a couple of months:

```bash
python3 generate_fake_data.py --num-users 100 --num-sessions 5000 --days 60
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--num-users` | 30 | Number of engineers |
| `--num-sessions` | 500 | Total coding sessions |
| `--days` | 30 | Time span in days |
| `--output-dir` | `output` | Output directory |
| `--seed` | 42 | Random seed for reproducibility |

## Output Files

| File | Format | Description |
|------|--------|-------------|
| `telemetry_logs.jsonl` | JSONL | Telemetry log batches |
| `employees.csv` | CSV | Employee directory |

## Telemetry Structure

Each log record contains a batch of `logEvents`. Each event has a JSON `message` with:

- `body` — event type
- `attributes` — event-specific fields
- `scope` — instrumentation metadata
- `resource` — host/user environment info

## Employee Table

| Column | Description |
|--------|-------------|
| email | Employee email |
| full_name | Full name |
| practice | Engineering practice |
| level | Seniority level (L1–L10) |
| location | Country |

## Notes

- All user identifiers are synthetic
- Prompt contents are redacted
- Employee emails match the telemetry data
