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
from conftest import make_engine

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
    assert api.patch("/api/rows/01THEIRS", json={"angle": "mine now"}).status_code == 404
    assert api.post("/api/rows/01THEIRS/approve").status_code == 404


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


def test_selecting_and_angling_a_row(api):
    seed([Row(ID="01A", Status=Status.NEW)])
    body = api.patch(
        "/api/rows/01A", json={"selected": True, "angle": "what buyers miss"}
    ).json()
    assert body["selected"] is True
    assert body["angle"] == "what buyers miss"


# --------------------------------------------------------------------------
# The decisions
# --------------------------------------------------------------------------


def test_approving_marks_the_row_and_publishes_nothing(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a real draft")])
    body = api.post("/api/rows/01A/approve").json()
    assert body["status"] == Status.APPROVED
    assert body["post_urn"] == ""     # nothing was published by this call


def test_an_empty_row_cannot_be_approved(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="   ")])
    response = api.post("/api/rows/01A/approve")
    assert response.status_code == 400
    assert "nothing to publish" in response.json()["detail"]


def test_a_new_row_cannot_be_approved(api):
    """Approval has to pass through a draft somebody actually read."""
    seed([Row(ID="01A", Status=Status.NEW, DraftText="")])
    assert api.post("/api/rows/01A/approve").status_code in (400, 409)


def test_a_posted_row_cannot_be_approved_again(api):
    seed([Row(ID="01A", Status=Status.POSTED, DraftText="already out", PostedAt=iso())])
    response = api.post("/api/rows/01A/approve")
    assert response.status_code == 409
    assert "POSTED" in response.json()["detail"]


def test_revising_stores_the_note_and_moves_the_row(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    body = api.post("/api/rows/01A/revise", json={"note": "stop opening with a question"}).json()
    assert body["status"] == Status.REVISE
    assert body["revision_note"] == "stop opening with a question"


def test_an_empty_revision_note_is_refused(api):
    seed([Row(ID="01A", Status=Status.DRAFTED, DraftText="a draft")])
    assert api.post("/api/rows/01A/revise", json={"note": "  "}).status_code in (400, 422)


def test_allowed_actions_come_from_the_state_machine(api):
    seed([
        Row(ID="01A", Status=Status.DRAFTED, DraftText="d"),
        Row(ID="01B", Status=Status.POSTING, DraftText="d"),
        Row(ID="01C", Status=Status.APPROVED, DraftText="d"),
    ])
    actions = {r["id"]: r["allowed_actions"] for r in api.get("/api/rows").json()}
    assert "approve" in actions["01A"]
    assert actions["01B"] == []            # mid-flight: the human waits
    assert "approve" not in actions["01C"]  # already approved


def test_a_row_being_published_cannot_be_touched(api):
    seed([Row(ID="01A", Status=Status.POSTING, DraftText="d")])
    assert api.post("/api/rows/01A/skip").status_code == 409


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
    # But not approve new posts.
    assert api.post("/api/rows/01A/approve").status_code == 402


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def test_the_voice_card_is_editable_in_full(api):
    assert api.get("/api/voice-card").status_code == 404
    api.put("/api/voice-card", json={"content": "# Voice\n\nWrite plainly."})
    assert "Write plainly" in api.get("/api/voice-card").json()["content"]


def test_accepting_an_amendment_does_not_itself_change_the_card(api):
    """Two gates: a person ticks it, and the weekly job writes it."""
    api.put("/api/voice-card", json={"content": "# Voice\n\n<!-- AMENDMENTS-BEGIN -->\n<!-- AMENDMENTS-END -->"})
    with session_mod.session_scope() as session:
        session.add(VoiceAmendment(id="01AM", tenant_id=TENANT, rule="No em dashes",
                                   signal="DIFF", source_row_ids=[]))

    body = api.put("/api/amendments/01AM", json={"accepted": True}).json()
    assert body["accepted"] is True
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
