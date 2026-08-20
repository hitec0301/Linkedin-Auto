# LinkedIn L&D publishing pipeline

A human-in-the-loop pipeline for publishing to LinkedIn three or four times a
week, for someone writing to corporate L&D practitioners and academic educators
at the same time.

**The human decides, the model drafts.** The system surfaces candidate source
material; you pick items and write a one-line angle; the model drafts from that
angle; you approve, revise, or rewrite; the system publishes. It never posts
unattended, and there is no mode that makes it do so. If you find yourself
wanting one, the thing to change is the voice card, not the architecture.

Your time cost is about ten minutes on Monday plus a few minutes per draft.

---

## How it runs

Four scheduled GitHub Actions jobs. No server.

```
JOB A  curate       Mon 12:00 UTC   feeds -> dedupe -> score -> 10 candidates into the Sheet
       [you: tick Selected on 3-4 rows, write a one-line Angle]     ~10 min/week
JOB B  draft        hourly          drafts selected rows; regenerates rows marked REVISE
       [you: edit FinalText, or write a RevisionNote, then set Status=APPROVED]
JOB C  publish      every 30 min    posts rows whose ScheduledFor is due
JOB D  voice_amend  Sun 15:00 UTC   proposes voice rules from your corrections; you tick to accept
```

Cron in GitHub Actions is UTC and does not follow daylight saving, so the local
times drift by an hour twice a year. Nothing depends on the exact minute.

### The status machine

```
NEW -> DRAFTED -> (REVISE -> DRAFTED)* -> APPROVED -> POSTING -> POSTED
                                                   \-> FAILED -> APPROVED

any -> SKIPPED ;  DRAFTED/APPROVED -> EXPIRED ;  POSTED, SKIPPED, EXPIRED are terminal
```

Every transition goes through `assert_transition()`, which raises on anything
not in that diagram. The Status column also carries Google Sheets data
validation, so a mistyped cell cannot inject a state the code has never heard
of.

### What the jobs will not do

These are enforced in code and each has a test:

1. Job C acts only on `APPROVED`. Every other status is a no-op. There is no
   flag that changes this.
2. `POSTING` is written to the Sheet before the HTTP call and `POSTED` after, so
   a crashed run cannot double-publish.
3. A row stuck in `POSTING` is never blindly retried. The Posts API is asked
   whether the post exists; if that cannot be answered, you are alerted and the
   row is left alone.
4. A row more than 48 hours past its `ScheduledFor` becomes `EXPIRED` and is
   never published. A four-day-old take is worse than no post.
5. Jobs never write `Selected`, `Angle`, `FinalText`, `RevisionNote` or `Reach`.
   Those columns are yours; an attempt to write one raises. The single exception
   is clearing `RevisionNote` after a successful regenerate.
6. `DraftText` is model output and is never overwritten with your edits. The
   difference between `DraftText` and what you published is the only learning
   signal the system has.
7. `PAUSED` in the Config tab stops Job C immediately.
8. A row approved with both drafts still in it is refused, not guessed at.
9. The drafting prompt forbids any number that is not in the fetched source
   extract.
10. Every job crash alerts. A silent pipeline looks like a working one.

---

## Setup

Roughly an hour, most of it waiting on LinkedIn's UI.

### 1. Install

```bash
git clone <this repo> && cd Linkedin-Auto
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest                      # should be green before you go further
```

### 2. Google Sheet

The pipeline reads and writes one Google Sheet. It does that as a **service
account** — a robot Google account with its own email address, which you invite
to your sheet exactly like you would invite a colleague. That is the whole
mental model: everything below is either creating that robot, or introducing it
to your sheet.

Budget fifteen minutes. Nothing here costs money.

#### 2a. Create the sheet and copy its id

Go to [sheets.new](https://sheets.new). That creates an empty sheet. Give it a
name — "LinkedIn pipeline" — so you can find it later.

Now look at the address bar. The URL is:

```
https://docs.google.com/spreadsheets/d/1a2B3cD4eFgHiJkLmNoPqRsTuVwXyZ_example/edit#gid=0
                                       └──────────── this part is the id ────┘
```

The id is the long string between `/d/` and `/edit`. It is around 44
characters, letters and numbers with the odd dash or underscore. Copy it
somewhere; you need it in step 2e.

#### 2b. Create a Google Cloud project

Go to [console.cloud.google.com](https://console.cloud.google.com). First time
in, it may ask you to accept terms and pick a country. There is no billing
setup and no credit card for what we are doing.

At the top of the page, next to the "Google Cloud" logo, is a project picker —
it either says "Select a project" or shows a project name. Click it, then click
**New Project** at the top right of the dialog.

Name it something like `linkedin-pipeline`. Leave "Location" as "No
organisation". Click **Create**, then wait a few seconds and make sure the
project picker at the top now shows your new project. If it still shows
something else, click the picker and select the one you just made — every step
after this applies to whichever project is selected.

#### 2c. Turn on the Sheets API

In the search bar at the top, type `Google Sheets API` and pick it out of the
results (it appears under "Marketplace" or "APIs & Services"). You will land on
a page with a blue **Enable** button. Click it. Wait for the page to reload
into the API's dashboard.

That is the only API this pipeline needs. It never touches Google Drive: the
sheet is opened directly by its id rather than searched for by name, so there
is no Drive permission to grant and no second API to enable.

#### 2d. Create the service account and download its key

In the left sidebar go to **APIs & Services** → **Credentials**. (No sidebar?
Click the hamburger menu at the top left.)

1. Click **+ Create Credentials** at the top, then **Service account**.
2. **Service account name**: `lnp-pipeline`. The account id fills itself in.
   Click **Create and Continue**.
3. **Grant this service account access to project** — *skip this*. Click
   **Continue**. This trips people up because it looks required. It is not, and
   it is genuinely not needed: those roles control access to Google Cloud
   resources, whereas access to your sheet comes from sharing the sheet in the
   next step. An account with no roles at all is exactly right here.
4. **Grant users access to this service account** — skip it too. Click **Done**.

You are back on the Credentials page with your service account listed under
"Service Accounts". Now get its key:

1. Click the service account's email to open it.
2. Open the **Keys** tab.
3. **Add Key** → **Create new key** → choose **JSON** → **Create**.

A `.json` file downloads immediately. **This file is a password.** Anyone
holding it can act as this account. It is shown to you exactly once — if you
lose it you delete the key and create another.

Move it into the repo's gitignored secrets folder:

```bash
mkdir -p .secrets
mv ~/Downloads/linkedin-pipeline-*.json .secrets/service-account.json
chmod 600 .secrets/service-account.json
```

(Adjust the download filename — it is your project name plus a random suffix.)
`.secrets/` is already in `.gitignore`, so it will not be committed.

#### 2e. Share the sheet with the service account

Open the JSON file and find the `client_email` line:

```bash
grep client_email .secrets/service-account.json
```

It looks like `lnp-pipeline@linkedin-pipeline-123456.iam.gserviceaccount.com`.

Now open your Google Sheet, click the green **Share** button at the top right,
paste that address in, and set the role to **Editor** — not Viewer, not
Commenter; the pipeline writes to the sheet. Untick **Notify people** (it is
not a real mailbox and the mail bounces). Click **Share**.

**This is the step people skip, and it is why the first run 403s.** Enabling
the API in step 2c grants nothing on its own — it only makes the API callable.
The sheet is still private until you share it, the same as any document.

#### 2f. Fill in `.env` and verify

Open `.env` and set two values:

```bash
GOOGLE_SA_JSON=.secrets/service-account.json
SHEET_ID=1a2B3cD4eFgHiJkLmNoPqRsTuVwXyZ_example
```

`GOOGLE_SA_JSON` takes either a path to that file (what you want locally) or
the entire JSON pasted on one line (what you will use for GitHub Actions
later). Both work; the code checks whether the value is a readable path first.

Check the wiring before changing anything:

```bash
python scripts/setup_sheet.py --check
```

It prints the service account address, the project, the sheet id, and the tabs
it can see — or it tells you which of the three usual problems you have. When
it says access is working, build the tabs:

```bash
python scripts/setup_sheet.py
```

That creates `Pipeline`, `History`, `Feedback`, `VoiceAmendments` and `Config`,
with headers, a frozen bold header row, the Status enum as a dropdown, a
`Selected` checkbox, per-status row colouring, and text wrapping on the long
columns. Refresh the sheet in your browser and you should see all five tabs.

It is idempotent — run it again any time the sheet looks wrong, and after any
upgrade that changes the columns. It repairs headers and formatting in place
and never deletes your rows.

#### If something fails

| What you see | What it means | Fix |
|---|---|---|
| `missing required secret GOOGLE_SA_JSON` | `.env` is missing or the line is not filled in | Check you are in the repo root and `.env` exists (`cp .env.example .env`) |
| `GOOGLE_SA_JSON is neither a readable file path nor valid JSON` | The path is wrong | `ls -l .secrets/` — the path in `.env` is relative to the repo root |
| `403 ... PERMISSION_DENIED` or `The caller does not have permission` | The sheet was never shared with the service account | Step 2e. Check you pasted the `client_email`, not your own address |
| `Google Sheets API has not been used in project ... or it is disabled` | Step 2c was skipped, or you enabled it on a different project | Enable the Sheets API on the project the key belongs to — the `project_id` in the JSON file |
| `404` / `Requested entity was not found` | `SHEET_ID` is wrong | It is only the part between `/d/` and `/edit`, not the whole URL |
| Newly enabled API still 403s | Enabling propagates for a minute or two | Wait sixty seconds and re-run `--check` |

Two failures that happen before the script reaches Google at all:

| What you see | What it means | Fix |
|---|---|---|
| `TypeError: Metaclasses with custom tp_new are not supported` from `yaml/_yaml` | A PyYAML built from source with an old Cython, which cannot import on Python 3.12. Usually a stale wheel in pip's cache | Upgrade pip first, then reinstall (below) |
| `ModuleNotFoundError: No module named 'yaml'` right after trying that fix | The reinstall uninstalled the old copy and then failed to install the new one | Same fix — it is safe to re-run |
| Any `pip` command tracebacks inside `pip/_vendor/packaging`, e.g. `InvalidVersion: Invalid version: '0.dev0'` | pip's own install is broken or half-upgraded, so it cannot install anything at all | Rebuild the venv (below). Do not try to fix pip with pip |
| `ModuleNotFoundError: No module named 'lnp'` | Run from the repo root, not from inside `scripts/` | `cd` to the repo root and use `python scripts/setup_sheet.py` |

For either of those two, upgrade pip before reinstalling. An old pip is the
root cause: it is worse at matching prebuilt wheels, so it falls back to
building from source.

```bash
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt
python -c "import yaml; print(yaml.__version__)"
```

Use `python -m pip` rather than bare `pip` so the install definitely lands in
the active venv. Avoid `--only-binary :all:` here: if it cannot match a wheel
it fails outright, and combined with `--force-reinstall` that removes the
working copy before discovering it has nothing to replace it with.

### Rebuilding the venv

The fix for anything environment-shaped — a broken pip, a C extension that will
not import, packages that never installed. It takes a minute and discards
nothing you care about: the venv holds no configuration, only downloaded
packages.

```bash
deactivate                     # ignore "command not found" if it is not active
rm -rf .venv
python3.12 -m venv --upgrade-deps .venv
source .venv/bin/activate
python -m pip install --no-cache-dir -r requirements.txt
pytest
```

`--upgrade-deps` gives the new venv a current pip and setuptools up front,
rather than the older pair bundled with your Python. That matters because a
broken pip cannot repair itself — `pip install --upgrade pip` needs a working
pip to run. Building a new venv sidesteps it entirely: the pip inside comes
from Python's own bundled wheel, not from the broken copy.

That creates the five tabs (`Pipeline`, `History`, `Feedback`,
`VoiceAmendments`, `Config`) with headers, a frozen bold header row, the Status
enum as data validation, a `Selected` checkbox, per-status row colouring, and
text wrapping. It is idempotent — run it again any time the Sheet looks wrong.

### 3. Feeds

```bash
python scripts/validate_sources.py
```

Every feed marked `verify: true` in `config/sources.yaml` is one whose URL has
not been confirmed against the live web. Run the validator, fix or disable
whatever fails, and drop the `verify` flag from the ones that work. A dead feed
left configured is worse than a removed one, because a quiet week and a broken
feed look identical from the outside.

Fosway and HolonIQ publish by newsletter rather than RSS. They are configured
with `ingest: gmail` and disabled. To use them, subscribe with a Gmail account,
label the messages, set `ingest.sources.gmail_label: true` in `config.yaml`, and
put `GMAIL_USER` / `GMAIL_APP_PASSWORD` (an app password, not your account
password) in the environment.

### 4. LinkedIn app

Do this by hand at [developer.linkedin.com](https://developer.linkedin.com).

1. **Create an app.** It must be linked to a Company Page even though you are
   only posting to your personal profile. A placeholder page you create yourself
   is fine — nothing is ever posted to it.
2. **Verify the app** from the page's Settings. LinkedIn emails a verification
   link to a page admin; the app does nothing until this is done.
3. **Products tab** — request these two self-serve products:
   - *Sign In with LinkedIn using OpenID Connect* (gives `openid`, `profile`)
   - *Share on LinkedIn* (gives `w_member_social`)

   They are granted automatically, usually within minutes.
4. **Do not apply for the Marketing Developer Platform.** Posting to your own
   profile does not need it, and the partner review queue runs for months.
5. **Auth tab** — add this exact redirect URL:
   `http://localhost:8765/callback`
6. Copy the Client ID and Client Secret into `.env`.

Then run the OAuth flow once, on your own machine:

```bash
python scripts/oauth_bootstrap.py
```

It opens a browser, catches the redirect on `localhost:8765`, validates the
`state` parameter, and writes the tokens to `.secrets/linkedin_tokens.json`
(chmod 600). If LinkedIn returns no refresh token it says so loudly — that means
a product is missing, and you should fix it and run the script again rather than
continuing.

### 5. Tokens in CI

Access tokens last 60 days; refresh tokens last 365. The pipeline refreshes
proactively 7 days before expiry, on every publish run, and persists the rotated
token.

A GitHub Actions run cannot write back to its own repository secrets, so
rotation in CI is persisted to a **private Gist**:

1. Create a private Gist containing one file, `linkedin_tokens.json`, with the
   contents of your local `.secrets/linkedin_tokens.json`.
2. Note the Gist id from its URL.
3. Create a fine-grained personal access token with **gist** read/write
   permission and nothing else.
4. Add `GIST_ID` and `GIST_TOKEN` as repository secrets.

The publish job switches to the Gist backend automatically when it detects
`GITHUB_ACTIONS` and a `GIST_ID`.

You will get an alert 30 days before the **refresh** token expires. That one
needs you at a browser — re-run `scripts/oauth_bootstrap.py` and update the
Gist. Ignoring it means the pipeline stops dead a month later.

### 6. Repository secrets

`ANTHROPIC_API_KEY`, `GOOGLE_SA_JSON` (paste the whole JSON blob),
`SHEET_ID`, `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `GIST_ID`,
`GIST_TOKEN`, and `SLACK_WEBHOOK_URL` (or the `SMTP_*` variables).

Nothing goes in the repo. `.gitignore` covers `.env`, `.secrets/`, and
service-account JSON.

### 7. Leave dry run on for two weeks

`config/config.yaml` ships with `publish.dry_run: true`. Job C logs the exact
payload it would send and posts nothing. Leave it that way for two weeks: read
what it would have published each morning and fix the voice card until you would
have been happy for those posts to go out. Then set it to `false`.

---

## Your week

**Monday, ten minutes.** Job A has put ten candidates in the Pipeline tab, each
with an audience tag, a theme tag, and a one-sentence note on why it matters.
Tick `Selected` on three or four. For each one, write a one-line `Angle`.

The angle is the whole system. It is your thesis, and the source article is
evidence for it. "Districts are buying AI tutoring seats faster than they can
staff the humans who supervise them" is an angle. "AI tutoring adoption is
growing" is a summary, and you will get a summary back.

**Within the hour.** Job B drafts each selected row and assigns a slot. For your
first twenty posts you get two variants, so you can see the range.

**Then, per draft, one of four things:**

| You want | Do this |
|---|---|
| It's good | Set `Status` to `APPROVED` |
| Small fix | Edit `FinalText`, then `APPROVED` |
| Structural fix | Write a `RevisionNote`, set `Status` to `REVISE` |
| It's wrong | Set `Status` to `SKIPPED` |

If you were given two variants, put the one you want in `FinalText` before
approving. A row approved with both still in it is refused, not guessed at.

`FinalText` always wins over `DraftText`. A `FinalText` cell containing only
whitespace falls through to the draft rather than publishing nothing.

**Job C** posts approved rows when their slot arrives, and writes back the URN,
the timestamp, and the edit distance.

**Sunday.** Job D reads your corrections and proposes voice rules into the
`VoiceAmendments` tab. Tick `Accepted` on the ones you agree with; the next
Sunday run writes them into `config/voice_card.md`. Nothing edits that file
without your tick.

**Do not wait for Sunday.** When a draft comes out wrong, open
`config/voice_card.md` and fix it yourself. It is a plain markdown file, it takes
thirty seconds, and it takes effect on the next hourly draft run. That is the
intended primary path; Job D exists to catch what you would not have thought to
write down.

`Reach` is yours to fill in by hand. Automated engagement retrieval needs
`r_member_social`, a restricted permission this app deliberately does not
request.

---

## Is it working?

The system logs the normalised edit distance between what the model drafted and
what you actually published, per post, and reports:

- **clean-publish rate** — posts published with zero revisions and zero edits
- **mean edit distance**, first 10 posts versus last 10

If the clean-publish rate is below 50% after 30 days, you get an alert saying
the pipeline is costing more editing time than it saves and should be shut off.
That message is deliberate. A drafting system you rewrite every time is worse
than a blank page, because it anchors you to someone else's framing before you
have thought about your own.

---

## Runbook

### The kill switch

Config tab, `PAUSED` cell, set to `TRUE`. Job C exits immediately without
posting. Editable from a phone in five seconds — it is the first thing the job
checks. If the Config tab or the `PAUSED` key is unreadable, the job treats
itself as paused rather than guessing.

### Failure mode 1: expired refresh token

**Symptom.** Every publish run fails with a 401, or the job's log says
`token refresh failed`. You will normally have had 30 days of alerts first.

**Why.** Refresh tokens last 365 days. Renewing one needs a human in a browser;
there is no way to automate it, by design.

**Fix.**

```bash
# on your own machine, not in CI
python scripts/oauth_bootstrap.py
cat .secrets/linkedin_tokens.json
```

Paste the contents into the private Gist referenced by `GIST_ID`, keeping the
filename `linkedin_tokens.json`. Then run the publish workflow manually with
dry-run on and check the log shows a valid payload.

Rows that expired while you were fixing it are `EXPIRED` and stay that way.
That is correct: they were stale before you got there.

### Failure mode 2: a row stuck in POSTING

**Symptom.** A row sits at `POSTING`. You have an alert saying it could not be
verified.

**Why.** The job writes `POSTING`, calls LinkedIn, then writes `POSTED`. If it
dies between those, the row is left mid-flight. The next run does **not** retry
it — it asks the Posts API whether the post exists:

- confirmed present → the row becomes `POSTED` and the URN is recorded;
- confirmed absent → the row becomes `FAILED`, ready for you to re-approve;
- **cannot tell** → the row is left untouched and you are alerted.

The third case is the one you have to resolve, and it usually means the Posts
API refused the read.

**Fix.** Open your LinkedIn profile and look.

- The post is there: set `Status` to `POSTED` and paste the URN into `PostURN`
  (it looks like `urn:li:share:7123...`, visible in the post's permalink).
- The post is not there: set `Status` to `FAILED`, then to `APPROVED`. The next
  run publishes it — if its slot has not gone stale.

Never set a stuck row straight back to `APPROVED` without checking. That is the
one action that double-posts, which is why the code will not do it either.

### Other things that happen

**A feed returns nothing.** You get an alert naming the feed. Run
`python scripts/validate_sources.py`, then fix the URL or set `enabled: false`.

**A row hits the revision cap.** After three revisions the row is `SKIPPED` and
you get an alert. Three instructions that did not land almost always means the
angle is the problem, not the prose. Write a new angle on a fresh row instead of
revising a fourth time.

**The same correction keeps recurring.** If one instruction appears on three or
more different posts, Job D flags it `RECURRING`, sorts it to the top of the
review queue, and alerts. Accept the rule, or write it into the voice card
yourself. A rule you keep repeating is the clearest sign the system is not
learning.

---

## Layout

```
config/config.yaml      all tunables: quotas, character limits, schedule, thresholds
config/sources.yaml     three tiers of feeds, with weights and verify flags
config/voice_card.md    the voice. Hand-editable. Edit this first when drafts are wrong.

src/lnp/models.py       status machine, Row, column ownership, health metric
src/lnp/sheets.py       the Sheet: every write goes through the two guards
src/lnp/ingest.py       feeds, two-stage dedupe, education filtering
src/lnp/scoring.py      batched scoring, tier weights, quota enforcement
src/lnp/drafting.py     extraction, prompts, post-processing, revision
src/lnp/voice.py        the card, feedback signals, rule proposals
src/lnp/tokens.py       OAuth storage, proactive refresh, rotation
src/lnp/linkedin.py     Posts API, and the stuck-row recovery query
src/lnp/alerts.py       Slack, SMTP, and always a log line

jobs/                   the four scheduled entry points
scripts/                one-time and diagnostic tooling
tests/test_pipeline.py  every invariant above, with the network mocked
```

### Local commands

```bash
python scripts/setup_sheet.py --check   # verify Google access, change nothing
python scripts/setup_sheet.py           # create or repair the Sheet
python scripts/validate_sources.py      # per-feed status, non-zero if any is dead
python scripts/oauth_bootstrap.py       # one-time LinkedIn OAuth

python jobs/curate.py --no-write        # score and print, write nothing
python jobs/draft.py --print            # draft and print, write nothing
python jobs/draft.py --row 01HZY...     # one row
python jobs/publish.py --dry-run        # log the payload, post nothing
python jobs/voice_amend.py --dry-run    # propose rules, write nothing

pytest
```

## Deliberately not built

Company-page posting. Image, video, or document posts. Comment or DM
automation. Any scraping of LinkedIn — everything goes through the official API.
Automated engagement retrieval. An autonomous mode.

The last one is not a backlog item. A pipeline that posts without a human in the
loop is a different product with a different risk profile, and this one is built
so that adding it would mean removing code rather than adding it.
