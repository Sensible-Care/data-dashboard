# Runbook — Zoom Transcripts → SharePoint

Operational guide for the nightly pipeline, which delivers three kinds of
record: **Zoom Phone** call transcripts, **Zoom meeting** transcripts and
**Zoom Team Chat** conversations.
Client: Sensible Care Pty Ltd. Timezone for every date below: **Australia/Melbourne**.

---

## 1. What it does

At 02:00 Melbourne each night, the pipeline:

1. Asks Zoom for call recordings from the last 2 days (a deliberate overlap).
2. Keeps only those with a transcript.
3. Works out which person the call belongs to, then which team they are in.
4. Streams the transcript from Zoom straight to SharePoint — it never lands on disk.
5. Files it as `<Team>/<ISO year>-W<week>/<time>_<direction>_<from>_to_<to>_<id>.txt`.
6. Once a week has been closed for 3 days, writes a manifest CSV into each week folder.

Then, in the same run and in this order:

7. **Meeting transcripts** — cloud recordings from the last `LOOKBACK_DAYS`,
   keeping only the TRANSCRIPT file and filing it under the host's team.
8. **Team Chat** — the last `CHAT_LOOKBACK_DAYS`, one file per channel per
   week, under every team the channel is mapped to.

Chat runs last on purpose: it is the longest stage (one Zoom call per channel
per day) and the least urgent, so a replica cut short loses chat rather than
the phone ingest. Both stages can be switched off independently — see §2b.

A separate reconciliation job compares what Zoom has against what SharePoint has
and emails on any gap. It covers phone only.

**There is no database.** SharePoint is the ledger. Each filename ends with the
first 12 characters of the Zoom recording id, and the pipeline skips anything
already present. Delete a file and the next run restores it.

---

## 2. Layout on disk (SharePoint)

```
Call Transcripts/                     <- document library
├── Care Partner Team/
│   ├── 2026-W38/
│   │   ├── 091500_inbound_+61...__a3f2190855cd.txt
│   │   └── _manifest_2026-W38_Care Partner Team.csv
│   └── 2026-W39/
├── Service Coordination/
├── Accounts Team/
└── _Unassigned/                      <- anyone with no team mapping
```

`_Unassigned` is not an error state. It is where calls go when the person is not
in `teams.csv`. Check it weekly; if it is filling up, the mapping needs updating.

Meetings and chat sit in their own top-level folders in the same library:

```
Meeting Transcripts/
├── Care Partner Team/
│   └── Meeting - 2026 - W41/
│       ├── 092316_Care Management Workshop_care_122c0b64.txt
│       └── _manifest_2026-W41_Care Partner Team.csv

Chat Transcripts/
├── Accounts Team/
│   └── Chat - 2026 - W41/
│       ├── CMs + ACCOUNTS_85e2d6be48fb.txt
│       └── _manifest_2026-W41_Accounts Team.csv
```

Note the week folder is worded differently from the phone layout
("Meeting - 2026 - W41", not "2026-W41"). That is the client's wording,
confirmed in writing. Do not "tidy" it.

---

## 2a. What is different about meetings and chat

**Meetings.** One file per transcript. A meeting has many participants spread
across teams, so there has to be one rule: the team is the **host's**, because
the host is the only party Zoom guarantees to name. A meeting recorded in parts
produces more than one transcript file, and each is delivered separately — the
id in the filename is the *file's* id, not the meeting's.

Not every meeting has a transcript. Of 415 recordings in the account, 275 had
one; the rest have audio and video but Zoom never transcribed them, and none of
them has a caption file either. There is nothing to collect for those.

**Chat.** One file per channel per ISO week, with threaded replies printed
underneath the message they answer rather than in raw arrival order. Two things
behave unlike anything else in the pipeline:

1. **A channel can belong to two teams**, and the client asked for the record
   to appear under both. So chat dedupes **per folder** — each (channel, week,
   team) is decided on its own. The phone and meeting rule ("delivered to *any*
   candidate folder means delivered") would write the first folder, find the id,
   and silently skip the second. 13 of the 54 selected channels are dual-team.

2. **A chat week is mutable.** Messages keep arriving until the week ends, so
   the file is rewritten on each run while the week is open, then frozen once
   `MANIFEST_SEAL_DAYS` have passed. A sealed week already present in every team
   folder is skipped *before any Zoom call is made*.

Ad-hoc group conversations (Zoom channel `type: 4`) are **never** archived.
Those are personal chats named after their participants; the client's selection
contains none, and `clients/zoom_chat.py` refuses them even if one is listed.

### Channel resolution is per-user, and that matters

There is no account-wide "list every channel" endpoint. Channels are listed per
user, and only the channels that user belongs to. The app's own account can see
82 of this tenant's ~640 channels, and only 5 of the 54 the client selected — so
the only way to reach them all is to walk every active user and union the
results, then read messages as a user who is a member of the channel.

Consequence worth knowing: **if a channel's only remaining members are all
deactivated Zoom users, that channel becomes unreachable.** Nothing is lost
that was already delivered, but no new messages can be collected from it.

---

## 2b. Switching meetings and chat off

Both stages are behind environment switches on the job, defaulting to **on**:

```bash
# stop delivery without touching the schedule
az containerapp job update -g rg-zoom-transcripts -n job-transcripts-nightly \
  --set-env-vars RUN_MEETINGS=0 RUN_CHAT=0

# the webhook has the same switch, so a Zoom event cannot write either
az containerapp update -g rg-zoom-transcripts -n app-transcripts-webhook \
  --set-env-vars RUN_MEETINGS=0

# back on
... --set-env-vars RUN_MEETINGS=1 RUN_CHAT=1
```

**Do not disable the job's schedule to stop meetings or chat.** All five stages
share one job, so that would also stop the phone pipeline, the manifest seal and
reconciliation.

Why this exists: everything written to the library is covered by a two-year
retention label and **cannot be deleted**. If a layout or rendering problem is
found, the only useful response is to stop writing immediately, and that must
not require a redeploy.

With `RUN_MEETINGS=0` the webhook still answers Zoom with 200 and simply drops
the event — a non-2xx would make Zoom retry it three times. The transcript is
picked up by the batch run once the switch is back on.

---

## 3. Daily operation

Cron entry (**not installed yet** — see §8):

```cron
0 2 * * *  /opt/transcripts/run_daily.sh
```

`run_daily.sh` takes a `flock` so two runs can never overlap, writes a dated log,
rotates logs at 30 days, and exits non-zero on failure.

Manual run:

```bash
python3 -m jobs.pipeline                      # last 2 days
python3 -m jobs.pipeline --lookback 7         # last 7 days
python3 -m jobs.pipeline --from 2026-09-01 --to 2026-09-07
python3 -m jobs.pipeline --dry-run            # plan only, writes nothing
```

Weekly manifests:

```bash
python3 -m jobs.seal                          # the most recently sealed week
python3 -m jobs.seal --weeks 6                # catch up on a backlog
python3 -m jobs.seal --week 2026-W39          # one specific week
python3 -m jobs.seal --dry-run                # say what it would do
```

Note `seal.py --dry-run` still **reads** SharePoint — it has to list the week
folders to know which manifests are missing. It writes nothing. This differs
from `pipeline.py --dry-run`, which makes no network calls at all.

Reconciliation:

```bash
python3 -m jobs.reconcile                     # yesterday
python3 -m jobs.reconcile --day 2026-09-20
python3 -m jobs.reconcile --no-alert          # check without emailing
```

### Why sealing is a separate pass

`pipeline.py` gathers manifest rows from whatever its window covers, and that
window is `LOOKBACK_DAYS` (2) wide. A week only becomes sealable
`MANIFEST_SEAL_DAYS` (3) after it closes — by which point the daily window has
moved on to the next week and holds none of that week's calls. Simulated over
21 consecutive daily runs, the number of manifests written was **zero**.

Widening the lookback is not the fix. A window that reaches back far enough to
touch the sealed week still only covers *part* of it, and a manifest listing
four days of a seven-day week is worse than none.

So `seal.py` runs over the week's exact Monday–Sunday range. Everything there
is already delivered, so it uploads nothing and just collects the rows —
and anything genuinely missing is delivered on the way, which makes this a
weekly self-heal as well. It is safe to run nightly: a week whose manifests
are all present is skipped without a single Zoom call.

A sealed manifest is **never** rewritten, because a retention label may have
made it a record. Anything arriving afterwards goes into a numbered
supplement beside it (`..._supplement_01.csv`), holding only the late rows.

---

## 4. First-time setup

In order. Each step is idempotent.

```bash
# 0. Build the environment (once per machine)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
#    Then use .venv/bin/python everywhere below, NOT the system python3 --
#    Ubuntu 20.04 ships requests 2.22.0 (2019) and the platform installs
#    2.32.x, and testing against one while deploying the other is how a
#    surprise reaches production.

# 1. Confirm Zoom credentials still work
python3 -m tools.check_zoom

# 2. Create the SharePoint library and its columns
python3 -m tools.provision --dry-run
python3 -m tools.provision

# 3. Confirm the site resolves and cache the drive id
python3 -m clients.sharepoint_site

# 4. Build the team mapping from the client's phone handout
pdftotext -layout "Phone Numbers & Emails.pdf" phone_list.txt
python3 build_team_map.py

# 5. Optional: hand teams.csv over to the client (see 4b)
python3 -m teams.team_source --push

# 6. Rehearse without writing
python3 -m jobs.pipeline --lookback 1 --dry-run

# 7. Go
python3 -m jobs.pipeline --lookback 1
```

---

## 4a. When someone changes department

Transcripts are filed by the team the person was in **on the day of the call**,
not the team they are in today. So a transfer must be recorded with dates,
never by overwriting the row.

Say Bea moves from Accounts Team to Nursing on **15 October 2026**. Edit
`teams.csv` and replace her single row with two:

```csv
email,team,valid_from,valid_to
carol@example.com,Accounts Team,,2026-10-14
carol@example.com,Nursing,2026-10-15,
```

Blank `valid_from` means "since forever", blank `valid_to` means "still
current". Check it resolves as you intended:

```bash
python3 -c "
from team_map import TeamMap
from datetime import date
tm = TeamMap('teams.csv')
for d in [(2026,10,14),(2026,10,15)]:
    print(d, tm.for_email('carol@example.com', date(*d)))"
```

September's calls stay in `Accounts Team/2026-W39/`. November's land in
`Nursing/`. Nothing already uploaded moves, which is the point — the archive
reflects who handled the call at the time.

### The simple way: just change the team

You can also edit the single row and leave the dates blank:

```csv
carol@example.com,Nursing,,
```

`team_sync.py` compares `teams.csv` against `teams_effective.csv` on every
run. When someone's team has changed and no dates were given, it closes the
old span yesterday, opens the new one today, and emails to say so. The dated
version above is written into `teams_effective.csv` automatically.

    teams.csv             what you or the client edit. Never written to.
    teams_effective.csv   derived, dated, and what the pipeline reads.

Dates you write yourself always win -- an explicit statement about history
beats an inferred one, so backdating a move works exactly as you would expect.

### Why nothing duplicates

Before uploading, the pipeline checks **every team folder for that ISO week**,
not just the one the current mapping points at (`models.week_folders`). A
transcript already delivered under the old team is therefore recognised no
matter how the mapping has since changed. Reconciliation looks the same way,
so a move never produces a false gap report either.

This matters in two cases auto-dating cannot cover: a move the client
**backdates** past calls already delivered, and losing `teams_effective.csv`
(a fresh container, for instance) so there is no history to date against.
`test_team_change.py` asserts both.

### Never add an undated row alongside dated ones

An undated span sorts first and matches every date, so it silently masks the
history. `build_team_map.py` avoids this, and `test_pipeline.py` asserts it.

`build_team_map.py` **preserves** any row carrying dates and ignores the phone
handout for that person, so regenerating after a new handout will not discard
recorded transfers. It prints what it kept.

## 4b. Where teams.csv lives

By default it is a file beside the code. That is fine on a laptop or a VPS
and useless in a container: there is nothing for the client to edit, and a
redeploy would be needed for every staff change.

Set `TEAM_CSV_SOURCE=sharepoint` and it is read from the library instead:

```
Call Transcripts/_config/teams.csv
```

```bash
python3 -m teams.team_source --show              # which source is in use
python3 -m teams.team_source --validate teams.csv
python3 -m teams.team_source --push              # upload the local file, once
python3 -m teams.team_source --pull              # fetch it now
```

Each run downloads it, checks it, and caches it in `STATE_DIR`. Everything
downstream is unchanged — `team_sync` still just reads a path.

### Why it is checked so carefully

This one file decides where every transcript goes. A bad edit is not a
crash; it is 73 people silently falling to `_Unassigned` and the next run
re-filing the library. So a download is **refused** and the previous good
copy kept, with an alert, when it:

| | |
|---|---|
| is empty, or not UTF-8 text | a failed upload or the wrong file |
| has no `email` or `team` column | someone saved the wrong sheet |
| has no usable rows | header only, or every row missing a field |
| has **under half** the previous row count | a filtered view saved by accident — the dangerous one, because it looks valid |

If the remote file cannot be reached at all, the previous copy is used and
the run continues. Yesterday's mapping is far better than none.

The **only** case that stops a run is no remote file *and* no cached copy —
because filing everything under `_Unassigned` would be worse than not
running.

A genuine large reduction is not a dead end. There is no cache file to
delete inside a container, so:

```bash
TEAM_CSV_MIN_FRACTION=0 python3 -m jobs.pipeline     # accept it for one run
```

`teams.csv` is still never written to. `teams_effective.csv` remains the
derived file, exactly as in §4a.

---

## 4c. Where the chat channel list lives

`_config/chat_channels.csv` in the same library, maintained by the client
exactly as `teams.csv` is. It is their own spreadsheet, unchanged:

```
Channel name,Channel owner,Members,Which Team(s)
CMs + ACCOUNTS,A. Example,40,Care Partner + Accounts
```

```bash
python3 -m teams.channel_source --show                  # what is cached
python3 -m teams.channel_source --validate <file>       # check, no network
python3 -m teams.channel_source --pull                  # fetch now
python3 -m teams.channel_source --push <file>           # upload (first time)
```

Adding or removing a channel is a row edit; no redeploy, no restart. The next
nightly run picks it up.

### Team names are normalised, not matched literally

The client's file uses 18 spellings for 12 teams — "Accounts" and "Accounts
Team", "Care Partner", "Care Partners" and "Care Partner Team", "Nursing Team"
for Nursing, plus one typo ("Care Coorcinators"). `teams/channel_map.py` holds
the alias table and resolves each against the staff list, preferring an exact
match so a team the client adds later works without an alias being added here
first.

A name that still does not resolve is **reported and filed under
`_Unassigned`** rather than stopping the run — a new team should still get its
chat archived somewhere findable.

A channel listed twice keeps the **first** row and warns. Taking the later row
would quietly change which teams it is delivered to.

### Why it is checked as carefully as teams.csv

This file decides *which channels are archived at all*. A truncated or
half-saved upload does not crash anything — it silently stops archiving the
channels that fell off the end, and nobody notices until someone goes looking
for a record that was never written.

So a download that loses more than half the channels is **refused** and the
previous good copy stays in force, with an alert. Override for one run with
`CHAT_CSV_MIN_FRACTION=0` if the reduction is genuine.

---

## 5. Historical backfill

One-off. Roughly 150,000 transcripts, 8–12 hours. **Run it on the client's
infrastructure, never a laptop** — it moves production data.

```bash
python3 -m jobs.backfill --from 2025-07 --to 2026-09 --dry-run
python3 -m jobs.backfill --from 2025-07 --to 2026-09
python3 -m jobs.backfill --status
```

Safe to kill at any point. Progress is recorded per month in
`.backfill_progress.json`; rerunning the same command carries on where it stopped.
Deleting that file costs time, not correctness — SharePoint is still the ledger.

Start no earlier than **July 2025**. Zoom holds no transcripts before that, so
earlier months only burn API calls.

Zoom retention is a **rolling ~630 days**, so the oldest month available moves
forward continuously. Anything not backfilled is eventually lost for good.

### 5a. Meeting and chat backfill

Both are complete as of 8 October 2026. Recorded here because the shape of the
job is not obvious and the numbers are the baseline for spotting a problem.

```bash
python3 -m jobs.meetings --from 2025-01-01 --to 2026-10-08    # ~7 min
python3 -m jobs.chat     --from 2025-08-01 --to 2025-10-31    # ~36 min per quarter
```

| | Delivered | Range | Runtime |
|---|---|---|---|
| Meetings | 277 files, 118 manifests | 2025-W20 → 2026-W41 | 7 min |
| Chat | 1,394 files, 513 manifests | 2025-W06 → 2026-W41 | ~3 h |

**Run chat in quarterly chunks**, not one long pass. Each chunk then sits well
inside the replica timeout even if Zoom throttles, and a chunk that dies costs
only its own slice. Chunk boundaries may overlap by a day without harm: the
edge week is expanded to a whole week by both neighbours, and the second pass
finds identical content and writes nothing.

Both are safe to interrupt and re-run. A sealed week already delivered to every
team folder is skipped before any Zoom call, so finished work costs nothing on a
second pass.

**The one place that is not true:** a week in which a channel had *no* messages
produces no file, so there is nothing for the seal check to recognise and those
days are re-fetched every run. It is bounded and cheap at the nightly's 14-day
window (~1,200 calls against Zoom's 60,000/day), but it is why re-running a long
historical range costs most of its original time rather than nothing.

**Zoom holds no Team Chat before February 2025**, and effectively nothing before
July 2025 — across all 54 channels and every day from 1 January to 3 August
2025, only four weeks had any content at all. Nothing exists in 2024. Earlier
dates only burn API calls.

**Zoom holds no cloud recordings before 2025** either: 2023 and 2024 both return
zero meetings.

A transient `502` from Zoom fails one channel and the run continues to the next —
check `failures.log` and simply re-run the same range, which retries only the
failed channel.

---

## 6. Alerts

Sent via Microsoft Graph as `datadashboard@sensiblecare.com.au`.

```
ALERT_TRANSPORT=graph
ALERT_FROM=datadashboard@sensiblecare.com.au
ALERT_EMAIL=datadashboard@sensiblecare.com.au
```

Switch to SMTP by setting `ALERT_TRANSPORT=smtp` plus `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`.

> **Note.** The `Mail.Send` application permission lets this app send as *any*
> mailbox in the tenant. To confine it to the one address, run this in Exchange
> Online PowerShell:
>
> ```powershell
> New-ApplicationAccessPolicy -AppId 864dd138-08dc-42eb-b518-bfa43e781eab `
>   -PolicyScopeGroupId datadashboard@sensiblecare.com.au `
>   -AccessRight RestrictAccess -Description "Zoom transcript pipeline"
> ```

---

## 7. Purview retention labelling

**Not automated, and deliberately so.** Retention labels are published from the
Purview compliance portal and cannot be created through Graph.

Once the client publishes a label:

1. Purview → Records management → **Label policies** → publish the label to this site.
2. SharePoint → the library → **Library settings** → **Apply label to items in this list**.
3. Choose the label as the **default**.

Every file then inherits it on upload, with no pipeline code. If the label
*declares items as records*, uploaded files become immutable — the pipeline can
still create new files but can never overwrite one, which is compatible with how
it works (it never overwrites) **except** for manifest supplements. Confirm
before enabling.

---

## 8. Things deliberately not done

| | Why |
|---|---|
| Cron not installed | No host chosen yet |
| Purview label | Client has not published one |
| Queue→team map only covers Reception | ~99% of calls attribute via the person; queues are the fallback |

### Known deviation from the brief

The brief asks for *"scripted, version-controlled provisioning — not manual
portal clicking."* The app's per-site grant is **`write`**, and creating a
document library needs **`fullcontrol`**, so the `Call Transcripts` library was
created once by hand in the browser.

Everything else remains scripted: folders, uploads, manifests, and the columns
(`provision.py` creates them the moment the grant is upgraded).

Nothing in the pipeline is impaired by this. `write` is sufficient to create
folders, upload, list and delete, which is the whole of the daily job. The only
casualties are the six list columns, and **nothing populates those today** —
all per-call metadata lives in the filename and the per-week manifest CSV.

To close the gap, ask the tenant admin to PATCH the grant to `fullcontrol`
(see §9), then run `python3 -m tools.provision`. Until then, use
`python3 -m tools.provision --skip-columns` to avoid six expected 403s.

---

## 8a. Real-time delivery (webhook)

Optional, and independent of the nightly job — if the webhook is down, the
nightly run still collects everything. It only makes delivery faster.

```bash
python3 -m api.webhook                  # serve on $PORT (default 8080)
python3 -m api.webhook --self-test      # verify signing works, offline
python3 -m api.webhook --status         # what is waiting
python3 -m api.webhook --drain          # process the queue once and exit
```

| Endpoint | |
|---|---|
| `POST /` | Zoom events |
| `GET /healthz` | liveness probe |

### Why there is a queue

Zoom allows **three seconds** for a response. Downloading a transcript and
uploading it to SharePoint takes longer. Miss the deadline and Zoom retries
at 5, 20 and 60 minutes, then gives up permanently.

So the handler verifies the signature, writes the event to a queue, and
answers 200 — nothing else. A worker thread delivers afterwards. The queue is
a directory of small JSON files under `STATE_DIR`, one per recording id, so:

* a container restart resumes instead of dropping what was in flight
* a retried event overwrites its own file rather than queueing twice
* there is no database or message broker to run

After `MAX_ATTEMPTS` (5) failures an event moves to `queue/failed/` rather
than retrying forever. `reconcile.py` is the backstop that notices the gap.

### What it refuses

Signature (`x-zm-signature`, HMAC-SHA256 over `v0:{timestamp}:{body}`),
replayed requests older than 5 minutes, tampered bodies, and anything over
1 MB. Unknown event types get a **200** on purpose — a non-2xx would make
Zoom retry a subscription we never asked for.

`endpoint.url_validation` is answered with the `encryptedToken` challenge.
Zoom re-runs this roughly **every 72 hours**, not just at setup, so the
endpoint must keep answering it for the life of the subscription.

### Running alongside the nightly job

Both can deliver the same recording. That is safe and needs no lock:
filenames are derived from the recording id, so both write to the *same*
path and an upload replaces rather than duplicating. The worst case is
wasted work, not a second copy.

The webhook worker never writes manifests — `seal.py` owns those.

### Any path works, on purpose

`do_POST` never inspects the request path, so Zoom can be pointed at `/`,
`/zoom`, `/zoom/transcripts` or anything else and all behave identically.
This is deliberate and should stay that way:

* security comes from the HMAC signature, not from the URL being secret;
* a typo in the path configured at Zoom's end would otherwise look exactly
  like an outage -- events silently 404ing with nothing to show why.

**Do not add path matching to `do_POST`.** Whatever path is registered with
Zoom would stop working the moment it did not match, and the symptom would
be "transcripts stopped arriving" with a healthy-looking endpoint.

`do_GET` does match paths -- `/`, `/healthz` and `/health` answer, anything
else is a 404. That is only for humans and probes.

### Still blocked

Registering the URL in Zoom needs access to the **Data Dashboard** app in
the Zoom Marketplace, which we do not have. Everything above is testable
without it; only the final registration is not.

---

## 9. Troubleshooting

**`401` from Graph**
Bad `SP_CLIENT_SECRET` or `SP_TENANT_ID`. Entra secrets expire — check the expiry
date in the portal. Note the portal shows both a *Value* and a *Secret ID*; the
`.env` needs the **Value** (the one containing `~`), not the GUID.

**`403` from Graph**
The app authenticated but has no grant on the site. `Sites.Selected` consent
alone grants access to nothing; the per-site grant must also exist:

```
POST /sites/{site-id}/permissions   role: fullcontrol
```

**`no library named 'Call Transcripts'`**
Run `python3 -m tools.provision`.

**Everything lands in `_Unassigned`**
`teams.csv` is missing or empty. Rebuild it with `build_team_map.py`.

**`throttledMidStream`**
Graph throttled us mid-upload. A streamed body cannot be safely replayed, so the
transfer is abandoned rather than risk a truncated file. The next run picks it up.

**Zoom `next_page_token` expired**

    400 The next page token is invalid or expired.

Tokens last 15 minutes. This used to happen on long ranges because pagination
was interleaved with uploading: page 2 was requested only after all 300
records from page 1 had been downloaded and delivered, by which point the
token was dead. `iter_recordings` now pages a whole window to the end before
yielding anything, so the token is only used for a few rapid calls.

If it reappears, the window itself is too large to page within 15 minutes --
lower `ZOOM_WINDOW_DAYS`. Re-running is always safe; delivered files are
skipped.

**A month failed during backfill**
It was not recorded, so the same command retries it. Check `failures.log` for
individual recordings that failed permanently.

**A chat channel failed with `502 <!DOCTYPE html>`**
A transient Zoom gateway error. The run logs it and carries on to the next
channel, so one bad channel does not lose the rest. Re-run the same range —
every other channel-week is already sealed and present, so only the failed one
is actually re-fetched. Seen once during the October 2026 backfill.

**A chat channel cannot be found**

    <name>: no such channel is visible to any active user

Channels are listed per user, so a channel is only reachable while at least one
of its members is an **active** Zoom user. If everyone in it has been
deactivated, it drops out. Check the channel still exists and has an active
member, then confirm the name in `_config/chat_channels.csv` matches Zoom
exactly — matching is exact on purpose, because this tenant contains both a
`#Brokerage` channel and an ad-hoc chat called `Brokerage`.

**A chat channel resolves to an ad-hoc conversation**

    <name>: this is an ad-hoc group conversation, not a channel -- skipped

Zoom `type: 4` is a group DM named after its participants, not a channel. These
are never archived. Remove the row, or give the real channel's name.

**Meeting transcripts only appear once a day**
The webhook event is not subscribed. Zoom logs every event it sends, so check
for `recording.transcript_completed` in the webhook's console logs; if there is
nothing, the subscription is missing (see §8a, "Still blocked"). Batch delivery
continues meanwhile — nothing is lost, it just arrives within the day rather
than within minutes.

**`.site.json is unreadable` on startup**
The cached site/drive id was damaged — a truncated write, or an accidental edit.
Harmless now: the run says so, resolves the site again from `SP_SITE_HOSTNAME`
and rewrites the cache. Before October 2026 this raised `JSONDecodeError` and
stopped every entry point at startup, so if an older image is ever rolled back,
delete `$STATE_DIR/.site.json` to recover.

---

## 8b. Python and dependencies

Pins in `requirements.txt` support **3.8 through 3.12**, deliberately. Local
development is on Ubuntu 20.04's Python 3.8; the platform build uses
something newer and chooses the version itself. One file covering both
removes the "works on my machine" gap.

Do not add `pkg_resources==0.0.0` if you ever regenerate this with
`pip freeze` on Ubuntu. It is a packaging artefact, not a real package, and
it fails the build in Azure.

Five places still call `datetime.utcnow()`, deprecated in 3.12. Fixing it is
fine, but the obvious fix is wrong: `datetime.now(timezone.utc).isoformat()`
ends `+00:00` where the current code ends `Z`, and that string is the
`uploaded_utc` column of a manifest format the client has signed off. Pin
the exact output with a test before changing it.

---

## 9a. State that must outlive the container

Five files are produced at runtime rather than shipped with the code:

| File | Losing it costs |
|---|---|
| `teams_effective.csv` | The history of who moved when. Nothing duplicates — SharePoint is still the ledger and cross-folder dedupe is tested against exactly this — but the next run stops alerting on moves it has forgotten |
| `.backfill_progress.json` | A 10-hour job restarts from month one |
| `failures.log` | A permanently broken item is retried forever with nobody the wiser |
| `.site.json` | One extra Graph call at startup (a cache) |
| `.zoom_directory.json` | One extra Zoom call at startup (a cache) |

On a laptop or a VPS these sit beside the code and nobody thinks about it. In
a container the filesystem is discarded on every restart. `STATE_DIR` points
them somewhere persistent:

```bash
STATE_DIR=/data python3 -m jobs.pipeline
```

Unset — the default — every path is the bare filename it has always been, so
local behaviour is unchanged. In Azure, mount an Azure Files share at `/data`
and set `STATE_DIR=/data`. On a VPS, any directory that survives a redeploy.

`test_state.py` runs a real pipeline in a subprocess and fails if *anything*
lands next to the code while `STATE_DIR` is set. Half-migrated is the worst
outcome: it works locally, then loses exactly one file in production.

---

## 10. Files

| File | Purpose |
|---|---|
| `pipeline.py` | The nightly job |
| `reconcile.py` | Gap check and alert |
| `backfill.py` | One-off history, resumable |
| `provision.py` | Creates the library and columns |
| `sharepoint_site.py` | Resolves the site URL to a drive id |
| `build_team_map.py` | Phone handout → `teams.csv` |
| `team_sync.py` | Dates team changes the client made without dates |
| `config.py` | Everything tunable; the one file that switches dev→prod |
| `teams.csv` | email → team. Edited by hand; never written to |
| `teams_effective.csv` | Derived from the above, with dates. What the pipeline reads |
| `queue_teams.csv` | call queue → team, the fallback |
| `seal.py` | Weekly manifests — the pass the daily window cannot reach |
| `webhook.py` | Real-time receiver: verify, queue, ack in <3s, deliver |
| `team_source.py` | Fetches and vets `teams.csv` from SharePoint |
| `state.py` | Where derived state lives (`STATE_DIR`) |
| `run_daily.sh` | Cron wrapper with locking, log rotation and the stage switches |
| `meetings.py` | Meeting transcripts to SharePoint |
| `meeting.py` | `MeetingTranscript`, meeting layout and filenames |
| `meeting_manifest.py` | Weekly manifests for meeting transcripts |
| `vtt.py` | WebVTT → readable text, with speakers |
| `zoom_meetings.py` | Cloud recordings, filtered to the TRANSCRIPT file |
| `chat.py` (jobs) | Team Chat to SharePoint, including dual-team delivery |
| `chat.py` (core) | `ChatMessage`, `ChannelWeek`, layout and thread rendering |
| `chat_manifest.py` | Weekly manifests for chat records |
| `zoom_chat.py` | Channel resolution across users, message retrieval |
| `channel_map.py` | Channel → team(s), with the client's spellings normalised |
| `channel_source.py` | Fetches and vets `chat_channels.csv` from SharePoint |
| `chat_channels.csv` | Which channels to archive, and under which team(s) |
| `requirements.txt` | Pinned dependencies; what the platform installs |
| `RUNBOOK.md` | This file |
