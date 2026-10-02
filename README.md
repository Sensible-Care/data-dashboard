# Zoom Phone Transcript Pipeline

Copies Zoom Phone call transcripts into a SharePoint document library in
Sensible Care's Microsoft 365 tenant, filed by team and ISO week. Transcript
text is streamed from Zoom to SharePoint and is never written to local disk.

**Full documentation: [`docs/pipeline-documentation.pdf`](docs/pipeline-documentation.pdf)**
— what the system does, how a transcript travels, where files land, and the
infrastructure it runs on. Operational procedures, configuration, rate limits
and known limitations live in [`RUNBOOK.md`](RUNBOOK.md).

## Where transcripts land

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

## Code layout

```
core/      config, state, models, transcript, manifest, alerts
clients/   zoom_client, zoom_directory, graph_client, drive_writer, sharepoint_site
teams/     team_map, team_source, team_sync
jobs/      pipeline, backfill, seal, reconcile
api/       webhook
tools/     provision, verify, check_zoom
docs/      generated documentation
```

Every entry point runs as a module from the repository root, so the root is the
import path and no `sys.path` juggling is needed:

```bash
python3 -m jobs.pipeline
python3 -m api.webhook
```

| Module | Purpose |
|---|---|
| `core/config.py` | Every setting, each an environment variable with a default |
| `core/models.py` | Folder and filename rules, `PhoneRecording`, redaction |
| `core/state.py` | Where persistent state lives (`STATE_DIR`) |
| `core/manifest.py` | Weekly CSV manifests and supplements |
| `core/alerts.py` | Email alerting |
| `jobs/pipeline.py` | The main run and the shared `deliver()` |
| `jobs/backfill.py` | Historical months, restart-safe |
| `jobs/seal.py` | Writes weekly manifests the daily window can never reach |
| `jobs/reconcile.py` | Compares what Zoom holds against what SharePoint holds |
| `api/webhook.py` | Zoom webhook receiver — signature checks, queueing |
| `clients/zoom_client.py` | Zoom API, paging, throttle handling |
| `clients/graph_client.py` | Microsoft Graph auth and throttle handling |
| `clients/drive_writer.py` | SharePoint uploads, folders, listings |
| `teams/` | Who belongs to which team, and when |

## Running

```bash
pip install -r requirements.txt

python3 -m jobs.pipeline                            # last LOOKBACK_DAYS
python3 -m jobs.pipeline --from 2026-09-01 --to 2026-09-30
python3 -m jobs.pipeline --dry-run                  # plan only, writes nothing
python3 -m jobs.seal                                # close out finished weeks
python3 -m jobs.reconcile                           # Zoom vs SharePoint
python3 -m jobs.backfill --status                   # months finished so far
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
