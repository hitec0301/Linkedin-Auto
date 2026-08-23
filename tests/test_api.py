"""Tests for the HTTP interface.

The rules the API has to keep are the same ones the pipeline has always kept,
now with a second writer in the system. Two in particular get tested hard:

  * the interface cannot write the model's columns, especially DraftText,
  * one signed-in account cannot reach another account's anything.

No network. The LinkedIn calls are faked at the two functions that make them.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import connect_linkedin, make_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lnp.db import crypto
from lnp.db import session as session_mod
from lnp.db.schema import Base, Source, Tenant, VoiceAmendment, VoiceCard
from lnp.db.store import PipelineStore
from lnp.models import Row, Status
from lnp.util import iso, utcnow

TENANT = "01J000000000000000000000AA"
OTHER = "01J000000000000000000000BB"

STARTER_VOICE_CARD = (
    Path(__file__).resolve().parents[1] / "config" / "voice_card.md"
).read_text()


@pytest.fixture
def api(monkeypatch):
    """An app on a throwaway database, with one signed-in account."""
    monkeypatch.setenv(crypto.ENV_KEY, crypto.generate_key())
    monkeypatch.setenv("LNP_SECRET_KEY", "test-secret-key-not-a-real-one-0123456789")
    monkeypatch.setenv("LNP_INSECURE_COOKIES", "1")
    monkeypatch.setenv("LNP_BASE_URL", "https://app.test")
    monkeypatch.setenv("LNP_AUTH_CLIENT_ID", "operatorapp")
    monkeypatch.setenv("LNP_AUTH_CLIENT_SECRET", "operator-secret")

    engine = make_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_mod.configure(engine)

    with session_mod.session_scope() as session:
        session.add(Tenant(id=TENANT, linkedin_sub="sub-one", email="one@example.test",
                           status="active"))
        session.add(Tenant(id=OTHER, linkedin_sub="sub-two", email="two@example.test",
                           status="active"))

    from lnp.api.app import create_app
    from lnp.api.security import SESSION_COOKIE, issue_session

    client = TestClient(create_app())
    client.cookies.set(SESSION_COOKIE, issue_session(TENANT))
    client.tenant_id = TENANT
    client.engine = engine
    return client


def store_for(tenant_id=TENANT):
    from sqlalchemy.orm import Session

    from lnp.config import Config

    return PipelineStore(Session(session_mod.engine()), tenant_id, Config({}))


def seed(rows, tenant_id=TENANT):
    store = store_for(tenant_id)
    store.append_rows(rows)
    return store


# --------------------------------------------------------------------------
# Who can see what
# --------------------------------------------------------------------------


def test_no_cookie_means_no_access(api):
    api.cookies.clear()
    assert api.get("/api/rows").status_code == 401
    assert api.get("/api/me").status_code == 401


def test_a_forged_cookie_is_not_a_session(api):
    api.cookies.set("lnp_session", "eyJ0IjoiMDFKMDAwIn0.forged.signature")
    assert api.get("/api/me").status_code == 401


def test_rows_are_only_this_accounts(api):
    seed([Row(ID="01MINE", Status=Status.DRAFTED, DraftText="mine")])
    seed([Row(ID="01THEIRS", Status=Status.DRAFTED, DraftText="theirs")], OTHER)
    ids = [r["id"] for r in api.get("/api/rows").json()]
    assert ids == ["01MINE"]


def test_another_accounts_row_is_not_found_rather_than_forbidden(api):
    """Not 403: whether that id exists is itself somebody else's business."""
    seed([Row(ID="01THEIRS", Status=Status.DRAFTED, DraftText="theirs")], OTHER)
    assert api.get("/api/rows/01THEIRS").status_code == 404
    assert api.patch("/api/rows/01THEIRS", json={"take": "mine now"}).status_code == 404
    assert api.put(
        "/api/rows/01THEIRS/status", json={"status": "APPROVED"}
    ).status_code == 404


def test_another_accounts_source_cannot_be_edited(api):
    with session_mod.session_scope() as session:
        session.add(Source(id="01SRC", tenant_id=OTHER, name="Theirs",
                           url="https://theirs.test/feed"))
    body = {"name": "Hijacked", "url": "https://evil.test/feed", "tier": 1}
    assert api.put("/api/sources/01SRC", json=body).status_code == 404
    assert api.delete("/api/sources/01SRC").status_code == 404


# --------------------------------------------------------------------------
# The human's columns, and only those
# --------------------------------------------------------------------------


def test_the_interface_cannot_write_the_draft(api):
    """The draft/final difference is the measurement. It must survive the UI."""
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="what the model wrote")])
    response = api.patch("/api/rows/01A", json={"draft_text": "rewritten by hand"})
    # The field is not in the schema at all, so it is ignored rather than
    # applied - and the draft is unchanged either way.
    assert response.status_code == 200
    assert response.json()["draft_text"] == "what the model wrote"


def test_edits_land_in_final_text(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="model text")])
    body = api.patch("/api/rows/01A", json={"final_text": "my text"}).json()
    assert body["final_text"] == "my text"
    assert body["draft_text"] == "model text"


def test_selecting_and_taking_a_row(api):
    seed([Row(ID="01A", Status=Status.NEW)])
    body = api.patch(
        "/api/rows/01A", json={"selected": True, "take": "what buyers miss"}
    ).json()
    assert body["selected"] is True
    assert body["take"] == "what buyers miss"


def test_take_lands_in_the_angle_before_a_draft_exists(api):
    seed([Row(ID="01A", Status=Status.NEW)])
    api.patch("/api/rows/01A", json={"take": "an angle"})
    row = store_for().pipeline_rows()[0]
    assert row.Angle == "an angle"
    assert row.RevisionNote == ""


def test_take_lands_in_the_revision_note_once_a_draft_exists(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="already drafted", Angle="original")])
    api.patch("/api/rows/01A", json={"take": "cut the closing question"})
    row = store_for().pipeline_rows()[0]
    assert row.RevisionNote == "cut the closing question"
    assert row.Angle == "original"  # the original angle is not overwritten


# --------------------------------------------------------------------------
# The decisions
# --------------------------------------------------------------------------


def test_setting_status_moves_the_row(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a real draft")])
    body = api.put("/api/rows/01A/status", json={"status": "approved"}).json()
    assert body["status"] == Status.APPROVED
    assert body["post_urn"] == ""     # nothing was published by this call


def test_status_can_be_set_freely_even_outside_the_state_machine(api):
    """The user's call, not the state machine's - a NEW row can jump straight
    to Approved, and a POSTED row can be walked back to Drafted."""
    seed([
        Row(ID="01A", Status=Status.NEW),
        Row(ID="01B", Status=Status.POSTED, DraftText="already out", PostedAt=iso()),
    ])
    assert api.put("/api/rows/01A/status", json={"status": "APPROVED"}).json()["status"] == "APPROVED"
    assert api.put("/api/rows/01B/status", json={"status": "DRAFTED"}).json()["status"] == "DRAFTED"


def test_status_refuses_posting(api):
    """POSTING is a marker the publish path sets itself, not a choice a person makes."""
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    response = api.put("/api/rows/01A/status", json={"status": "POSTING"})
    assert response.status_code == 400


def test_status_refuses_an_unknown_value(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    response = api.put("/api/rows/01A/status", json={"status": "NOT_A_REAL_STATUS"})
    assert response.status_code == 422


def test_bulk_skip_removes_several_rows_at_once(api):
    seed([
        Row(ID="01A", Status=Status.NEW),
        Row(ID="01B", Status=Status.DRAFTED, DraftText="d"),
        Row(ID="01C", Status=Status.NEW),
    ])
    body = api.post("/api/rows/bulk-skip", json={"ids": ["01A", "01B"]}).json()
    assert set(body["removed"]) == {"01A", "01B"}
    statuses = {r["id"]: r["status"] for r in api.get("/api/rows").json()}
    assert statuses["01A"] == Status.SKIPPED
    assert statuses["01B"] == Status.SKIPPED
    assert statuses["01C"] == Status.NEW


def test_bulk_skip_does_not_touch_a_row_mid_publish(api):
    seed([Row(ID="01A", Status=Status.POSTING, DraftText="d")])
    body = api.post("/api/rows/bulk-skip", json={"ids": ["01A"]}).json()
    assert body["removed"] == []
    assert api.get("/api/rows/01A").json()["status"] == Status.POSTING


def test_bulk_skip_does_not_reach_another_tenants_rows(api):
    seed([Row(ID="01THEIRS", Status=Status.NEW)], tenant_id=OTHER)
    body = api.post("/api/rows/bulk-skip", json={"ids": ["01THEIRS"]}).json()
    assert body["removed"] == []
    assert store_for(OTHER).pipeline_rows()[0].status == Status.NEW


def test_rows_can_be_filtered_by_status(api):
    seed([
        Row(ID="01A", Status=Status.DRAFTED, DraftText="d"),
        Row(ID="01B", Status=Status.POSTED, DraftText="p", PostedAt=iso()),
    ])
    ids = [r["id"] for r in api.get("/api/rows?status_filter=DRAFTED").json()]
    assert ids == ["01A"]


# --------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------


def test_an_account_starts_paused(api):
    """No PAUSED value means paused. A new account cannot post by accident."""
    assert api.get("/api/me").json()["paused"] is True


def test_the_kill_switch_can_be_set_and_cleared(api):
    assert api.put("/api/pause", json={"paused": False}).json()["paused"] is False
    assert api.get("/api/me").json()["paused"] is False
    assert api.put("/api/pause", json={"paused": True}).json()["paused"] is True
    assert api.get("/api/me").json()["paused"] is True


def test_a_lapsed_account_can_still_pause_and_still_read(api):
    with session_mod.session_scope() as session:
        session.get(Tenant, TENANT).status = "canceled"
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="theirs to keep")])

    assert api.get("/api/rows").status_code == 200
    assert api.put("/api/pause", json={"paused": True}).status_code == 200
    # But not change anything about a post.
    assert api.put("/api/rows/01A/status", json={"status": "APPROVED"}).status_code == 402


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def test_the_voice_card_is_editable_in_full(api):
    assert api.get("/api/voice-card").status_code == 404
    api.put("/api/voice-card", json={"content": "# Voice\n\nWrite plainly."})
    assert "Write plainly" in api.get("/api/voice-card").json()["content"]


def test_accepting_an_amendment_writes_it_into_the_card_immediately(api):
    """No weekly job left to do it later - accepting is the only gate now."""
    api.put("/api/voice-card", json={"content": "# Voice\n\n<!-- AMENDMENTS-BEGIN -->\n<!-- AMENDMENTS-END -->"})
    with session_mod.session_scope() as session:
        session.add(VoiceAmendment(id="01AM", tenant_id=TENANT, rule="No em dashes",
                                   signal="DIFF", source_row_ids=[]))

    body = api.put("/api/amendments/01AM", json={"accepted": True}).json()
    assert body["accepted"] is True
    assert body["applied"] != ""
    assert "No em dashes" in api.get("/api/voice-card").json()["content"]


def test_unticking_an_amendment_does_not_touch_the_card(api):
    api.put("/api/voice-card", json={"content": "# Voice\n\n<!-- AMENDMENTS-BEGIN -->\n<!-- AMENDMENTS-END -->"})
    with session_mod.session_scope() as session:
        session.add(VoiceAmendment(id="01AM", tenant_id=TENANT, rule="No em dashes",
                                   signal="DIFF", source_row_ids=[]))

    body = api.put("/api/amendments/01AM", json={"accepted": False}).json()
    assert body["accepted"] is False
    assert body["applied"] == ""
    assert "No em dashes" not in api.get("/api/voice-card").json()["content"]


def test_an_amendment_already_in_the_card_cannot_be_reticked(api):
    with session_mod.session_scope() as session:
        session.add(VoiceAmendment(id="01AM", tenant_id=TENANT, rule="No em dashes",
                                   accepted=True, applied=iso(), source_row_ids=[]))
    assert api.put("/api/amendments/01AM", json={"accepted": False}).status_code == 409


def test_another_accounts_amendment_is_not_found(api):
    with session_mod.session_scope() as session:
        session.add(VoiceAmendment(id="01AM", tenant_id=OTHER, rule="Theirs",
                                   source_row_ids=[]))
    assert api.put("/api/amendments/01AM", json={"accepted": True}).status_code == 404


# --------------------------------------------------------------------------
# Sources, usage, health
# --------------------------------------------------------------------------


def test_sources_round_trip(api):
    created = api.post("/api/sources", json={
        "name": "Chief Learning Officer", "url": "https://clomedia.com/feed",
        "tier": 1, "audience": "AUD_CORPORATE",
    }).json()
    assert created["tier"] == 1
    listed = api.get("/api/sources").json()
    assert [s["name"] for s in listed] == ["Chief Learning Officer"]
    api.delete(f"/api/sources/{created['id']}")
    assert api.get("/api/sources").json() == []


def test_usage_is_reported_to_the_person_paying_indirectly(api):
    from lnp.db.usage import TenantMeter
    from lnp.llm import Usage

    with session_mod.session_scope() as session:
        TenantMeter(session, TENANT, "draft").record(
            Usage(model="claude-sonnet-4-6", input_tokens=1000, output_tokens=200)
        )
    body = api.get("/api/usage").json()
    assert body["total_tokens"] == 1200
    assert body["cap"] > 0
    assert 0 < body["fraction_used"] < 1


def test_the_health_verdict_reaches_the_customer(api):
    """Including the unflattering one. They are paying for this."""
    old = iso(utcnow() - timedelta(days=60))
    seed([
        Row(ID=f"01P{i}", Status=Status.POSTED, PostedAt=old, CreatedAt=old,
            DraftText="the model's version of the post",
            FinalText="a completely different post written by the human")
        for i in range(5)
    ])
    body = api.get("/api/health-metric").json()
    assert body["published"] == 5
    assert body["clean_rate"] == 0.0
    assert "shut it off" in body["verdict"]


# --------------------------------------------------------------------------
# Sign-in and connecting
# --------------------------------------------------------------------------


def test_signing_in_creates_an_account_and_a_session(api, monkeypatch):
    from lnp.api import routes_auth
    from lnp.tokens import TokenSet

    monkeypatch.setattr(routes_auth, "exchange_code",
                        lambda code, uri, app=None: TokenSet(access_token="signin-token"))
    monkeypatch.setattr(routes_auth, "userinfo", lambda token: {
        "sub": "sub-new", "email": "new@example.test", "name": "New Person",
    })

    api.cookies.clear()
    start = api.get("/auth/linkedin/start", follow_redirects=False)
    assert start.status_code in (302, 303, 307)
    assert "openid%20profile%20email" in start.headers["location"]
    assert "w_member_social" not in start.headers["location"]

    state_cookie = start.cookies.get("lnp_oauth_state")
    api.cookies.set("lnp_oauth_state", state_cookie)
    # The state that comes back must be the one we issued.
    import lnp.api.security as security_mod
    issued = security_mod.serializer("oauth-state").loads(state_cookie)["v"]

    done = api.get(
        f"/auth/linkedin/callback?code=abc&state={issued}", follow_redirects=False
    )
    assert done.status_code == 303
    assert api.get("/api/me").json()["email"] == "new@example.test"


def test_a_mismatched_state_is_refused(api, monkeypatch):
    from lnp.api import routes_auth

    monkeypatch.setattr(routes_auth, "exchange_code",
                        lambda *a, **kw: pytest.fail("must not exchange a bad state"))
    start = api.get("/auth/linkedin/start", follow_redirects=False)
    api.cookies.set("lnp_oauth_state", start.cookies.get("lnp_oauth_state"))
    response = api.get(
        "/auth/linkedin/callback?code=abc&state=not-the-state-we-issued",
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_connecting_uses_the_tenants_own_app_and_asks_to_post(api, monkeypatch):
    from lnp.api import routes_auth
    from lnp.tokens import TokenSet

    api.put("/auth/linkedin/app", json={
        "client_id": "tenantclientid", "client_secret": "WPL_AP1.EXAMPLE0000FAKE.aBcDeF==",
    })
    start = api.get("/auth/linkedin/connect", follow_redirects=False)
    assert "client_id=tenantclientid" in start.headers["location"]
    assert "w_member_social" in start.headers["location"]

    monkeypatch.setattr(routes_auth, "exchange_code",
                        lambda code, uri, app=None: TokenSet(access_token="posting-token",
                                                             refresh_token="r"))
    monkeypatch.setattr(routes_auth, "userinfo", lambda token: {"sub": "member-abc"})

    import lnp.api.security as security_mod
    state_cookie = start.cookies.get("lnp_oauth_state")
    api.cookies.set("lnp_oauth_state", state_cookie)
    issued = security_mod.serializer("oauth-state").loads(state_cookie)["v"]

    done = api.get(
        f"/auth/linkedin/connect/callback?code=abc&state={issued}", follow_redirects=False
    )
    assert done.status_code == 303
    me = api.get("/api/me").json()
    assert me["linkedin_connected"] is True
    assert me["linkedin_app_configured"] is True


def test_signing_in_does_not_grant_the_ability_to_post(api):
    """Creating an account and handing over your identity are separate acts."""
    assert api.get("/api/me").json()["linkedin_connected"] is False


def test_disconnecting_keeps_the_drafts(api, monkeypatch):
    from lnp.db.tokens import DbTokenBackend
    from lnp.tokens import TokenSet

    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="mine")])
    with session_mod.session_scope() as session:
        DbTokenBackend(session, TENANT).save(TokenSet(access_token="posting-token"))
    assert api.get("/api/me").json()["linkedin_connected"] is True

    api.delete("/auth/linkedin/connect")
    assert api.get("/api/me").json()["linkedin_connected"] is False
    assert [r["id"] for r in api.get("/api/rows").json()] == ["01A"]


def test_a_client_secret_never_comes_back_out(api):
    api.put("/auth/linkedin/app", json={
        "client_id": "tenantclientid", "client_secret": "WPL_AP1.EXAMPLE0000FAKE.aBcDeF==",
    })
    for path in ("/api/me", "/api/usage", "/api/sources"):
        assert "WPL_AP1" not in api.get(path).text


def test_signing_out_clears_the_session(api):
    assert api.get("/api/me").status_code == 200
    api.post("/auth/signout")
    api.cookies.clear()
    assert api.get("/api/me").status_code == 401


# --------------------------------------------------------------------------
# What a new account starts with
# --------------------------------------------------------------------------


def test_a_new_account_starts_ready_to_use(api, monkeypatch):
    from lnp.api import routes_auth
    from lnp.tokens import TokenSet

    monkeypatch.setattr(routes_auth, "exchange_code",
                        lambda code, uri, app=None: TokenSet(access_token="signin-token"))
    monkeypatch.setattr(routes_auth, "userinfo",
                        lambda token: {"sub": "sub-new", "email": "new@example.test"})

    api.cookies.clear()
    start = api.get("/auth/linkedin/start", follow_redirects=False)
    state_cookie = start.cookies.get("lnp_oauth_state")
    api.cookies.set("lnp_oauth_state", state_cookie)
    import lnp.api.security as security_mod
    issued = security_mod.serializer("oauth-state").loads(state_cookie)["v"]
    api.get(f"/auth/linkedin/callback?code=abc&state={issued}", follow_redirects=False)

    # A card to draft from and feeds to read: without both, the first run of
    # every job fails and the customer sees an error, not a product.
    assert "Voice card" in api.get("/api/voice-card").json()["content"]
    assert len(api.get("/api/sources").json()) > 0
    # And it cannot post until they say so.
    assert api.get("/api/me").json()["paused"] is True


def test_provisioning_twice_does_not_duplicate_anything(api):
    from lnp.db.provision import provision_tenant
    from lnp.db.schema import Tenant

    with session_mod.session_scope() as session:
        tenant = session.get(Tenant, TENANT)
        provision_tenant(session, tenant)
        first = len(api.get("/api/sources").json())
        provision_tenant(session, tenant)
    assert len(api.get("/api/sources").json()) == first


def test_a_seeded_card_carries_the_amendment_markers(api):
    """Without them the weekly job has nowhere to put an accepted rule."""
    from lnp.db.provision import starter_card
    from lnp.voice import AMENDMENTS_BEGIN, AMENDMENTS_END

    card = starter_card()
    assert AMENDMENTS_BEGIN in card
    assert AMENDMENTS_END in card


def test_the_shipped_schema_matches_the_migrations(tmp_path, monkeypatch):
    """A model changed without a migration is a production table that is wrong.

    Runs the real migrations through the real env.py against an empty database
    and asks Alembic whether anything is still missing.
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config as AlembicConfig
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from lnp.db.schema import Base

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    # Index shape differs harmlessly between SQLite and the models; a missing
    # or extra table or column does not.
    real = [d for d in diff if d[0] not in ("add_index", "remove_index")]
    assert real == [], f"the models and the migrations disagree: {real}"


# --------------------------------------------------------------------------
# On-demand curate: the Setup screen's "fetch candidates now"
# --------------------------------------------------------------------------


def test_curate_now_writes_rows_when_review_is_empty(api, monkeypatch):
    """A brand-new account can get its first batch without waiting for Monday."""
    from lnp import ingest as ingest_mod
    from lnp.ingest import Candidate, FeedResult

    with session_mod.session_scope() as session:
        session.add(Source(id="01SRCNOW", tenant_id=TENANT, name="Feed",
                            url="https://feed.test/rss", tier=2,
                            audience="AUD_CORPORATE", active=True))

    def fake_fetch(feed, timeout, user_agent):
        items = [
            Candidate(url="https://feed.test/a", title="A district buys AI seats",
                      summary="summary", source_name=feed["name"],
                      tier=feed["tier"], weight=feed["weight"]),
        ]
        return FeedResult(feed["name"], feed["url"], feed["tier"], True, len(items), "", items)

    def fake_scorer(config, *, system, user, model=None, max_tokens=0):
        import json
        items = json.loads(user.split("Items to triage:\n", 1)[1])
        return [
            {"i": item["i"], "score": 8, "audience": "AUD_CORPORATE",
             "theme": "THM_AI", "why": "Buyers will feel this before vendors do."}
            for item in items
        ]

    monkeypatch.setattr(ingest_mod, "fetch_feed", fake_fetch)
    monkeypatch.setattr("lnp.scoring.complete_json", fake_scorer)

    response = api.post("/api/rows/curate-now")
    assert response.status_code == 200
    assert response.json()["written"] == 1

    rows = api.get("/api/rows").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "NEW"


def test_curate_now_adds_to_an_already_populated_review(api, monkeypatch):
    """Fetching more is not gated on Review being empty - dedupe does the work."""
    from lnp import ingest as ingest_mod
    from lnp.ingest import Candidate, FeedResult

    seed([Row(ID="01ALREADYNEW", Status=Status.NEW, SourceURL="https://feed.test/existing")])

    with session_mod.session_scope() as session:
        session.add(Source(id="01SRCMORE", tenant_id=TENANT, name="Feed",
                            url="https://feed.test/rss", tier=2,
                            audience="AUD_CORPORATE", active=True))

    def fake_fetch(feed, timeout, user_agent):
        items = [
            Candidate(url="https://feed.test/a", title="A district buys AI seats",
                      summary="summary", source_name=feed["name"],
                      tier=feed["tier"], weight=feed["weight"]),
        ]
        return FeedResult(feed["name"], feed["url"], feed["tier"], True, len(items), "", items)

    def fake_scorer(config, *, system, user, model=None, max_tokens=0):
        import json
        items = json.loads(user.split("Items to triage:\n", 1)[1])
        return [
            {"i": item["i"], "score": 8, "audience": "AUD_CORPORATE",
             "theme": "THM_AI", "why": "Buyers will feel this before vendors do."}
            for item in items
        ]

    monkeypatch.setattr(ingest_mod, "fetch_feed", fake_fetch)
    monkeypatch.setattr("lnp.scoring.complete_json", fake_scorer)

    response = api.post("/api/rows/curate-now")
    assert response.status_code == 200
    assert response.json()["written"] == 1

    rows = api.get("/api/rows").json()
    assert len(rows) == 2


def test_curate_now_respects_the_cooldown(api):
    """A batch that just ran cannot be re-triggered a second later."""
    store_for().set_config_value("LAST_CURATE", iso())
    response = api.post("/api/rows/curate-now")
    assert response.status_code == 429


def test_curate_now_does_not_reach_another_tenants_rows(api, monkeypatch):
    """Triggered from one account's session, it only ever touches that account."""
    from lnp import ingest as ingest_mod
    from lnp.ingest import Candidate, FeedResult

    with session_mod.session_scope() as session:
        session.add(Source(id="01SRCOTHER", tenant_id=TENANT, name="Feed",
                            url="https://feed.test/rss", tier=2,
                            audience="AUD_CORPORATE", active=True))
    seed([Row(ID="01OTHERROW", Status=Status.NEW)], tenant_id=OTHER)

    def fake_fetch(feed, timeout, user_agent):
        items = [
            Candidate(url="https://feed.test/a", title="A district buys AI seats",
                      summary="summary", source_name=feed["name"],
                      tier=feed["tier"], weight=feed["weight"]),
        ]
        return FeedResult(feed["name"], feed["url"], feed["tier"], True, len(items), "", items)

    def fake_scorer(config, *, system, user, model=None, max_tokens=0):
        import json
        items = json.loads(user.split("Items to triage:\n", 1)[1])
        return [
            {"i": item["i"], "score": 8, "audience": "AUD_CORPORATE",
             "theme": "THM_AI", "why": "Buyers will feel this before vendors do."}
            for item in items
        ]

    monkeypatch.setattr(ingest_mod, "fetch_feed", fake_fetch)
    monkeypatch.setattr("lnp.scoring.complete_json", fake_scorer)

    response = api.post("/api/rows/curate-now")
    assert response.status_code == 200

    other_rows = store_for(OTHER).pipeline_rows()
    assert [r.ID for r in other_rows] == ["01OTHERROW"]


# --------------------------------------------------------------------------
# Redraft with AI: one button, reachable from any row, at any stage
# --------------------------------------------------------------------------


def test_redraft_now_drafts_a_new_row_using_the_take_as_the_angle(api, monkeypatch):
    from lnp import drafting as drafting_mod, voice as voice_mod

    seed([Row(ID="01NEW", Status=Status.NEW,
               SourceURL="https://x.test/a", SourceTitle="A title")])
    store_for().save_voice_card("A starter voice card.")

    monkeypatch.setattr(voice_mod, "build_voice_context", lambda config, **kw: "VOICE CONTEXT")
    monkeypatch.setattr(drafting_mod, "draft",
                         lambda config, row, ctx, **kw: "A fresh draft of about the right length. " * 20)

    response = api.post(
        "/api/rows/01NEW/redraft-now",
        json={"take": "Procurement cycles, not model quality, decide adoption."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFTED"
    assert "fresh draft" in body["draft_text"]
    assert body["scheduled_for"]
    assert body["take"] == "Procurement cycles, not model quality, decide adoption."


def test_redraft_now_works_with_no_take_at_all(api, monkeypatch):
    """No angle, no history - still a generic blurb the person can edit or publish."""
    from lnp import drafting as drafting_mod, voice as voice_mod

    seed([Row(ID="01NEW", Status=Status.NEW,
               SourceURL="https://x.test/a", SourceTitle="A title")])
    store_for().save_voice_card("A starter voice card.")

    monkeypatch.setattr(voice_mod, "build_voice_context", lambda config, **kw: "VOICE CONTEXT")
    monkeypatch.setattr(drafting_mod, "draft",
                         lambda config, row, ctx, **kw: "A generic blurb about the article. " * 20)

    response = api.post("/api/rows/01NEW/redraft-now", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "DRAFTED"


def test_redraft_now_revises_a_drafted_row_using_the_take_as_the_instruction(api, monkeypatch):
    from lnp import drafting as drafting_mod, voice as voice_mod

    seed([Row(ID="01REV", Status=Status.DRAFTED, Angle="An angle",
               DraftText="old draft", RevisionCount="1",
               SourceURL="https://x.test/b", SourceTitle="B title",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    store_for().save_voice_card("A starter voice card.")

    monkeypatch.setattr(voice_mod, "build_voice_context", lambda config, **kw: "VOICE CONTEXT")
    monkeypatch.setattr(drafting_mod, "revise",
                         lambda config, row, ctx, **kw: "A revised draft. " * 18)
    monkeypatch.setattr(voice_mod, "propose_rules", lambda config, items, card: [])

    response = api.post(
        "/api/rows/01REV/redraft-now", json={"take": "cut the closing question"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFTED"
    assert "revised draft" in body["draft_text"]

    row = store_for().pipeline_rows()[0]
    assert row.RevisionCount == "2"
    assert row.RevisionNote == ""


def test_redraft_now_clones_a_posted_row_instead_of_overwriting_it(api, monkeypatch):
    from lnp import drafting as drafting_mod, voice as voice_mod

    seed([Row(ID="01POSTED", Status=Status.POSTED, DraftText="what actually went out",
               PostURN="urn:li:share:1", PostedAt=iso(),
               SourceURL="https://x.test/c", SourceTitle="C title")])
    store_for().save_voice_card("A starter voice card.")

    monkeypatch.setattr(voice_mod, "build_voice_context", lambda config, **kw: "VOICE CONTEXT")
    monkeypatch.setattr(drafting_mod, "draft",
                         lambda config, row, ctx, **kw: "A brand new draft. " * 20)

    response = api.post("/api/rows/01POSTED/redraft-now", json={"take": "a new angle"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] != "01POSTED"
    assert body["status"] == "DRAFTED"

    original = next(r for r in api.get("/api/rows").json() if r["id"] == "01POSTED")
    assert original["status"] == "POSTED"
    assert original["draft_text"] == "what actually went out"
    assert original["post_urn"] == "urn:li:share:1"


def test_redraft_now_refuses_a_row_being_published(api):
    seed([Row(ID="01A", Status=Status.POSTING, DraftText="d")])
    response = api.post("/api/rows/01A/redraft-now", json={"take": "x"})
    assert response.status_code == 409


def test_redraft_now_does_not_reach_another_tenants_row(api, monkeypatch):
    from lnp import drafting as drafting_mod, voice as voice_mod

    seed([Row(ID="01THEIRS", Status=Status.DRAFTED, Angle="a", DraftText="old",
               RevisionCount="0",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())], tenant_id=OTHER)

    monkeypatch.setattr(voice_mod, "build_voice_context", lambda config, **kw: "VOICE CONTEXT")
    monkeypatch.setattr(drafting_mod, "revise", lambda config, row, ctx, **kw: "revised. " * 20)

    response = api.post("/api/rows/01THEIRS/redraft-now", json={"take": "note"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Publish timing: approve via the status dropdown, then schedule or post now
# --------------------------------------------------------------------------


def test_approving_via_status_does_not_itself_set_a_schedule(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    body = api.put("/api/rows/01A/status", json={"status": "APPROVED"}).json()
    assert body["status"] == "APPROVED"
    assert body["scheduled_for"] == ""


def test_a_freshly_approved_row_can_then_be_scheduled(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    api.put("/api/rows/01A/status", json={"status": "APPROVED"})
    when = utcnow() + timedelta(days=3)
    body = api.put(
        "/api/rows/01A/schedule", json={"scheduled_for": when.isoformat()}
    ).json()
    assert body["scheduled_for"].startswith(when.strftime("%Y-%m-%dT%H:%M"))


# --------------------------------------------------------------------------
# Publish timing on an already-approved row
# --------------------------------------------------------------------------


def test_reschedule_moves_an_approved_rows_slot(api):
    seed([Row(ID="01A", Status=Status.APPROVED, DraftText="a draft",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    when = utcnow() + timedelta(days=5)
    body = api.put(
        "/api/rows/01A/schedule", json={"scheduled_for": when.isoformat()}
    ).json()
    assert body["scheduled_for"].startswith(when.strftime("%Y-%m-%dT%H:%M"))


def test_reschedule_refuses_a_row_that_is_not_approved(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    when = utcnow() + timedelta(days=1)
    response = api.put(
        "/api/rows/01A/schedule", json={"scheduled_for": when.isoformat()}
    )
    assert response.status_code == 409


def test_reschedule_refuses_a_past_time(api):
    seed([Row(ID="01A", Status=Status.APPROVED, DraftText="a draft",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    when = utcnow() - timedelta(hours=1)
    response = api.put(
        "/api/rows/01A/schedule", json={"scheduled_for": when.isoformat()}
    )
    assert response.status_code == 400


def test_reschedule_refuses_an_unparseable_time(api):
    seed([Row(ID="01A", Status=Status.APPROVED, DraftText="a draft",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    response = api.put("/api/rows/01A/schedule", json={"scheduled_for": "not a date"})
    assert response.status_code == 400


def test_publish_now_route_refuses_a_row_that_is_not_approved(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    assert api.post("/api/rows/01A/publish-now").status_code == 409


def test_publish_now_route_is_a_dry_run_on_the_shipped_default(api, monkeypatch):
    from lnp import publish_now as publish_now_mod
    from lnp.linkedin import LinkedIn

    seed([Row(ID="01A", Status=Status.APPROVED, DraftText="a real draft",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    connect_linkedin(store_for())
    store_for().set_config_value("PAUSED", "FALSE")

    class FakeAPI:
        base = "https://api.linkedin.com"
        version = "202605"

        def __init__(self, config, tokens, session=None):
            pass

        def person_urn(self):
            return "urn:li:person:ABC123"

        build_payload = LinkedIn.build_payload

        def create_post(self, payload):  # pragma: no cover - must never run
            raise AssertionError("dry run must not call the API")

    monkeypatch.setattr(publish_now_mod, "LinkedIn", FakeAPI)

    body = api.post("/api/rows/01A/publish-now").json()
    assert body["status"] == "APPROVED"
    assert "dry run" in body["error"].lower()


def test_publish_now_route_respects_the_pause_switch(api, monkeypatch):
    from lnp import publish_now as publish_now_mod

    def explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("must not touch LinkedIn while paused")

    seed([Row(ID="01A", Status=Status.APPROVED, DraftText="a real draft",
               ScheduledFor=(utcnow() + timedelta(days=1)).isoformat())])
    connect_linkedin(store_for())
    store_for().set_config_value("PAUSED", "TRUE")
    monkeypatch.setattr(publish_now_mod, "LinkedIn", explode)

    body = api.post("/api/rows/01A/publish-now").json()
    assert body["status"] == "APPROVED"
    assert "paused" in body["error"].lower()


# --------------------------------------------------------------------------
# Audience: who this account writes for, and the voice card it seeds
# --------------------------------------------------------------------------


def test_set_audience_saves_description_and_redrafts_who_and_stance(api, monkeypatch):
    from lnp import voice as voice_mod

    store_for().save_voice_card(STARTER_VOICE_CARD)

    def fake_complete(config, *, system, user, model=None, max_tokens=0, **kw):
        assert "Staff platform engineers" in user
        return "## Who is writing\nStaff platform engineers.\n\n## Stance\n- Prefers proven tools.\n"

    monkeypatch.setattr(voice_mod, "complete", fake_complete)

    body = api.put(
        "/api/audience", json={"description": "Staff platform engineers"}
    ).json()
    assert "Staff platform engineers" in body["content"]
    assert "Prefers proven tools" in body["content"]
    # The audience-agnostic sections survive untouched.
    assert "## Structure" in body["content"]
    assert "## Banned" in body["content"]
    assert "<!-- AMENDMENTS-BEGIN -->" in body["content"]

    me = api.get("/api/me").json()
    assert me["audience_description"] == "Staff platform engineers"


def test_set_audience_rejects_a_blank_description(api):
    response = api.put("/api/audience", json={"description": "   "})
    assert response.status_code == 422


def test_set_audience_runs_inside_a_metered_run(api, monkeypatch):
    """The call has to go through runner.runs(), the same as every other
    on-demand LLM call, or it is unlimited and free rather than metered."""
    from lnp import llm, voice as voice_mod

    store_for().save_voice_card(STARTER_VOICE_CARD)
    seen = {}

    def fake_complete(config, *, system, user, model=None, max_tokens=0, **kw):
        seen["meter"] = llm.current_meter()
        return "## Who is writing\nSomeone.\n\n## Stance\n- a point.\n"

    monkeypatch.setattr(voice_mod, "complete", fake_complete)
    response = api.put("/api/audience", json={"description": "Someone specific"})
    assert response.status_code == 200
    assert seen["meter"] is not None
