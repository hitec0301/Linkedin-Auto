"""Conformance tests for the storage port.

Every test in the parametrised section runs twice: once against the Sheet and
once against Postgres. That is the point of the file. A port whose two
implementations are only ever tested separately is not a port, it is two
modules that happen to share method names, and the first sign of the
difference is a customer's row going somewhere the tests never looked.

The database tests run on SQLite in-memory by default so the suite needs no
server. Set TEST_DATABASE_URL to run the same tests against real Postgres.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lnp.config import Config
from lnp.models import (
    COLUMNS,
    ColumnPermissionError,
    Row,
    Status,
    TransitionError,
)
from lnp.store import AmendmentRecord, FeedbackRecord, SheetsStore, StoreError
from lnp.util import iso, utcnow

from lnp.db import crypto
from lnp.db.schema import Base, Tenant, VoiceCard
from lnp.db.store import BY_COLUMN, FIELDS, PostgresStore, from_db, to_db

from lnp import sheets as sheets_mod
from lnp.sheets import AMENDMENT_COLUMNS, FEEDBACK_COLUMNS, Sheets

from test_pipeline import (  # the existing fake gspread
    SHEET_CONFIG,
    FakeSpreadsheet,
    FakeWorksheet,
)

TENANT = "01J000000000000000000000AA"
OTHER_TENANT = "01J000000000000000000000BB"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(crypto.ENV_KEY, crypto.generate_key())


def new_engine():
    url = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def make_pg_store(rows=(), paused="FALSE", tenant_id=TENANT):
    from sqlalchemy.orm import Session

    engine = new_engine()
    session = Session(engine)
    for tid in {tenant_id, OTHER_TENANT}:
        session.add(Tenant(id=tid, email=f"{tid}@example.test"))
    session.commit()
    store = PostgresStore(session, tenant_id, SHEET_CONFIG)
    store.set_config_value("PAUSED", paused)
    store.set_config_value("POST_COUNT", "3")
    if rows:
        store.append_rows([Row(**{c: getattr(r, c) for c in COLUMNS}) for r in rows])
    return store


def make_sheet_store(rows=(), paused="FALSE", tmp_path=None):
    """The Sheet with all five tabs, so the port's whole surface is exercised."""
    tabs = {
        "Pipeline": FakeWorksheet(COLUMNS, [r.to_values() for r in rows]),
        "Config": FakeWorksheet(
            ["Key", "Value", "Notes"],
            [["PAUSED", paused, ""], ["POST_COUNT", "3", ""]],
            title="Config",
        ),
        "History": FakeWorksheet(sheets_mod.HISTORY_COLUMNS, title="History"),
        "Feedback": FakeWorksheet(FEEDBACK_COLUMNS, title="Feedback"),
        "VoiceAmendments": FakeWorksheet(AMENDMENT_COLUMNS, title="VoiceAmendments"),
    }
    return SheetsStore(Sheets(FakeSpreadsheet(tabs), SHEET_CONFIG), SHEET_CONFIG)


BACKENDS = {"sheets": make_sheet_store, "postgres": make_pg_store}


@pytest.fixture(params=sorted(BACKENDS))
def make_store(request):
    """A factory for one backend, so each test builds the state it needs."""
    return BACKENDS[request.param]


# --------------------------------------------------------------------------
# The guarantees, on every backend
# --------------------------------------------------------------------------


def test_store_refuses_to_write_human_columns(make_store):
    store = make_store([Row(ID="01AAA", Status=Status.DRAFTED, DraftText="d")])
    row = store.pipeline_rows()[0]
    for column in ("Angle", "FinalText", "Selected", "RevisionNote", "Reach"):
        with pytest.raises(ColumnPermissionError):
            store.write(row, {column: "a job must never write this"})


def test_rejected_write_leaves_storage_untouched(make_store):
    store = make_store([Row(ID="01AAA", Status=Status.DRAFTED, DraftText="original")])
    row = store.pipeline_rows()[0]
    with pytest.raises(ColumnPermissionError):
        store.write(row, {"DraftText": "changed", "Angle": "and this"})
    assert store.pipeline_rows()[0].DraftText == "original"


def test_store_refuses_illegal_transitions(make_store):
    store = make_store([Row(ID="01AAA", Status=Status.DRAFTED, DraftText="d")])
    row = store.pipeline_rows()[0]
    with pytest.raises(TransitionError):
        store.transition(row, Status.POSTING)
    assert store.pipeline_rows()[0].Status == Status.DRAFTED


def test_illegal_transition_writes_nothing_at_all(make_store):
    """The other columns in the same call must not land either."""
    store = make_store([Row(ID="01AAA", Status=Status.DRAFTED, DraftText="d")])
    row = store.pipeline_rows()[0]
    with pytest.raises(TransitionError):
        store.transition(row, Status.POSTED, {"PostURN": "urn:li:share:1"})
    live = store.pipeline_rows()[0]
    assert live.Status == Status.DRAFTED
    assert live.PostURN == ""


def test_legal_transition_writes_both_status_and_fields(make_store):
    store = make_store([Row(ID="01AAA", Status=Status.APPROVED, DraftText="d")])
    row = store.pipeline_rows()[0]
    store.transition(row, Status.POSTING, {"Error": ""})
    assert store.pipeline_rows()[0].Status == Status.POSTING


def test_revision_note_is_writable_only_on_the_revision_path(make_store):
    store = make_store([Row(ID="01AAA", Status=Status.REVISE, RevisionNote="shorter")])
    row = store.pipeline_rows()[0]
    with pytest.raises(ColumnPermissionError):
        store.write(row, {"RevisionNote": ""})
    store.write(row, {"RevisionNote": ""}, allow_revision_note=True)
    assert store.pipeline_rows()[0].RevisionNote == ""


def test_missing_paused_key_reads_as_paused(make_store):
    store = make_store([])
    store.set_config_value("PAUSED", "")
    assert store.is_paused() is True


def test_paused_values_the_human_might_type(make_store):
    store = make_store([])
    for value in ("TRUE", "true", "yes", "1", "on"):
        store.set_config_value("PAUSED", value)
        assert store.is_paused() is True, value
    for value in ("FALSE", "false", "no", "0"):
        store.set_config_value("PAUSED", value)
        assert store.is_paused() is False, value


def test_post_count_round_trips(make_store):
    store = make_store([])
    assert store.post_count() == 3
    assert store.bump_post_count() == 4
    assert store.post_count() == 4


def test_expire_stale_only_touches_drafted_and_approved(make_store):
    old = iso(utcnow() - timedelta(hours=100))
    store = make_store([
        Row(ID="01AAA", Status=Status.DRAFTED, ScheduledFor=old),
        Row(ID="01BBB", Status=Status.APPROVED, ScheduledFor=old),
        Row(ID="01CCC", Status=Status.POSTED, ScheduledFor=old),
        Row(ID="01DDD", Status=Status.NEW, ScheduledFor=old),
    ])
    expired = store.expire_stale(store.pipeline_rows(), 48)
    assert {r.ID for r in expired} == {"01AAA", "01BBB"}
    by_id = {r.ID: r.Status for r in store.pipeline_rows()}
    assert by_id["01CCC"] == Status.POSTED
    assert by_id["01DDD"] == Status.NEW


def test_append_rows_stamps_created_at(make_store):
    store = make_store([])
    store.append_rows([Row(ID="01AAA", SourceURL="https://example.test/a")])
    assert store.pipeline_rows()[0].CreatedAt


def test_recent_index_returns_urls_and_titles(make_store):
    store = make_store([
        Row(ID="01AAA", SourceURL="https://example.test/a", SourceTitle="A",
            CreatedAt=iso()),
    ])
    urls, titles = store.recent_index(30)
    assert "https://example.test/a" in urls
    assert "A" in titles


def test_archive_removes_finished_rows_from_the_pipeline(make_store):
    old = iso(utcnow() - timedelta(days=200))
    store = make_store([
        Row(ID="01AAA", Status=Status.POSTED, CreatedAt=old,
            SourceURL="https://example.test/a", SourceTitle="A"),
        Row(ID="01BBB", Status=Status.DRAFTED, CreatedAt=old),
    ])
    assert store.archive_old_rows(90) == 1
    assert [r.ID for r in store.pipeline_rows()] == ["01BBB"]


def test_published_rows_are_only_posted_ones(make_store):
    store = make_store([
        Row(ID="01AAA", Status=Status.POSTED),
        Row(ID="01BBB", Status=Status.DRAFTED),
    ])
    assert [r.ID for r in store.published_rows()] == ["01AAA"]


def test_feedback_round_trips(make_store):
    store = make_store([])
    store.append_feedback([
        FeedbackRecord(id="01FFF", created_at=iso(), row_id="01AAA", signal="NOTE",
                       instruction="shorter", draft_text="d", final_text="f"),
    ])
    records = store.feedback_records()
    assert len(records) == 1
    assert records[0].instruction == "shorter"
    assert records[0].signal == "NOTE"
    assert records[0].ref  # a handle back to the record


def test_amendments_round_trip_and_report_pending(make_store):
    store = make_store([])
    store.append_amendments([
        AmendmentRecord(id="01AM1", created_at=iso(), rule="No em dashes",
                        rationale="edited out twice", signal="DIFF", occurrences=2,
                        recurring=True, accepted=True, source_row_ids=["01AAA", "01BBB"]),
        AmendmentRecord(id="01AM2", created_at=iso(), rule="Shorter openers",
                        signal="NOTE", occurrences=1, accepted=False),
    ])
    records = {r.rule: r for r in store.amendment_records()}
    assert records["No em dashes"].accepted is True
    assert records["No em dashes"].recurring is True
    assert records["No em dashes"].occurrences == 2
    assert records["No em dashes"].source_row_ids == ["01AAA", "01BBB"]
    assert records["Shorter openers"].accepted is False

    pending = store.pending_amendments()
    assert [r.rule for r in pending] == ["No em dashes"]

    store.mark_amendment_applied(pending[0], iso())
    assert store.pending_amendments() == []


def test_row_survives_a_round_trip_through_every_column(make_store):
    """A column that gets dropped in storage is a column the human loses."""
    original = Row(
        ID="01AAA", CreatedAt=iso(), SourceURL="https://example.test/a",
        SourceTitle="A title", Audience="AUD_CORPORATE", Theme="THM_AI",
        WhyItMatters="because", RelevanceScore="7.5", Selected="TRUE",
        Angle="an angle", DraftText="draft", FinalText="final",
        RevisionNote="note", RevisionCount="2", CharCount="123",
        Status=Status.DRAFTED, ScheduledFor=iso(), PostURN="urn:li:share:1",
        PostedAt=iso(), EditDistance="0.25", Reach="900", Error="none",
    )
    store = make_store([original])
    live = store.pipeline_rows()[0]
    for column in COLUMNS:
        assert getattr(live, column), f"{column} came back empty"
    assert live.relevance_score == 7.5
    assert live.is_selected is True
    assert live.revision_count == 2


# --------------------------------------------------------------------------
# Postgres-only: tenant isolation and the field map
# --------------------------------------------------------------------------


def test_field_map_covers_every_column_exactly_once():
    assert [c for c, _, _ in FIELDS] == COLUMNS
    assert len(BY_COLUMN) == len(COLUMNS)


def test_blank_is_stored_as_unset_not_zero():
    """A row nobody has scored is not a row that scored nothing."""
    assert to_db("", "float") is None
    assert to_db("", "int") is None
    assert to_db("", "ts") is None
    assert to_db("", "bool") is None
    assert to_db("0", "float") == 0.0
    assert from_db(None, "float") == ""
    assert from_db(0.0, "float") == "0"


def test_one_tenant_cannot_see_another_tenants_rows():
    store = make_pg_store([Row(ID="01AAA", Status=Status.DRAFTED)])
    other = PostgresStore(store.session, OTHER_TENANT, SHEET_CONFIG)
    assert other.pipeline_rows() == []
    assert other.config_values() == {}
    assert other.recent_index(365) == ([], [])


def test_writing_across_tenants_is_refused():
    store = make_pg_store([Row(ID="01AAA", Status=Status.DRAFTED, DraftText="d")])
    row = store.pipeline_rows()[0]
    other = PostgresStore(store.session, OTHER_TENANT, SHEET_CONFIG)
    with pytest.raises(StoreError):
        other.write(row, {"DraftText": "reaching into someone else's account"})
    assert store.pipeline_rows()[0].DraftText == "d"


def test_archive_keeps_the_row_for_dedupe():
    """Archived is not deleted: dedupe still has to see it."""
    old = iso(utcnow() - timedelta(days=200))
    store = make_pg_store([
        Row(ID="01AAA", Status=Status.POSTED, CreatedAt=old,
            SourceURL="https://example.test/a", SourceTitle="A"),
    ])
    store.archive_old_rows(90)
    assert store.pipeline_rows() == []
    urls, titles = store.recent_index(365)
    assert urls == ["https://example.test/a"]
    assert titles == ["A"]


def test_voice_card_round_trips_per_tenant():
    store = make_pg_store([])
    with pytest.raises(StoreError):
        store.load_voice_card()
    store.save_voice_card("# Voice\n\nWrite plainly.")
    assert "Write plainly" in store.load_voice_card()
    other = PostgresStore(store.session, OTHER_TENANT, SHEET_CONFIG)
    with pytest.raises(StoreError):
        other.load_voice_card()


def test_secrets_are_encrypted_at_rest():
    """A database dump must not be enough to post as somebody."""
    from sqlalchemy import text as sql_text

    from lnp.db.schema import LinkedInApp, LinkedInToken

    store = make_pg_store([])
    store.session.add(LinkedInApp(tenant_id=TENANT, client_id="772vtx01ov0awb",
                                  client_secret="WPL_AP1.EXAMPLE0000FAKE.aBcDeF=="))
    store.session.add(LinkedInToken(tenant_id=TENANT, access_token="AQV-not-real",
                                    refresh_token="AQW-not-real"))
    store.session.commit()
    store.session.expire_all()

    raw = store.session.execute(
        sql_text("SELECT client_secret FROM linkedin_apps")
    ).scalar_one()
    assert "WPL_AP1.EXAMPLE" not in raw
    raw_token = store.session.execute(
        sql_text("SELECT access_token FROM linkedin_tokens")
    ).scalar_one()
    assert "AQV-not-real" not in raw_token

    app = store.session.get(LinkedInApp, TENANT)
    assert app.client_secret == "WPL_AP1.EXAMPLE0000FAKE.aBcDeF=="
    assert store.session.get(LinkedInToken, TENANT).access_token == "AQV-not-real"


def test_wrong_encryption_key_is_an_error_not_garbage(monkeypatch):
    """Silently returning nonsense would look like a revoked token."""
    from lnp.db.schema import LinkedInToken

    store = make_pg_store([])
    store.session.add(LinkedInToken(tenant_id=TENANT, access_token="AQV-not-real"))
    store.session.commit()
    store.session.expire_all()

    monkeypatch.setenv(crypto.ENV_KEY, crypto.generate_key())
    with pytest.raises(crypto.CryptoError):
        _ = store.session.get(LinkedInToken, TENANT).access_token


def test_missing_encryption_key_names_the_fix(monkeypatch):
    monkeypatch.delenv(crypto.ENV_KEY, raising=False)
    with pytest.raises(crypto.CryptoError) as excinfo:
        crypto.encrypt("something")
    assert crypto.ENV_KEY in str(excinfo.value)


def test_database_url_shapes_hosts_hand_out():
    from lnp.db.session import normalize_url

    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


# --------------------------------------------------------------------------
# Usage metering: the operator is the one paying
# --------------------------------------------------------------------------


def make_meter(job="draft", cap=None, alerter=None):
    from lnp.db.usage import TenantMeter

    store = make_pg_store([])
    if cap is not None:
        store.tenant().monthly_token_cap = cap
        store.session.commit()
    return store, TenantMeter(store.session, TENANT, job, alerter=alerter)


def test_usage_is_recorded_per_call():
    from lnp.db.usage import summary
    from lnp.llm import Usage

    store, meter = make_meter()
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=1000, output_tokens=200))
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=500, output_tokens=100))
    used = summary(store.session, TENANT)
    assert used.input_tokens == 1500
    assert used.output_tokens == 300
    assert used.total_tokens == 1800
    assert used.cost_usd > 0


def test_cap_stops_further_calls():
    from lnp.llm import Usage, UsageCapExceeded

    store, meter = make_meter(cap=1000)
    meter.check()  # nothing used yet
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=900, output_tokens=200))
    with pytest.raises(UsageCapExceeded):
        meter.check()


def test_cap_message_says_what_happens_next():
    from lnp.llm import Usage, UsageCapExceeded

    store, meter = make_meter(cap=100)
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=200, output_tokens=0))
    with pytest.raises(UsageCapExceeded) as excinfo:
        meter.check()
    message = str(excinfo.value)
    assert "next month" in message
    assert "nothing already drafted or approved is lost" in message.lower()


def test_warning_fires_once_before_the_cap():
    from lnp.llm import Usage

    seen = []
    store, meter = make_meter(cap=1000, alerter=lambda title, body: seen.append(title))
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=850, output_tokens=0))
    meter.check()
    meter.check()
    assert len(seen) == 1


def test_usage_does_not_leak_between_tenants():
    from lnp.db.usage import TenantMeter, summary
    from lnp.llm import Usage

    store, meter = make_meter()
    meter.record(Usage(model="claude-sonnet-4-6", input_tokens=1000, output_tokens=100))
    other = TenantMeter(store.session, OTHER_TENANT, "draft")
    other.record(Usage(model="claude-sonnet-4-6", input_tokens=7, output_tokens=3))
    assert summary(store.session, TENANT).total_tokens == 1100
    assert summary(store.session, OTHER_TENANT).total_tokens == 10


def test_every_model_call_goes_through_the_meter():
    """A call site that skips metering is a call site the operator pays for."""
    from lnp import llm

    class Recorder:
        def __init__(self):
            self.checks = 0
            self.records = []

        def check(self):
            self.checks += 1

        def record(self, usage):
            self.records.append(usage)

    class FakeUsage:
        input_tokens = 11
        output_tokens = 22

    class FakeResponse:
        stop_reason = "end_turn"
        usage = FakeUsage()
        content = [type("Block", (), {"type": "text", "text": "hello"})()]

    class FakeMessages:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeAPI:
        messages = FakeMessages()

    recorder = Recorder()
    llm.set_meter(recorder)
    try:
        text = llm.complete(SHEET_CONFIG, system="s", user="u", api=FakeAPI())
    finally:
        llm.set_meter(None)
    assert text == "hello"
    assert recorder.checks == 1
    assert recorder.records[0].input_tokens == 11
    assert recorder.records[0].output_tokens == 22


def test_the_cap_refuses_the_call_rather_than_warning():
    from lnp import llm

    class Blocked:
        def check(self):
            raise llm.UsageCapExceeded("out of allowance")

        def record(self, usage):
            raise AssertionError("no call should have been made")

    llm.set_meter(Blocked())
    try:
        with pytest.raises(llm.UsageCapExceeded):
            llm.complete(SHEET_CONFIG, system="s", user="u", api=object())
    finally:
        llm.set_meter(None)


# --------------------------------------------------------------------------
# Per-tenant LinkedIn app and tokens
# --------------------------------------------------------------------------


def test_tokens_round_trip_through_the_database():
    from lnp.db.tokens import DbTokenBackend
    from lnp.tokens import TokenSet

    store = make_pg_store([])
    backend = DbTokenBackend(store.session, TENANT)
    assert backend.load() is None
    backend.save(TokenSet(access_token="AQV-x", refresh_token="AQW-x",
                          expires_at=iso(), person_urn="urn:li:person:abc"))
    loaded = backend.load()
    assert loaded.access_token == "AQV-x"
    assert loaded.person_urn == "urn:li:person:abc"


def test_disconnect_forgets_the_tokens():
    from lnp.db.tokens import DbTokenBackend
    from lnp.tokens import TokenSet

    store = make_pg_store([])
    backend = DbTokenBackend(store.session, TENANT)
    backend.save(TokenSet(access_token="AQV-x"))
    backend.forget()
    assert backend.load() is None


def test_refresh_uses_the_tenants_own_app_not_the_environment(monkeypatch):
    """One app per tenant is the whole reason the credentials are a parameter."""
    from lnp import tokens as tokens_mod
    from lnp.db.tokens import app_credentials, save_app_credentials
    from lnp.tokens import AppCredentials, TokenSet

    store = make_pg_store([])
    save_app_credentials(store.session, TENANT, "tenantclientid",
                         "WPL_AP1.EXAMPLE0000FAKE.aBcDeF==", "https://app.test/callback")
    app = app_credentials(store.session, TENANT)
    assert app.client_id == "tenantclientid"

    sent = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "new", "expires_in": 5184000}

    def fake_post(url, data=None, headers=None, timeout=None):
        sent.update(data)
        return FakeResponse()

    monkeypatch.setattr(tokens_mod.requests, "post", fake_post)
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "the-wrong-app")
    tokens_mod.refresh(TokenSet(refresh_token="r"), app)
    assert sent["client_id"] == "tenantclientid"


def test_a_tenant_without_an_app_is_told_what_to_do():
    from lnp.db.tokens import app_credentials
    from lnp.tokens import TokenError

    store = make_pg_store([])
    with pytest.raises(TokenError) as excinfo:
        app_credentials(store.session, TENANT)
    assert "developer.linkedin.com" in str(excinfo.value)


# --------------------------------------------------------------------------
# The runner: one job, many accounts
# --------------------------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch):
    """Point the runner at a throwaway database with two live tenants."""
    from lnp.db import session as session_mod
    from lnp.db.schema import Tenant

    engine = new_engine()
    session_mod.configure(engine)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

    with session_mod.session_scope() as session:
        session.add(Tenant(id=TENANT, email="one@example.test", status="active"))
        session.add(Tenant(id=OTHER_TENANT, email="two@example.test", status="active"))
    return engine


def test_hosted_mode_yields_one_run_per_account(hosted):
    from lnp import runner

    labels = [run.label for run in runner.runs("draft", SHEET_CONFIG)]
    assert labels == ["one@example.test", "two@example.test"]


def test_cancelled_accounts_are_not_run(hosted):
    from lnp import runner
    from lnp.db import session as session_mod
    from lnp.db.schema import Tenant

    with session_mod.session_scope() as session:
        session.get(Tenant, OTHER_TENANT).status = "canceled"

    labels = [run.label for run in runner.runs("draft", SHEET_CONFIG)]
    assert labels == ["one@example.test"]


def test_one_account_can_be_named(hosted):
    from lnp import runner

    labels = [run.label for run in runner.runs("draft", SHEET_CONFIG, OTHER_TENANT)]
    assert labels == ["two@example.test"]


def test_one_accounts_failure_does_not_stop_the_others(hosted):
    """The tenth customer must not lose their week because the third had a bad feed."""
    from lnp import runner

    served = []
    for run in runner.runs("draft", SHEET_CONFIG):
        with runner.isolated(run, "draft"):
            served.append(run.label)
            if run.label == "one@example.test":
                raise RuntimeError("this account's feed is broken")
    assert served == ["one@example.test", "two@example.test"]


def test_a_local_failure_still_stops_the_job(monkeypatch):
    """Locally there is nobody else to protect, and a non-zero exit is the signal."""
    from lnp import runner

    monkeypatch.delenv("DATABASE_URL", raising=False)
    run = runner.Run(config=SHEET_CONFIG, store=make_sheet_store())
    with pytest.raises(RuntimeError):
        with runner.isolated(run, "draft"):
            raise RuntimeError("broken")


def test_the_meter_follows_the_account_and_is_removed_after(hosted):
    from lnp import llm, runner

    seen = []
    for run in runner.runs("draft", SHEET_CONFIG):
        meter = llm.current_meter()
        assert meter is not None
        seen.append(meter.tenant_id)
    assert seen == [TENANT, OTHER_TENANT]
    assert llm.current_meter() is None


def test_the_meter_is_removed_even_when_a_tenant_fails(hosted):
    """A crashed run must not leave one account's meter on another's calls."""
    from lnp import llm, runner

    with pytest.raises(RuntimeError):
        for run in runner.runs("draft", SHEET_CONFIG):
            raise RuntimeError("something went wrong mid-tenant")
    assert llm.current_meter() is None


def test_a_run_reads_that_accounts_sources(hosted):
    from lnp import runner
    from lnp.db import session as session_mod
    from lnp.db.schema import Source

    with session_mod.session_scope() as session:
        session.add(Source(id="01S1", tenant_id=TENANT, name="Mine",
                           url="https://mine.test/feed", tier=1, audience="AUD_CORPORATE"))
        session.add(Source(id="01S2", tenant_id=OTHER_TENANT, name="Theirs",
                           url="https://theirs.test/feed", tier=3))
        session.add(Source(id="01S3", tenant_id=TENANT, name="Switched off",
                           url="https://off.test/feed", tier=2, active=False))

    by_label = {run.label: run.sources() for run in runner.runs("curate", SHEET_CONFIG)}
    assert [f["name"] for f in by_label["one@example.test"]] == ["Mine"]
    assert [f["name"] for f in by_label["two@example.test"]] == ["Theirs"]
    assert by_label["one@example.test"][0]["weight"] == 0.7


def test_a_run_reads_that_accounts_voice_card(hosted):
    from lnp import runner
    from lnp.db import session as session_mod
    from lnp.db.schema import VoiceCard

    with session_mod.session_scope() as session:
        session.add(VoiceCard(tenant_id=TENANT, content="# One's voice"))
        session.add(VoiceCard(tenant_id=OTHER_TENANT, content="# Two's voice"))

    cards = {run.label: run.voice_card() for run in runner.runs("draft", SHEET_CONFIG)}
    assert cards["one@example.test"] == "# One's voice"
    assert cards["two@example.test"] == "# Two's voice"


def test_alerts_name_the_account_by_email_not_by_token(hosted, monkeypatch):
    from lnp import runner

    seen = []
    monkeypatch.setattr("lnp.runner.alert",
                        lambda cfg, title, body="", **kw: seen.append(title))
    for run in runner.runs("draft", SHEET_CONFIG):
        run.alert("a feed returned nothing")
    assert seen == [
        "[one@example.test] a feed returned nothing",
        "[two@example.test] a feed returned nothing",
    ]


def test_the_voice_job_never_ticks_its_own_proposals(hosted):
    """The model does not get to accept its own instructions."""
    import jobs.voice_amend as voice_job
    from lnp import runner, voice
    from lnp.db import session as session_mod
    from lnp.db.schema import VoiceCard

    with session_mod.session_scope() as session:
        session.add(VoiceCard(
            tenant_id=TENANT,
            content=f"# Voice\n\n{voice.AMENDMENTS_BEGIN}\n{voice.AMENDMENTS_END}\n",
        ))

    run = next(runner.runs("voice_amend", SHEET_CONFIG, TENANT))
    proposal = voice.Proposal(rule="No em dashes", rationale="edited out twice",
                              signal="DIFF", occurrences=2, source_row_ids=["01AAA"])
    run.store.append_amendments([
        AmendmentRecord(rule=proposal.rule, rationale=proposal.rationale,
                        signal=proposal.signal, occurrences=proposal.occurrences,
                        accepted=False),
    ])
    assert run.store.pending_amendments() == []

    class Args:
        dry_run = False

    assert voice_job.apply_accepted(run.store, Args.dry_run) == []
    assert voice.AMENDMENTS_BEGIN in run.store.load_voice_card()
    assert "No em dashes" not in run.store.load_voice_card()


def test_an_accepted_rule_reaches_the_card_exactly_once(hosted):
    import jobs.voice_amend as voice_job
    from lnp import runner, voice
    from lnp.db import session as session_mod
    from lnp.db.schema import VoiceCard

    with session_mod.session_scope() as session:
        session.add(VoiceCard(
            tenant_id=TENANT,
            content=f"# Voice\n\n{voice.AMENDMENTS_BEGIN}\n{voice.AMENDMENTS_END}\n",
        ))

    run = next(runner.runs("voice_amend", SHEET_CONFIG, TENANT))
    run.store.append_amendments([
        AmendmentRecord(rule="No em dashes", signal="DIFF", accepted=True),
    ])
    assert voice_job.apply_accepted(run.store, False) == ["No em dashes"]
    card = run.store.load_voice_card()
    assert card.count("No em dashes") == 1
    # Nothing pending means a second run is a no-op, not a duplicate.
    assert voice_job.apply_accepted(run.store, False) == []
    assert run.store.load_voice_card().count("No em dashes") == 1
