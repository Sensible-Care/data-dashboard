# Zoom Phone Transcript Pipeline

Copies Zoom Phone call transcripts into a SharePoint document library in
Sensible Care's Microsoft 365 tenant, filed by team and ISO week. Transcript
text is streamed from Zoom to SharePoint and is never written to local disk.

**Full documentation: [`docs/pipeline-documentation.pdf`](docs/pipeline-documentation.pdf)**
— what the system does, how a transcript travels, where files land, and the
infrastructure it runs on. Operational procedures, configuration, rate limits
and known limitations live in [`RUNBOOK.md`](RUNBOOK.md).

## Layout

```
Call Transcripts/{Team}/{ISO year}-W{week}/HHMMSS_direction_caller_to_callee_id.txt
```

Set by `FOLDER_TEMPLATE` in `config.py`. Times and week boundaries are
Australia/Melbourne local, not UTC. `{week}` must always be paired with
`{iso_year}` — 1 January 2027 is ISO week 53 of *2026*.

## The three entry points

| | Runs | Covers |
|---|---|---|
| `webhook.py` | always on | one recording, on Zoom's signal — the primary path |
| `pipeline.py` | nightly | the last `LOOKBACK_DAYS` days — the safety net |
| `backfill.py` | until history is done | one calendar month per pass |

All three deliver through the same `deliver()` function in `pipeline.py`. There
is deliberately only one copy of "which folder, and has it already been
delivered".

## Files

| File | Purpose |
|---|---|
| `config.py` | Every setting, each an environment variable with a default |
| `pipeline.py` | The main run and the shared `deliver()` |
| `webhook.py` | Zoom webhook receiver — signature checks, queueing |
| `backfill.py` | Historical months, restart-safe |
| `seal.py` | Writes weekly manifests the daily window can never reach |
| `reconcile.py` | Compares what Zoom holds against what SharePoint holds |
| `zoom_client.py` | Zoom API, paging, throttle handling |
| `graph_client.py` | Microsoft Graph auth and throttle handling |
| `drive_writer.py` | SharePoint uploads, folders, listings |
| `team_map.py` / `team_source.py` / `team_sync.py` | Who belongs to which team, and when |
| `manifest.py` | Weekly CSV manifests and supplements |
| `models.py` | Folder and filename rules, redaction |
| `state.py` | Where persistent state lives (`STATE_DIR`) |
| `alerts.py` | Email alerting |

## Running

```bash
pip install -r requirements.txt

python3 pipeline.py                            # last LOOKBACK_DAYS
python3 pipeline.py --from 2026-09-01 --to 2026-09-30
python3 pipeline.py --dry-run                  # plan only, writes nothing
python3 seal.py                                # close out finished weeks
python3 reconcile.py                           # Zoom vs SharePoint
python3 backfill.py --status                   # months finished so far
```

Credentials come from a `.env` file, which is never committed. See the
configuration table in the PDF for the full list.

## What is not in this repository

This repository carries the production code only.

`teams.csv` and the Zoom user directory name real staff, and the `.env` file
holds live credentials. Both are excluded by `.gitignore` and are not meant to
be version controlled — the team map is maintained by the client in SharePoint
at `_config/teams.csv`.

The test suites and their synthetic fixtures are kept on the development
machine rather than here.
