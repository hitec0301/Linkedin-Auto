# LinkedIn publishing pipeline

A hosted, human-in-the-loop tool for publishing to LinkedIn three or four times
a week. Customers subscribe, connect their LinkedIn account, and spend about
ten minutes a week deciding what goes out.

**The human decides, the model drafts.** The system surfaces candidate source
material; the customer picks items and writes a one-line angle; the model
drafts from that angle; they approve, revise, or rewrite; the system publishes.
It never posts unattended, and there is no mode that makes it do so. If a draft
is wrong, the thing to change is the voice card, not the architecture.

The shipped voice and source list are written for an L&D leader in edtech
posting to corporate L&D practitioners and academic educators. Both are
per-account and fully editable, so that is a starting position rather than a
constraint.

---

## How it runs

A web service and four scheduled jobs, all from one image. Each job serves
every live account in turn.

```
JOB A  curate       Mon 12:00 UTC   feeds -> dedupe -> score -> 10 candidates
       [them: tick 3-4, write a one-line angle for each]        ~10 min/week
JOB B  draft        hourly          drafts ticked rows; regenerates rows sent back
       [them: edit their version, or send it back with a note, then approve]
JOB C  publish      every 30 min    posts approved rows whose slot is due
JOB D  voice_amend  Sun 15:00 UTC   proposes voice rules from their corrections
       [them: tick the ones they agree with; nothing else reaches the card]
```

Cron is UTC and does not follow daylight saving, so the local times drift by an
hour twice a year. Nothing depends on the exact minute.

### The status machine

```
NEW -> DRAFTED -> (REVISE -> DRAFTED)* -> APPROVED -> POSTING -> POSTED
                                                   \-> FAILED -> APPROVED

any -> SKIPPED ;  DRAFTED/APPROVED -> EXPIRED ;  POSTED, SKIPPED, EXPIRED are terminal
```

Every transition goes through `assert_transition()`, which raises on anything
not in that diagram, whichever interface asked. The web app derives the
buttons it offers from the same table, so a button only exists for a move the
machine actually has - the rule is written once and read twice, rather than
restated in the browser where the two copies can drift.

### What the jobs will not do

These are enforced in code and each has a test:

1. Job C acts only on `APPROVED`. Every other status is a no-op. There is no
   flag that changes this.
2. `POSTING` is written to the row before the HTTP call and `POSTED` after, so
   a crashed run cannot double-publish.
3. A row stuck in `POSTING` is never blindly retried. The Posts API is asked
   whether the post exists; if that cannot be answered, you are alerted and the
   row is left alone.
4. A row more than 48 hours past its `ScheduledFor` becomes `EXPIRED` and is
   never published. A four-day-old take is worse than no post.
5. Jobs never write `Selected`, `Angle`, `FinalText`, `RevisionNote` or `Reach`.
   Those columns belong to the customer; an attempt to write one raises. The
   single exception is clearing `RevisionNote` after a successful regenerate.
6. The reverse holds too: the web app cannot write `DraftText`. The difference
   between what the model wrote and what actually went out is the only learning
   signal the system has, so an edit goes to `FinalText` and the draft stays.
7. `PAUSED` stops Job C immediately, from the switch on the Setup screen.
8. A row approved with both drafts still in it is refused, not guessed at.
9. The drafting prompt forbids any number that is not in the fetched source
   extract.
10. Every job crash alerts, and names the account it concerns. A silent
    pipeline looks like a working one.
11. No query reaches data without a tenant id, and the tenant id comes from the
    session rather than from a parameter. Another account's row id reads
    exactly like one that does not exist.

---

## The customer's week

**Monday, ten minutes.** Job A has put ten candidates on the Review screen,
each with an audience tag, a theme tag, and a sentence on why it matters. They
tick three or four and write a one-line angle for each.

The angle is the whole system. It is their thesis, and the source article is
evidence for it. "Districts are buying AI tutoring seats faster than they can
staff the humans who supervise them" is an angle. "AI tutoring adoption is
growing" is a summary, and a summary is what comes back.

**Within the hour.** Job B drafts each ticked row and assigns a slot. For the
first twenty posts it produces two variants, so they can see the range.

**Then, per draft, one of four buttons:**

| They want | They press |
|---|---|
| It's good | **Approve for publishing** |
| Small fix | edit *your version*, then **Approve** |
| Structural fix | **Send back with a note** |
| It's wrong | **Skip** |

The draft is shown but not editable. Edits go in a separate field, and that
separation is the measurement: the distance between the two is what the health
metric and the weekly voice job are computed from.

If they were given two variants, one has to go before approving. A row approved
with both still in it is refused, not guessed at.

**Job C** posts approved rows when their slot arrives, and writes back the URN,
the timestamp, and the edit distance.

**Sunday.** Job D reads their corrections and proposes voice rules on the Voice
screen. They tick the ones they agree with; the next Sunday run writes those
into their card. Nothing edits the card without a tick.

**They should not wait for Sunday.** When a draft comes out wrong, the Voice
screen has the whole card in a text box. That is the fastest fix available, it
takes thirty seconds, and it takes effect on the next hourly draft run. Job D
exists to catch what they would not have thought to write down.

Impressions are theirs to fill in by hand. Automated engagement retrieval needs
`r_member_social`, a restricted permission this product deliberately does not
request.

---

## Is it working?

The system logs the normalised edit distance between what the model drafted and
what actually got published, per post, and reports:

- **clean-publish rate** — posts published with zero revisions and zero edits
- **mean edit distance**, first 10 posts versus last 10

If the clean-publish rate is below 50% after 30 days, the verdict says the
pipeline is costing more editing time than it saves and should be shut off.

That verdict is shown to the customer, on their Published screen, in those
words. A drafting tool someone rewrites every time is worse than a blank page,
because it anchors them to someone else's framing before they have thought
about their own — and the person paying for it is the one who most needs to be
told. A product that hides its own failure metric is a product that keeps
charging for something that stopped working.

---

## Runbook

### The kill switch

The switch on a customer's Setup screen, or `PAUSED` in their settings. Job C
checks it before anything else and exits without posting. If the settings are
unreadable, or the key is missing, the job treats itself as paused rather than
guessing: an unreachable stop button might be pressed.

### Failure mode 1: a customer's LinkedIn access expired

**Symptom.** Every publish run for that account fails with a 401, or the log
says `token refresh failed`. They will have had 30 days of warnings first.

**Why.** Refresh tokens last 365 days. Renewing one needs the customer in a
browser; there is no way to do it on their behalf, by design.

**Fix.** They open Setup and press **Authorise posting** again. Nothing on the
operator's side is involved, and no other account is affected.

Rows that expired while it was broken stay `EXPIRED`. That is correct: they
were stale before anyone got to them.

### Failure mode 2: a row stuck in POSTING

**Symptom.** A row sits at `POSTING` and you have an alert saying it could not
be verified.

**Why.** The job writes `POSTING`, calls LinkedIn, then writes `POSTED`. If it
dies between those, the row is left mid-flight. The next run does **not** retry
it — it asks the Posts API whether the post exists:

- confirmed present → the row becomes `POSTED` and the URN is recorded;
- confirmed absent → the row becomes `FAILED`, ready to be re-approved;
- **cannot tell** → the row is left untouched and a human is alerted.

The third case is the one that needs resolving, and it usually means the Posts
API refused the read.

**Fix.** Look at the profile. If the post is there, set the row to `POSTED` and
record the URN; if it is not, set it to `FAILED` so it can be re-approved.
Never move a stuck row straight back to `APPROVED` without checking — that is
the one action that double-posts, which is why the code will not do it either.

### Other things that happen

**A feed returns nothing.** The account's owner gets an alert naming the feed;
the Sources screen shows the error against it. They fix the URL or remove it.

**A row hits the revision cap.** After three revisions the row is `SKIPPED` and
they are told why. Three instructions that did not land almost always means the
angle is the problem, not the prose — a new angle on a fresh row beats a fourth
revision.

**The same correction keeps recurring.** If one instruction appears on three or
more different posts, Job D flags it `RECURRING`, sorts it to the top of the
Voice screen, and alerts. A rule that keeps being repeated is the clearest sign
the system is not learning.

**An account runs out of allowance.** Drafting stops for that account until the
next period. Approving and publishing what is already drafted are unaffected,
and they were warned at 80%.

---

## Layout

```
config/config.yaml      product tunables: quotas, character limits, thresholds
config/sources.yaml     the starter feed list every new account is seeded with
config/voice_card.md    the starter voice card, likewise

src/lnp/models.py       status machine, Row, column ownership, health metric
src/lnp/runner.py       one run per account: store, sources, card, tokens, meter
src/lnp/ingest.py       feeds, two-stage dedupe, education filtering
src/lnp/scoring.py      batched scoring, tier weights, quota enforcement
src/lnp/drafting.py     extraction, prompts, post-processing, revision
src/lnp/voice.py        the card, feedback signals, rule proposals
src/lnp/tokens.py       OAuth rotation, proactive refresh, expiry warnings
src/lnp/linkedin.py     Posts API, and the stuck-row recovery query
src/lnp/llm.py          Anthropic access, and the metering hook every call passes
src/lnp/alerts.py       Slack, SMTP, and always a log line

src/lnp/db/schema.py    the multi-tenant tables
src/lnp/db/store.py     one account's pipeline; the only module that writes SQL
src/lnp/db/crypto.py    encryption for stored credentials, as a column type
src/lnp/db/tokens.py    per-account LinkedIn app and tokens
src/lnp/db/usage.py     per-account metering and the cap
src/lnp/db/provision.py what a new account starts with

src/lnp/api/            FastAPI: sign-in, the pipeline, the account
web/src/                React: Review, Published, Voice, Sources, Setup
alembic/                migrations; the schema of record in production

jobs/                   the four scheduled entry points
scripts/gen_keys.py     the two secrets a deployment needs
scripts/serve.sh        migrate, then start the web service
tests/test_pipeline.py  every invariant above, with the network mocked
tests/test_store.py     the store guards, tenant isolation, the cap, the runner
tests/test_api.py       tenancy, the human-side guard, and the two OAuth flows
```

### Commands

```bash
pytest                                  # 179 tests, no network, no server
TEST_DATABASE_URL=postgresql://localhost/lnp_test pytest    # on real Postgres

alembic upgrade head                    # apply migrations
alembic revision --autogenerate -m "…"  # after editing schema.py
python scripts/gen_keys.py              # the two secrets, printed once

python jobs/curate.py                   # every live account
python jobs/curate.py --tenant 01J…     # one account
python jobs/curate.py --no-write        # score and print, write nothing
python jobs/draft.py --print            # draft and print, write nothing
python jobs/publish.py --dry-run        # log the payload, post nothing
python jobs/voice_amend.py --dry-run    # propose rules, write nothing
```

Every job takes `--tenant`, which is how you reproduce one customer's problem
without touching anybody else's account.

---

## Running it

### The architecture, in three paragraphs

`PipelineStore` (`src/lnp/db/store.py`) is everything the jobs and the API do
to one account's state, and the only module in the product that writes SQL.
Every query filters on `tenant_id`, and the tenant id comes from the store
object rather than from an argument, so no call site can forget which customer
it is serving.

Two rules live in its write path rather than at the call sites, because a call
site is a place somebody can forget. `write` refuses to touch a human's column;
`write_as_human` refuses to touch the model's. Both refuse before anything is
persisted, and `transition` checks the status machine on the same terms.

The one that matters most is `DraftText`. The difference between what the model
wrote and what actually went out is the only thing this system learns from, so
the customer's edits go in `FinalText` and the draft stays as written. That is
why the web app shows the draft read-only next to a field of their own, and why
"send it back with a note" is a separate action rather than a retype.

### The five services

One image, built once, from the same `Dockerfile`. The web service serves the
API and the built front end from the same origin, which is why the session
cookie can be `SameSite=Lax` and there is no CORS configuration to get wrong.

| Service | Start command | Schedule (UTC) |
|---|---|---|
| `web` | `sh scripts/serve.sh` | always on |
| `curate` | `python jobs/curate.py` | `0 12 * * 1` |
| `draft` | `python jobs/draft.py` | `0 * * * *` |
| `publish` | `python jobs/publish.py` | `*/30 * * * *` |
| `voice` | `python jobs/voice_amend.py` | `0 15 * * 0` |

Restart policy `NEVER` on the four cron services; a cron job that exits 0 has
finished. The web service restarts normally.

Each job iterates every account whose subscription is `trialing` or `active`.
One account's broken feed alerts and the loop moves on — the tenth customer
does not lose their week because the third one's source list rotted.

### Connecting the repo to Railway

Roughly twenty minutes, most of it waiting on builds.

**1. Make your sign-in LinkedIn app.** This one is yours, not a customer's, and
it only ever identifies people — it cannot post. At
[developer.linkedin.com](https://developer.linkedin.com/) create an app, and on
its **Products** tab request **Sign In with LinkedIn using OpenID Connect**.
Leave the Auth tab open; you come back to it in step 5.

**2. Generate the two secrets.**

```bash
python scripts/gen_keys.py
```

Keep that output. `LNP_ENCRYPTION_KEY` is not rotatable — changing it makes
every stored LinkedIn credential unreadable and every customer has to
reconnect. Back it up somewhere that is not the database it protects.

**3. Create the project and the database.** In Railway: **New Project → Deploy
from GitHub repo**, pick this repository, and let the first build run. Then
**New → Database → Add PostgreSQL** in the same project.

**4. Set the variables on the project**, not on a service, so all five share
them. Project → **Variables**:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — reference it, do not paste it |
| `LNP_SECRET_KEY` | from step 2 — signs session cookies |
| `LNP_ENCRYPTION_KEY` | from step 2 — encrypts stored credentials |
| `LNP_BASE_URL` | your public URL, no trailing slash (step 5) |
| `LNP_AUTH_CLIENT_ID` | the sign-in app from step 1 |
| `LNP_AUTH_CLIENT_SECRET` | the secret for that app |
| `ANTHROPIC_API_KEY` | yours: the operator pays for drafting |
| `SLACK_WEBHOOK_URL` | optional, and the only way you hear about a failed run |

Referencing `${{Postgres.DATABASE_URL}}` rather than copying the string means a
database that gets recreated does not leave five services pointing at a
hostname that no longer resolves. `postgres://` and `postgresql://` are both
accepted; the code rewrites either to the driver it actually uses.

**5. Give the web service a domain.** Service → Settings → Networking →
**Generate Domain**. Put that URL in `LNP_BASE_URL`, and add
`<that URL>/auth/linkedin/callback` to your sign-in app's **Authorized redirect
URLs** on LinkedIn. It has to match character for character.

**6. Add the four cron services.** Each is **New → GitHub Repo**, same
repository, then Settings → **Custom Start Command** and **Cron Schedule**:

| Service | Start command | Cron (UTC) | Restart policy |
|---|---|---|---|
| `curate` | `python jobs/curate.py` | `0 12 * * 1` | NEVER |
| `draft` | `python jobs/draft.py` | `0 * * * *` | NEVER |
| `publish` | `python jobs/publish.py` | `*/30 * * * *` | NEVER |
| `voice` | `python jobs/voice_amend.py` | `0 15 * * 0` | NEVER |

Restart policy **NEVER** on all four: a cron job that exits 0 has finished, and
restarting it runs it again immediately — on `publish` that is the one
behaviour you do not want. The web service keeps the default restart policy.

The build runs the test suite, so a broken commit fails at build time rather
than at 07:00 on a Monday with nobody watching.

### Testing it

**Locally first, without a database server.** SQLite is enough to exercise
everything except Postgres-specific behaviour, and the whole flow works:

```bash
export DATABASE_URL="sqlite:///$PWD/local.db"
eval "$(python scripts/gen_keys.py | grep '^LNP_' | sed 's/^/export /')"
export LNP_BASE_URL=http://localhost:8000 LNP_INSECURE_COOKIES=1
export LNP_AUTH_CLIENT_ID=... LNP_AUTH_CLIENT_SECRET=...   # your sign-in app

alembic upgrade head
uvicorn lnp.api.app:app --app-dir src --reload      # :8000
cd web && npm install && npm run dev                # :5173, proxies to :8000
```

`LNP_INSECURE_COOKIES=1` is only for plain-HTTP local runs; without it the
session cookie is `Secure` and a browser on `http://` will drop it. Never set it
on a deployment.

Signing in needs a LinkedIn app with
`http://localhost:8000/auth/linkedin/callback` registered as a redirect URL,
which LinkedIn does accept for localhost. Without `LNP_AUTH_CLIENT_ID` set,
`/auth/linkedin/start` answers **503** rather than failing obscurely — that is
the deployment saying sign-in is not configured, not a bug.

Every other screen works without it, and the test suite covers the OAuth paths
with no browser at all:

```bash
pytest                              # 218 tests, no network
pytest tests/test_api.py -v         # tenancy, the guards, both OAuth flows
TEST_DATABASE_URL=postgresql://localhost/lnp_test pytest   # against real Postgres
```

**Then on Railway,** in this order — each step is the precondition for the next,
so a failure tells you exactly which one broke:

1. `curl https://<your-domain>/healthz` → `{"ok":true}`. The image built, the
   process started, and migrations applied.
2. `curl -i https://<your-domain>/api/rows` → **401**. The API is refusing
   anonymous requests, which is the one failure mode worth checking by hand.
3. Open the domain in a browser and sign in with LinkedIn. A redirect back to
   the app means `LNP_BASE_URL` and the registered redirect URL agree; a
   LinkedIn error page means they do not.
4. You should land on **Setup** with the five-step wizard, because a new account
   has no posting grant yet. Check the Sources screen has feeds and the Voice
   screen has a card — that is provisioning having run.
5. Walk the wizard with a throwaway LinkedIn app of your own, as a customer
   would. It ends with **Authorise posting** and a green "Connected".
6. Run curate by hand rather than waiting until Monday: the `curate` service →
   **Deploy** (or `railway run python jobs/curate.py`). Candidates should appear
   on the Review screen within a minute.
7. Tick one, write an angle, and run `draft` the same way. A draft appears.
8. **Leave `publish.dry_run: true` in `config/config.yaml` for the first
   fortnight.** Approve a row and run `publish` by hand: it logs the exact
   payload it would send and posts nothing. Read those logs each morning, fix
   the voice card, and only then set `dry_run: false` and redeploy.

The kill switch works throughout, for each account, from the switch on their
Setup screen. Publishing stops within half an hour and everything else keeps
running.

### What I could not verify

I built and tested all of this, and ran the web service against a real database
to confirm migrations apply, the app serves, and the API refuses an
unauthenticated request. But I have no Railway account and no LinkedIn app, so
the dashboard steps and the two OAuth round-trips above come from how those
services document themselves rather than from me having clicked them. The shape
holds either way: one image, five services, one database, and two LinkedIn apps
that must never be confused.

### Migrations

Alembic owns the production schema, and `scripts/serve.sh` runs
`alembic upgrade head` before starting the web service, so a deploy carries its
own migration. A test asserts the models and the migrations still agree, which
is what catches a column added to a model and nowhere else.

```bash
alembic revision --autogenerate -m "what changed"   # after editing schema.py
alembic upgrade head
```

### Working on the front end

```bash
cd web && npm install && npm run dev     # localhost:5173, proxying to :8000
DATABASE_URL=... uvicorn lnp.api.app:app --app-dir src --reload
```

The dev server proxies `/api` and `/auth` to the API, so the session cookie is
first-party in development exactly as it is in production and nothing about
auth behaves differently between the two.

### What each customer has to do once

Five steps, in the order LinkedIn's own screens ask for them, with every value
shown ready to paste:

1. Create a LinkedIn app (it needs a company page — making one takes a minute).
2. Request the **Share on LinkedIn** and **Sign In with LinkedIn using OpenID
   Connect** products. Both are granted automatically.
3. Paste the Client ID and Secret into the setup screen.
4. Copy the redirect URL it gives them back into the app's Auth tab.
5. Authorise posting.

Their own app rather than one shared app, deliberately: LinkedIn's rate limits
and any suspension are per app. Sharing one would put every customer behind a
single ceiling and let one customer's behaviour take down everybody's posting.

### Per-account usage caps

The operator pays for inference, so every model call is checked against a
monthly token allowance before it is made and recorded after. Running out stops
drafting and nothing else — approving and publishing what is already drafted
are unaffected — and the customer is warned at 80% rather than discovering it
at zero. At real prompt sizes the measured cost is a little over a dollar per
active account per month.

### What is not built yet

Billing. `Tenant.status` and `plan` are read everywhere they need to be, so
connecting a payment provider means writing to those two fields on a webhook
and nothing else. There is no billing code to remove first, and no place where
a subscription state is inferred from something other than that field.

---

## Deliberately not built

Company-page posting. Image, video, or document posts. Comment or DM
automation. Any scraping of LinkedIn — everything goes through the official API.
Automated engagement retrieval. An autonomous mode.

The last one is not a backlog item. A pipeline that posts without a human in the
loop is a different product with a different risk profile, and this one is built
so that adding it would mean removing code rather than adding it.
