# Zoom Transcript Pipeline

Copies Zoom Phone call transcripts, Zoom meeting transcripts and Zoom Team
Chat records into a SharePoint document library in Sensible Care's Microsoft
365 tenant, filed by team and ISO week. Content is streamed from Zoom to
SharePoint and is never written to local disk.

**Full documentation: [`docs/pipeline-documentation.pdf`](docs/pipeline-documentation.pdf)**
— what the system does, how a transcript travels, where files land, and the
infrastructure it runs on. Operational procedures, configuration, rate limits
and known limitations live in [`RUNBOOK.md`](RUNBOOK.md).

## Three kinds of record, three layouts

```
Call Transcripts/{Team}/{ISO year}-W{week}/HHMMSS_direction_caller_to_callee_id.txt

Meeting Transcripts/{Team}/Meeting - {ISO year} - W{week}/HHMMSS_topic_host_id.txt

Chat Transcripts/{Team}/Chat - {ISO year} - W{week}/{channel}_{channel id}.txt
```

The phone layout is set by `FOLDER_TEMPLATE`; the other two by
`MEETING_WEEK_TEMPLATE` and `CHAT_WEEK_TEMPLATE`. The meeting and chat week
folders are spelled out differently from the phone one ("Meeting - 2026 - W41"
rather than "2026-W41") because that is the wording the client asked for and
confirmed in writing — not an inconsistency to tidy up.

Times and week boundaries are Australia/Melbourne local, not UTC. `{week}`
must always be paired with `{iso_year}` — 1 January 2027 is ISO week 53 of
*2026*.

Which team a record belongs to differs per product:

| | Team is decided by |
|---|---|
| Phone | the call's owner, or the call queue it arrived through |
| Meetings | the host — the only party Zoom guarantees to name |
| Chat | the channel map the client maintains (a channel may belong to two) |

## Entry points

| | Runs | Covers |
|---|---|---|
| `api/webhook.py` | always on | one recording, on Zoom's signal — the primary path for phone and meetings |
| `jobs/pipeline.py` | nightly | phone, the last `LOOKBACK_DAYS` days — the safety net |
| `jobs/meetings.py` | nightly | meeting transcripts |
| `jobs/chat.py` | nightly | Team Chat, the last `CHAT_LOOKBACK_DAYS` days |
| `jobs/backfill.py` | until history is done | phone history, one calendar month per pass |

Phone delivery goes through the single `deliver()` in `jobs/pipeline.py`, so
there is only one copy of "which folder, and has it already been delivered".
Meetings and chat are deliberately separate modules: they share every piece of
infrastructure — SharePoint, the team map, throttling — but nothing about their
data, so a change to one cannot break the phone pipeline.

`run_daily.sh` runs the nightly sequence in order: phone ingest, manifest
seal, reconciliation, meetings, chat. Chat is last because it is the longest
stage and the least urgent, so a replica cut short loses chat rather than the
phone ingest.

### Turning meetings and chat on and off

Both stages are behind switches read from the environment, defaulting to on:

```bash
RUN_MEETINGS=0      # the nightly skips meetings; the webhook drops meeting events
RUN_CHAT=0          # the nightly skips chat
```

These exist so delivery can be halted without disabling the whole nightly job,
which would also stop the phone pipeline. Anything written to the library is
covered by a retention label and cannot be deleted, so the ability to stop
writing — quickly, and without a redeploy — matters.

## Dedupe

SharePoint is the ledger; there is no local database to drift from reality. A
record whose id already appears in the destination is skipped, so re-running
any range is safe.

The rule differs for chat, and the difference is deliberate. Phone and meeting
records belong to exactly one team but may move between them, so "delivered to
*any* candidate folder" counts as delivered. A chat channel can belong to two
teams at once and must appear under both, so chat dedupes **per folder** —
each (channel, week, team) is decided on its own. Pooling them would write the
first folder, find the id, and silently skip the second.

A chat week is also mutable in a way a transcript is not: messages keep
arriving until the week ends, so the file is rewritten while the week is open
and frozen once `MANIFEST_SEAL_DAYS` have passed. A sealed week already present
in every team folder is skipped *before any Zoom call is made*, which is what
makes re-running a long range cheap.

## Code layout

```
core/      config, state, models, transcript, manifest, alerts,
           meeting, meeting_manifest, chat, chat_manifest, vtt
clients/   zoom_client, zoom_meetings, zoom_chat, zoom_directory,
           graph_client, drive_writer, sharepoint_site
teams/     team_map, team_source, team_sync, channel_map, channel_source
jobs/      pipeline, backfill, seal, reconcile, meetings, chat
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
| `core/meeting.py` | `MeetingTranscript`, meeting layout and filenames |
| `core/meeting_manifest.py` | Weekly manifests for meeting transcripts |
| `core/chat.py` | `ChatMessage`, `ChannelWeek`, chat layout and rendering |
| `core/chat_manifest.py` | Weekly manifests for chat records |
| `core/vtt.py` | WebVTT parsing, and rendering a transcript as readable text |
| `core/alerts.py` | Email alerting |
| `jobs/pipeline.py` | The main phone run and the shared `deliver()` |
| `jobs/meetings.py` | Meeting transcripts to SharePoint |
| `jobs/chat.py` | Team Chat to SharePoint, including dual-team delivery |
| `jobs/backfill.py` | Historical phone months, restart-safe |
| `jobs/seal.py` | Writes weekly manifests the daily window can never reach |
| `jobs/reconcile.py` | Compares what Zoom holds against what SharePoint holds |
| `api/webhook.py` | Zoom webhook receiver — signature checks, queueing |
| `clients/zoom_client.py` | Zoom API, paging, throttle handling |
| `clients/zoom_meetings.py` | Cloud recordings, filtered to transcript files |
| `clients/zoom_chat.py` | Channel resolution and message retrieval |
| `clients/graph_client.py` | Microsoft Graph auth and throttle handling |
| `clients/drive_writer.py` | SharePoint uploads, folders, listings |
| `teams/` | Who belongs to which team, and when |
| `teams/channel_map.py` | Chat channel → team(s), with the client's spellings |
| `teams/channel_source.py` | Fetches and validates the channel list from SharePoint |

## Running

```bash
pip install -r requirements.txt

# phone
python3 -m jobs.pipeline                            # last LOOKBACK_DAYS
python3 -m jobs.pipeline --from 2026-09-01 --to 2026-09-30
python3 -m jobs.pipeline --dry-run                  # plan only, writes nothing
python3 -m jobs.seal                                # close out finished weeks
python3 -m jobs.reconcile                           # Zoom vs SharePoint
python3 -m jobs.backfill --status                   # months finished so far

# meetings
python3 -m jobs.meetings                            # last LOOKBACK_DAYS
python3 -m jobs.meetings --from 2025-01-01 --to 2026-10-08
python3 -m jobs.meetings --dry-run

# team chat
python3 -m jobs.chat                                # last CHAT_LOOKBACK_DAYS
python3 -m jobs.chat --from 2025-08-01 --to 2025-10-31
python3 -m jobs.chat --dry-run

# the chat channel list, which the client maintains in SharePoint
python3 -m teams.channel_source --show
python3 -m teams.channel_source --validate channels.csv
python3 -m teams.channel_source --pull
```

Credentials come from a `.env` file, which is never committed. See the
configuration table in the PDF for the full list.

## Client-maintained configuration

Two files live in the library's `_config` folder so the client can change them
without a redeploy. Both are downloaded, validated and cached on every run, and
a copy that looks wrong is **refused** in favour of the last good one, with an
alert — refusing to update is always safer than accepting nonsense that would
silently re-file thousands of records.

| File | Decides |
|---|---|
| `_config/teams.csv` | which team each member of staff belongs to, and when |
| `_config/chat_channels.csv` | which chat channels to archive, and under which team(s) |

The channel list is the client's own spreadsheet. It uses several spellings for
the same team — "Accounts" and "Accounts Team", "Care Partner" and "Care
Partners", "Nursing Team" for Nursing — so `teams/channel_map.py` normalises
them against the staff list. An unrecognised name is reported and falls back to
`_Unassigned` rather than stopping the run.

## What is not in this repository

This repository carries the production code only.

`teams.csv`, the chat channel list and the Zoom user directory all name real
people, and the `.env` file holds live credentials. All are excluded by
`.gitignore` and are not meant to be version controlled.

The test suites and their synthetic fixtures are kept on the development
machine rather than here.
