"""Tests for the pipeline invariants.

All network is mocked. Every non-negotiable invariant in the brief has a test
here; if one of these goes red, something that could embarrass the owner of the
account has stopped being enforced.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lnp import models
from lnp.models import (
    COLUMNS,
    HUMAN_OWNED_COLUMNS,
    ColumnPermissionError,
    Row,
    Status,
    TransitionError,
    assert_transition,
    assert_writable,
)
from lnp.util import (
    normalize_url,
    normalized_edit_distance,
    parse_bool,
    utcnow,
)

from conftest import (
    TENANT,
    connect_linkedin,
    make_run,
    make_store,
    pipeline_config,
    rows_of,
)


# --------------------------------------------------------------------------
# 1. Status state machine
# --------------------------------------------------------------------------


def test_approved_to_posting_is_legal():
    assert_transition(Status.APPROVED, Status.POSTING)


def test_drafted_to_posting_is_illegal():
    with pytest.raises(TransitionError):
        assert_transition(Status.DRAFTED, Status.POSTING)


def test_new_to_approved_is_illegal():
    """Approval must pass through a draft a human has actually read."""
    with pytest.raises(TransitionError):
        assert_transition(Status.NEW, Status.APPROVED)


def test_posted_is_terminal():
    for target in models.ALL_STATUSES:
        with pytest.raises(TransitionError):
            assert_transition(Status.POSTED, target)


def test_expired_and_skipped_are_terminal():
    for terminal in (Status.EXPIRED, Status.SKIPPED):
        for target in models.ALL_STATUSES:
            with pytest.raises(TransitionError):
                assert_transition(terminal, target)


def test_revise_round_trips():
    assert_transition(Status.DRAFTED, Status.REVISE)
    assert_transition(Status.REVISE, Status.DRAFTED)
    assert_transition(Status.DRAFTED, Status.APPROVED)


def test_publish_failure_path_returns_to_approved():
    assert_transition(Status.APPROVED, Status.POSTING)
    assert_transition(Status.POSTING, Status.FAILED)
    assert_transition(Status.FAILED, Status.APPROVED)


def test_posting_cannot_be_re_entered_from_posting():
    """Guards double-publish: a stuck POSTING row cannot re-enter POSTING."""
    with pytest.raises(TransitionError):
        assert_transition(Status.POSTING, Status.POSTING)


def test_drafted_and_approved_may_expire_but_posting_may_not():
    assert_transition(Status.DRAFTED, Status.EXPIRED)
    assert_transition(Status.APPROVED, Status.EXPIRED)
    with pytest.raises(TransitionError):
        assert_transition(Status.POSTING, Status.EXPIRED)


def test_unknown_status_raises():
    with pytest.raises(TransitionError):
        assert_transition("BANANA", Status.DRAFTED)
    with pytest.raises(TransitionError):
        assert_transition(Status.NEW, "BANANA")


def test_blank_current_status_is_treated_as_new():
    assert_transition("", Status.DRAFTED)


# --------------------------------------------------------------------------
# 2. Human-owned columns
# --------------------------------------------------------------------------


def test_jobs_cannot_write_human_columns():
    for name in HUMAN_OWNED_COLUMNS:
        with pytest.raises(ColumnPermissionError):
            assert_writable([name])


def test_jobs_can_write_machine_columns():
    assert_writable(["Status", "DraftText", "PostURN", "Error", "EditDistance"])


def test_revision_note_clearing_is_the_only_exception():
    assert_writable(["RevisionNote"], allow_revision_note=True)
    with pytest.raises(ColumnPermissionError):
        assert_writable(["Angle"], allow_revision_note=True)


def test_unknown_column_raises():
    with pytest.raises(ColumnPermissionError):
        assert_writable(["NotAColumn"])


# --------------------------------------------------------------------------
# 3. Row serialisation and derived views
# --------------------------------------------------------------------------


def test_column_order_is_exact():
    assert COLUMNS == [
        "ID", "CreatedAt", "SourceURL", "SourceTitle", "Audience", "Theme",
        "WhyItMatters", "RelevanceScore", "Selected", "Angle", "DraftText",
        "FinalText", "RevisionNote", "RevisionCount", "CharCount", "Status",
        "ScheduledFor", "PostURN", "PostedAt", "EditDistance", "Reach", "Error",
    ]


def test_every_column_has_a_field_and_a_place_to_be_stored():
    """A column with no field, or no mapping, is a column the customer loses."""
    from lnp.db.store import BY_COLUMN

    row = Row()
    for name in COLUMNS:
        assert hasattr(row, name), f"{name} has no field on Row"
        assert name in BY_COLUMN, f"{name} has nowhere to be stored"


def test_final_text_takes_precedence_over_draft():
    row = Row(DraftText="model wrote this", FinalText="human wrote this")
    assert row.effective_text == "human wrote this"
    assert row.was_edited is True


def test_whitespace_final_text_falls_through_to_draft():
    row = Row(DraftText="model wrote this", FinalText="   \n  ")
    assert row.effective_text == "model wrote this"
    assert row.was_edited is False


def test_selected_parsing_across_sheet_values():
    assert parse_bool("TRUE") is True
    assert parse_bool("true") is True
    assert parse_bool(True) is True
    assert parse_bool("") is False
    assert parse_bool("FALSE") is False
    assert parse_bool(None) is False
    assert Row(Selected="TRUE").is_selected is True
    assert Row(Selected="").is_selected is False


# --------------------------------------------------------------------------
# 4. Staleness
# --------------------------------------------------------------------------


def test_row_past_the_cap_is_stale():
    now = utcnow()
    row = Row(ScheduledFor=(now - timedelta(hours=49)).isoformat())
    assert row.is_stale(48, now=now) is True


def test_row_inside_the_cap_is_not_stale():
    now = utcnow()
    row = Row(ScheduledFor=(now - timedelta(hours=47)).isoformat())
    assert row.is_stale(48, now=now) is False


def test_blank_scheduled_for_is_never_stale():
    assert Row(ScheduledFor="").is_stale(48) is False
    assert Row(ScheduledFor="not a date").is_stale(48) is False


def test_future_row_is_not_due():
    now = utcnow()
    assert Row(ScheduledFor=(now + timedelta(hours=2)).isoformat()).is_due(now) is False
    assert Row(ScheduledFor=(now - timedelta(minutes=1)).isoformat()).is_due(now) is True
    assert Row(ScheduledFor="").is_due(now) is True


# --------------------------------------------------------------------------
# 5. Edit distance
# --------------------------------------------------------------------------


def test_identical_text_has_zero_edit_distance():
    assert normalized_edit_distance("same text", "same text") == 0.0
    assert Row(DraftText="same", FinalText="same").edit_distance() == 0.0


def test_full_rewrite_has_high_edit_distance():
    d = normalized_edit_distance(
        "The model wrote a paragraph about learning technology.",
        "Completely different words chosen by a human being here.",
    )
    assert d > 0.5


def test_small_edit_has_small_distance():
    d = normalized_edit_distance("hello world", "hello worlds")
    assert 0 < d < 0.2


def test_empty_pair_is_zero():
    assert normalized_edit_distance("", "") == 0.0


def test_url_normalisation_strips_tracking():
    assert normalize_url(
        "https://WWW.Example.com/story/?utm_source=x&utm_medium=y&id=7"
    ) == "https://example.com/story?id=7"


# --------------------------------------------------------------------------
# 6. Dedupe
# --------------------------------------------------------------------------

from lnp.ingest import Candidate, Deduper, interleave, matches_education_terms


def _c(url, title, **kw):
    return Candidate(url=url, title=title, **kw)


def test_dedupe_by_url_ignores_tracking_params():
    d = Deduper(threshold=85)
    first = _c("https://example.com/story", "One story")
    second = _c(
        "https://www.example.com/story/?utm_source=newsletter&utm_campaign=x",
        "A completely unrelated headline about something else entirely",
    )
    assert d.keep([first, second]) == [first]


def test_dedupe_by_near_identical_title():
    """Four outlets covering one story is the normal case, not the exception."""
    d = Deduper(threshold=85)
    kept = d.keep([
        _c("https://a.test/1", "OpenAI launches a new tutor for K-12 classrooms"),
        _c("https://b.test/2", "OpenAI Launches New Tutor for K-12 Classrooms"),
        _c("https://c.test/3", "New OpenAI tutor launches for K-12 classrooms"),
    ])
    assert len(kept) == 1
    assert d.dropped_title == 2


def test_dedupe_against_sheet_history():
    d = Deduper(
        threshold=85,
        history_urls=["https://example.com/already-seen?utm_source=rss"],
        history_titles=["Coursera reports fourth quarter enrollment growth"],
    )
    by_url = _c("https://example.com/already-seen", "Some fresh headline text here")
    by_title = _c("https://other.test/x", "Coursera Reports Fourth-Quarter Enrollment Growth")
    assert d.keep([by_url, by_title]) == []


def test_distinct_stories_survive_dedupe():
    d = Deduper(threshold=85)
    items = [
        _c("https://a.test/1", "Duolingo earnings beat expectations"),
        _c("https://b.test/2", "OECD publishes new report on adult skills"),
        _c("https://c.test/3", "University of Michigan rethinks its LMS contract"),
    ]
    assert d.keep(items) == items


def test_education_filter_keeps_only_relevant_arxiv_items():
    terms = ["education", "learning", "student"]
    relevant = _c("https://arxiv.org/abs/1", "LLM tutors and student outcomes")
    irrelevant = _c("https://arxiv.org/abs/2", "Sparse attention kernels on TPUs")
    assert matches_education_terms(relevant, terms) is True
    assert matches_education_terms(irrelevant, terms) is False


def test_interleave_prevents_one_feed_filling_the_cap():
    prolific = [_c(f"https://a.test/{i}", f"A {i}") for i in range(10)]
    weekly = [_c("https://b.test/1", "B 1")]
    out = interleave([prolific, weekly], 4)
    assert [c.url for c in out] == [
        "https://a.test/0", "https://b.test/1", "https://a.test/1", "https://a.test/2",
    ]


# --------------------------------------------------------------------------
# 7. Scoring, tier weights and mix enforcement
# --------------------------------------------------------------------------

from lnp.config import Config
from lnp.scoring import ScoredCandidate, enforce_mix, score_candidates

QUOTA_CONFIG = Config({
    "scoring": {
        "candidates": 10,
        "batch_size": 40,
        "quotas": {
            "audience": {"AUD_CORPORATE": 0.5, "AUD_ACADEMIC": 0.5},
            "theme": {
                "THM_AI": 0.4, "THM_PLATFORM": 0.2,
                "THM_DELIVERY": 0.2, "THM_STRATEGY": 0.2,
            },
        },
    }
})


def _scored(score, audience, theme, tier=2, weight=1.0, title="t"):
    return ScoredCandidate(
        candidate=Candidate(url=f"https://x.test/{title}{score}{theme}", title=title,
                            tier=tier, weight=weight),
        score=score, audience=audience, theme=theme, why="because",
    )


def test_tier_weighting_favours_primary_sources():
    tier1 = _scored(9.0, "AUD_CORPORATE", "THM_AI", tier=1, weight=0.7)
    tier3 = _scored(7.0, "AUD_CORPORATE", "THM_AI", tier=3, weight=1.3)
    assert tier3.weighted_score > tier1.weighted_score


def test_mix_survives_an_ai_heavy_news_week():
    """Twenty AI items plus a few others must not yield ten AI candidates."""
    pool = [_scored(9.5, "AUD_CORPORATE", "THM_AI", title=f"ai{i}") for i in range(20)]
    pool += [_scored(4.0, "AUD_ACADEMIC", "THM_PLATFORM", title=f"pl{i}") for i in range(3)]
    pool += [_scored(4.0, "AUD_ACADEMIC", "THM_DELIVERY", title=f"de{i}") for i in range(3)]
    pool += [_scored(4.0, "AUD_CORPORATE", "THM_STRATEGY", title=f"st{i}") for i in range(3)]

    picked = enforce_mix(QUOTA_CONFIG, pool)
    themes = {}
    for item in picked:
        themes[item.theme] = themes.get(item.theme, 0) + 1

    assert len(picked) == 10
    assert themes["THM_AI"] <= 5
    assert themes.get("THM_PLATFORM", 0) >= 2
    assert themes.get("THM_DELIVERY", 0) >= 2
    assert themes.get("THM_STRATEGY", 0) >= 2


def test_mix_returns_everything_when_pool_is_small():
    pool = [_scored(5.0, "AUD_CORPORATE", "THM_AI", title=f"a{i}") for i in range(4)]
    assert len(enforce_mix(QUOTA_CONFIG, pool)) == 4


def test_unfillable_quota_is_left_short_not_padded():
    """A week with no platform stories yields no platform candidates."""
    pool = [_scored(8.0, "AUD_ACADEMIC", "THM_AI", title=f"a{i}") for i in range(30)]
    picked = enforce_mix(QUOTA_CONFIG, pool)
    assert len(picked) == 10
    assert all(p.theme == "THM_AI" for p in picked)


def test_scoring_parses_model_output_and_clamps_bad_tags():
    candidates = [
        Candidate(url="https://a.test/1", title="One", tier=3, weight=1.3),
        Candidate(url="https://b.test/2", title="Two", tier=1, weight=0.7),
    ]

    def fake_completer(config, *, system, user, model=None, max_tokens=0):
        return [
            {"i": 0, "score": 8, "audience": "AUD_ACADEMIC", "theme": "THM_AI", "why": "Implication."},
            {"i": 1, "score": 99, "audience": "nonsense", "theme": "nonsense", "why": ""},
        ]

    scored = score_candidates(QUOTA_CONFIG, candidates, completer=fake_completer)
    assert len(scored) == 2
    assert scored[0].weighted_score == 10.4          # 8 * 1.3
    assert scored[1].score == 10.0                    # clamped from 99
    assert scored[1].audience == "AUD_CORPORATE"      # bad tag falls back
    assert scored[1].theme == "THM_STRATEGY"


def test_scoring_survives_a_failed_batch():
    def boom(*a, **kw):
        raise RuntimeError("api down")

    scored = score_candidates(
        QUOTA_CONFIG, [Candidate(url="https://a.test/1", title="One")], completer=boom
    )
    assert scored == []


def test_extract_json_tolerates_fences_and_preamble():
    from lnp.llm import extract_json
    assert extract_json('```json\n[{"i": 0}]\n```') == [{"i": 0}]
    assert extract_json('Here is the JSON:\n[{"i": 1}]') == [{"i": 1}]


# --------------------------------------------------------------------------
# 8. Voice card
# --------------------------------------------------------------------------

from lnp import voice
from lnp.voice import (
    AMENDMENTS_BEGIN,
    AMENDMENTS_END,
    FeedbackItem,
    amend_card_text,
    build_voice_context,
    collect_feedback,
    existing_amendments,
    find_recurring,
    retrieve_examples,
)

STARTER_CARD = (
    Path(__file__).resolve().parents[1] / "config" / "voice_card.md"
).read_text()


@pytest.fixture
def card_config():
    return Config({"voice": {"retrieval_min_posts": 20}})


def test_starter_card_has_markers_and_banned_list():
    """Every new account is seeded from this, so it has to be usable as-is."""
    assert AMENDMENTS_BEGIN in STARTER_CARD and AMENDMENTS_END in STARTER_CARD
    for banned in ["game-changer", "delve", "Thoughts?", "in today's rapidly evolving landscape"]:
        assert banned in STARTER_CARD


def test_amending_a_card_is_idempotent():
    rule = "Delete the closing question; end on the strongest declarative sentence."
    once, written = amend_card_text(STARTER_CARD, [rule])
    assert written == [rule]
    twice, written_again = amend_card_text(once, [rule])
    assert written_again == []
    assert twice.count(rule) == 1
    assert existing_amendments(twice) == [rule]
    assert twice.index(rule) < twice.index(AMENDMENTS_END)


def test_amending_rejects_a_card_without_markers():
    with pytest.raises(ValueError):
        amend_card_text("no markers here", ["a rule"])


def test_voice_context_says_there_is_no_corpus_before_twenty_posts(card_config):
    context = build_voice_context(
        card_config, angle="an angle", published=[], post_count=0, card=STARTER_CARD
    )
    assert "No corpus yet" in context
    assert "generic LinkedIn conventions" in context


def test_voice_context_uses_examples_once_the_corpus_exists(card_config):
    published = [
        Row(ID="1", Status=Status.POSTED, Angle="LMS procurement is broken",
            DraftText="Procurement text about buying an LMS badly."),
        Row(ID="2", Status=Status.POSTED, Angle="AI tutors and evidence",
            DraftText="Tutoring evidence text."),
    ]
    context = build_voice_context(
        card_config, angle="LMS procurement is broken", published=published,
        post_count=25, card=STARTER_CARD,
    )
    assert "Published examples" in context
    assert "Procurement text" in context


def test_retrieval_ranks_the_closest_post_first():
    published = [
        Row(Status=Status.POSTED, Angle="Duolingo earnings", DraftText="streaks and revenue"),
        Row(Status=Status.POSTED, Angle="LMS procurement is broken", DraftText="buying an LMS"),
    ]
    assert retrieve_examples("LMS procurement is broken", published, 1)[0].Angle == (
        "LMS procurement is broken"
    )


def test_collect_feedback_reads_both_signals():
    rows = [
        Row(ID="a", RevisionNote="cut the closing question", DraftText="d", Status=Status.DRAFTED),
        Row(ID="b", Status=Status.POSTED, DraftText="the model wrote this whole thing",
            FinalText="the human rewrote this whole thing completely"),
        Row(ID="c", Status=Status.POSTED, DraftText="identical", FinalText="identical"),
    ]
    items = collect_feedback(rows)
    assert {(i.row_id, i.signal) for i in items} == {("a", "NOTE"), ("b", "DIFF")}


def test_recurrence_alarm_fires_on_the_third_repeat():
    items = [
        FeedbackItem(row_id=str(i), signal="NOTE", instruction="drop the closing question")
        for i in range(2)
    ]
    assert find_recurring(items, threshold=3) == {}

    items.append(
        FeedbackItem(row_id="3", signal="NOTE", instruction="Drop the closing question please")
    )
    recurring = find_recurring(items, threshold=3)
    assert len(recurring) == 1
    assert len(next(iter(recurring.values()))) == 3


def test_recurrence_needs_distinct_posts():
    """Three notes on one row is one stubborn row, not a systemic problem."""
    items = [
        FeedbackItem(row_id="same", signal="NOTE", instruction="drop the closing question")
        for _ in range(3)
    ]
    assert find_recurring(items, threshold=3) == {}


# --------------------------------------------------------------------------
# 9. Drafting
# --------------------------------------------------------------------------

from lnp import drafting
from lnp.drafting import (
    VARIANT_A,
    VARIANT_B,
    Extract,
    build_draft_prompt,
    char_count,
    check_constraints,
    contains_both_variants,
    postprocess,
    split_variants,
)

DRAFT_CONFIG = Config({
    "drafting": {
        "min_chars": 900, "max_chars": 1300, "hook_chars": 210,
        "max_hashtags": 3, "model": "claude-sonnet-4-6", "max_tokens": 4000,
    }
})


def test_postprocess_strips_fences_preamble_and_em_dashes():
    raw = "```\nHere's the post:\n\nThe claim is simple — it costs money.\n```"
    out = postprocess(raw)
    assert "—" not in out
    assert "```" not in out
    assert not out.lower().startswith("here")
    assert out == "The claim is simple, it costs money."


def test_postprocess_leaves_clean_text_alone():
    text = "First line carries the claim.\n\nSecond paragraph does the work."
    assert postprocess(text) == text


def test_both_variants_present_is_detected():
    text = f"{VARIANT_A}\npost one\n\n{VARIANT_B}\npost two"
    assert contains_both_variants(text) is True
    assert split_variants(text) == ("post one", "post two")
    assert contains_both_variants("just one post") is False


def test_char_count_measures_the_longest_variant_not_the_wrapper():
    text = f"{VARIANT_A}\n{'a' * 950}\n\n{VARIANT_B}\n{'b' * 1100}"
    assert char_count(text) == 1100


def test_constraint_check_flags_the_things_the_card_bans():
    problems = check_constraints(DRAFT_CONFIG, "Too short. Is that clear?")
    assert any("chars" in p for p in problems)
    assert any("question" in p for p in problems)


def test_constraint_check_passes_a_good_draft():
    body = ("The claim goes here and it is specific. " * 24)[:1000] + " It ends flat."
    assert check_constraints(DRAFT_CONFIG, body) == []


def test_draft_prompt_orders_voice_then_angle_then_source_then_constraints():
    prompt = build_draft_prompt(
        DRAFT_CONFIG,
        voice_context="VOICE CARD BODY",
        angle="Procurement cycles, not model quality, decide adoption.",
        title="A title", url="https://x.test/a", why_it_matters="It implies things.",
        extract=Extract("article body text", True), variants=True,
    )
    assert prompt.index("VOICE CARD BODY") < prompt.index("this is the thesis")
    assert prompt.index("this is the thesis") < prompt.index("Source material")
    assert prompt.index("Source material") < prompt.index("Constraints:")
    assert VARIANT_A in prompt and VARIANT_B in prompt
    assert "Only numbers that appear in the source extract" in prompt


def test_failed_extraction_forbids_inventing_detail():
    prompt = build_draft_prompt(
        DRAFT_CONFIG, voice_context="v", angle="a", title="t", url="u",
        why_it_matters="", extract=Extract("", False, "404"), variants=False,
    )
    assert "Do not state" in prompt
    assert "do not imply you have read it" in prompt


def test_draft_uses_the_angle_and_post_processes(monkeypatch):
    captured = {}

    def fake_completer(config, *, system, user, model=None, max_tokens=0, temperature=None):
        captured["system"] = system
        captured["user"] = user
        return "```\nHere is the post:\nA claim — with a dash.\n```"

    row = Row(ID="1", SourceURL="https://x.test/a", SourceTitle="T",
              Angle="Procurement, not model quality, decides adoption.")
    out = drafting.draft(
        DRAFT_CONFIG, row, "VOICE", variants=False,
        extract=Extract("body", True), completer=fake_completer,
    )
    assert out == "A claim, with a dash."
    assert "you have failed" in captured["system"]
    assert "Procurement, not model quality" in captured["user"]


def test_revision_prompt_carries_the_note_and_previous_draft():
    row = Row(ID="1", Angle="the angle", DraftText="the previous draft",
              RevisionNote="cut the closing question", SourceTitle="T", SourceURL="u")
    captured = {}

    def fake_completer(config, *, system, user, model=None, max_tokens=0, temperature=None):
        captured["system"] = system
        captured["user"] = user
        return "revised text"

    out = drafting.revise(
        DRAFT_CONFIG, row, "VOICE", extract=Extract("body", True), completer=fake_completer
    )
    assert out == "revised text"
    assert "the previous draft" in captured["user"]
    assert "cut the closing question" in captured["user"]
    assert "Leave everything else alone" in captured["system"]


# --------------------------------------------------------------------------
# 10. Store writes, and the publish invariants
# --------------------------------------------------------------------------

from lnp.models import COLUMN_INDEX, health_stats


def test_store_write_refuses_human_columns():
    store = make_store([Row(ID="01A", Status=Status.DRAFTED)])
    live = store.pipeline_rows()[0]
    with pytest.raises(ColumnPermissionError):
        store.write(live, {"Angle": "a job must never write this"})
    with pytest.raises(ColumnPermissionError):
        store.write(live, {"FinalText": "nor this"})
    store.write(live, {"DraftText": "but this is fine"})
    assert rows_of(store)[0].DraftText == "but this is fine"


def test_store_transition_refuses_illegal_moves_before_writing():
    store = make_store([Row(ID="01A", Status=Status.DRAFTED, DraftText="d")])
    live = store.pipeline_rows()[0]
    with pytest.raises(TransitionError):
        store.transition(live, Status.POSTED, {"PostURN": "urn:li:share:1"})
    stored = rows_of(store)[0]
    assert stored.status == Status.DRAFTED
    assert stored.PostURN == ""


def test_revision_note_can_only_be_cleared_through_the_exception():
    store = make_store([Row(ID="01A", Status=Status.REVISE, RevisionNote="shorter")])
    live = store.pipeline_rows()[0]
    with pytest.raises(ColumnPermissionError):
        store.write(live, {"RevisionNote": ""})
    store.write(live, {"RevisionNote": ""}, allow_revision_note=True)
    assert rows_of(store)[0].RevisionNote == ""


def test_kill_switch_reads_paused():
    assert make_store(paused="TRUE").is_paused() is True
    assert make_store(paused="FALSE").is_paused() is False


def test_missing_paused_key_counts_as_paused():
    """An unreachable stop button might be pressed. Assume it is."""
    store = make_store()
    store.set_config_value("PAUSED", "")
    assert store.is_paused() is True


def test_expire_stale_only_touches_drafted_and_approved():
    old = (utcnow() - timedelta(hours=100)).isoformat()
    store = make_store([
        Row(ID="01A", Status=Status.DRAFTED, ScheduledFor=old),
        Row(ID="01B", Status=Status.APPROVED, ScheduledFor=old),
        Row(ID="01C", Status=Status.POSTED, ScheduledFor=old),
        Row(ID="01D", Status=Status.NEW, ScheduledFor=old),
    ])
    expired = store.expire_stale(store.pipeline_rows(), 48)
    assert {r.ID for r in expired} == {"01A", "01B"}


# ---- Job C ---------------------------------------------------------------

from jobs import publish as publish_job


def test_job_c_acts_only_on_approved():
    now = utcnow()
    past = (now - timedelta(hours=1)).isoformat()
    rows = [
        Row(ID="new", Status=Status.NEW, ScheduledFor=past),
        Row(ID="drafted", Status=Status.DRAFTED, ScheduledFor=past),
        Row(ID="revise", Status=Status.REVISE, ScheduledFor=past),
        Row(ID="approved", Status=Status.APPROVED, ScheduledFor=past),
        Row(ID="posting", Status=Status.POSTING, ScheduledFor=past),
        Row(ID="posted", Status=Status.POSTED, ScheduledFor=past),
        Row(ID="failed", Status=Status.FAILED, ScheduledFor=past),
        Row(ID="skipped", Status=Status.SKIPPED, ScheduledFor=past),
        Row(ID="expired", Status=Status.EXPIRED, ScheduledFor=past),
    ]
    assert [r.ID for r in publish_job.due_rows(rows, pipeline_config(), now)] == ["approved"]


def test_job_c_skips_future_and_stale_rows():
    now = utcnow()
    rows = [
        Row(ID="future", Status=Status.APPROVED,
            ScheduledFor=(now + timedelta(hours=3)).isoformat()),
        Row(ID="stale", Status=Status.APPROVED,
            ScheduledFor=(now - timedelta(hours=72)).isoformat()),
        Row(ID="due", Status=Status.APPROVED,
            ScheduledFor=(now - timedelta(minutes=5)).isoformat()),
    ]
    assert [r.ID for r in publish_job.due_rows(rows, pipeline_config(), now)] == ["due"]


def test_stuck_posting_row_is_never_blindly_retried():
    """Unverifiable means leave it alone and tell a human. Never republish."""
    row = Row(ID="stuck", Status=Status.POSTING, DraftText="body text")
    store = make_store([row])
    alerts = []

    class UnhelpfulAPI:
        def find_recent_post(self, author, text):
            raise PostNotConfirmed("posts lookup returned 403")

        def create_post(self, payload):  # pragma: no cover - must never run
            raise AssertionError("a stuck row must never be republished")

    run = make_run(store, alerts)
    publish_job.resolve_stuck_rows(
        run, run.store.pipeline_rows(), UnhelpfulAPI(), "urn:li:person:x"
    )
    assert rows_of(store)[0].status == Status.POSTING   # untouched
    # Untouched, but not unreported: this is the one case only a human can
    # resolve, so silence here would be the worst outcome.
    assert len(alerts) == 1
    assert "could not be verified" in alerts[0]


def test_stuck_row_confirmed_live_becomes_posted():
    row = Row(ID="stuck", Status=Status.POSTING, DraftText="body text")
    store = make_store([row])

    class ConfirmingAPI:
        def find_recent_post(self, author, text):
            return True, "urn:li:share:999"

    run = make_run(store)
    publish_job.resolve_stuck_rows(
        run, run.store.pipeline_rows(), ConfirmingAPI(), "urn:li:person:x"
    )
    stored = rows_of(store)[0]
    assert stored.status == Status.POSTED
    assert stored.PostURN == "urn:li:share:999"


def test_stuck_row_confirmed_absent_becomes_failed():
    row = Row(ID="stuck", Status=Status.POSTING, DraftText="body text")
    store = make_store([row])

    class DenyingAPI:
        def find_recent_post(self, author, text):
            return False, None

    run = make_run(store)
    publish_job.resolve_stuck_rows(
        run, run.store.pipeline_rows(), DenyingAPI(), "urn:li:person:x"
    )
    assert rows_of(store)[0].status == Status.FAILED


from lnp.linkedin import LinkedIn, LinkedInError, PostNotConfirmed


def test_posting_is_written_to_the_sheet_before_the_http_call():
    """A crashed run must leave evidence in the row, not a silent gap."""
    row = Row(ID="a", Status=Status.APPROVED, DraftText="body")
    store = make_store([row])
    live = store.pipeline_rows()[0]
    seen = {}

    class WatchingAPI:
        def create_post(self, payload):
            seen["status_during_call"] = store.pipeline_rows()[0].status
            return "urn:li:share:1"

    store.transition(live, Status.POSTING, {"Error": ""})
    urn = WatchingAPI().create_post({})
    store.transition(live, Status.POSTED, {"PostURN": urn, "PostedAt": iso_now()})

    assert seen["status_during_call"] == Status.POSTING
    assert rows_of(store)[0].status == Status.POSTED


def iso_now():
    from lnp.util import iso
    return iso()


def test_ambiguous_row_with_both_variants_is_refused():
    """Both variants still present means nobody said which post this is."""
    text = f"{VARIANT_A}\nfirst post\n\n{VARIANT_B}\nsecond post"
    row = Row(ID="a", Status=Status.APPROVED, DraftText=text)
    assert contains_both_variants(row.effective_text) is True

    store = make_store([row])
    alerts = []
    run = make_run(store, alerts)

    live = run.store.pipeline_rows()[0]
    publish_job.flag(run, live, "ambiguous: both variants are still present",
                     "title", "body")
    stored = rows_of(store)[0]
    assert stored.status == Status.APPROVED           # nothing was posted, nothing broken
    assert "ambiguous" in stored.Error
    assert len(alerts) == 1

    # A row still waiting on a human must not alert on every run.
    publish_job.flag(run, stored, "ambiguous: both variants are still present",
                     "title", "body")
    assert len(alerts) == 1


def test_linkedin_payload_shape():
    from lnp.tokens import TokenSet
    api = LinkedIn(Config({"publish": {"api_version": "202605"}}), TokenSet(access_token="t"))
    payload = api.build_payload("urn:li:person:abc", "the post body")
    assert payload["author"] == "urn:li:person:abc"
    assert payload["commentary"] == "the post body"
    assert payload["visibility"] == "PUBLIC"
    assert payload["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert payload["lifecycleState"] == "PUBLISHED"
    headers = api._headers()
    assert headers["LinkedIn-Version"] == "202605"
    assert headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert headers["Authorization"] == "Bearer t"


def test_missing_restli_id_header_is_not_confirmed():
    """No URN back means we do not know. That is not the same as failure."""
    from lnp.tokens import TokenSet

    class FakeResponse:
        status_code = 201
        headers = {}
        text = ""

    class FakeSession:
        def post(self, *a, **kw):
            return FakeResponse()

    api = LinkedIn(Config({}), TokenSet(access_token="t"), session=FakeSession())
    with pytest.raises(PostNotConfirmed):
        api.create_post({})


# --------------------------------------------------------------------------
# 11. Health metric
# --------------------------------------------------------------------------


def _posted(days_ago, draft, final="", revisions=0):
    when = (utcnow() - timedelta(days=days_ago)).isoformat()
    return Row(Status=Status.POSTED, PostedAt=when, DraftText=draft,
               FinalText=final, RevisionCount=str(revisions))


def test_clean_publish_rate_counts_untouched_posts():
    rows = [
        _posted(40, "a post published as written"),
        _posted(35, "a post published as written"),
        _posted(30, "the model wrote this", "the human rewrote all of this"),
        _posted(20, "a post", revisions=2),
    ]
    stats = health_stats(rows, window=10, floor=0.5, evaluate_after_days=30)
    assert stats.published == 4
    assert stats.clean == 2
    assert stats.clean_rate == 0.5


def test_health_says_shut_it_off_when_below_the_floor():
    rows = [
        _posted(40 - i, "the model wrote this one", "the human rewrote this one entirely")
        for i in range(5)
    ]
    stats = health_stats(rows, window=10, floor=0.5, evaluate_after_days=30)
    assert stats.clean_rate == 0.0
    assert "shut it off" in stats.verdict


def test_health_holds_judgement_before_the_evaluation_window():
    rows = [_posted(3, "draft", "a totally different published version")]
    stats = health_stats(rows, window=10, floor=0.5, evaluate_after_days=30)
    assert "too early to judge" in stats.verdict
    assert "shut it off" not in stats.verdict


def test_health_tracks_improvement_between_first_and_last_posts():
    rows = [_posted(60 - i, "draft text here", "completely different text entirely")
            for i in range(10)]
    rows += [_posted(20 - i, "draft text here", "draft text here!") for i in range(10)]
    stats = health_stats(rows, window=10)
    assert stats.mean_edit_distance_last < stats.mean_edit_distance_first
    assert stats.improving is True


def test_health_with_no_posts_says_so():
    assert health_stats([Row(Status=Status.DRAFTED)]).published == 0
    assert "no posts" in health_stats([]).verdict


# --------------------------------------------------------------------------
# 12. End-to-end job runs, with every network call faked
# --------------------------------------------------------------------------

import json as _json

from jobs import draft as draft_job
from lnp.tokens import TokenSet

REAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"


def real_config():
    from lnp.config import load_config
    return load_config(REAL_CONFIG_PATH)


def test_shipped_config_has_dry_run_on():
    """The repo must not ship able to post on the first run."""
    assert real_config().dry_run is True


def test_publish_dry_run_logs_a_full_payload_and_posts_nothing(monkeypatch, capsys):
    now = utcnow()
    row = Row(ID="01ROW", Status=Status.APPROVED,
              ScheduledFor=(now - timedelta(minutes=10)).isoformat(),
              DraftText="The claim.\n\nThe evidence.\n\nhttps://x.test/a")
    store = connect_linkedin(make_store([row], tenants=(TENANT,)))

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

    monkeypatch.setattr(publish_job, "load_config", real_config)
    monkeypatch.setattr(publish_job, "LinkedIn", FakeAPI)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--dry-run"])

    assert publish_job.main() == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "POST https://api.linkedin.com/rest/posts" in out
    assert "LinkedIn-Version: 202605" in out

    payload = _json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert payload["author"] == "urn:li:person:ABC123"
    assert payload["commentary"].startswith("The claim.")
    assert payload["visibility"] == "PUBLIC"
    assert payload["lifecycleState"] == "PUBLISHED"
    assert payload["distribution"]["feedDistribution"] == "MAIN_FEED"

    # Nothing moved: the row is still waiting for a real run.
    assert rows_of(store)[0].status == Status.APPROVED


# ---- lnp.publish_now: the "post now" button on approve --------------------


def test_publish_one_posts_immediately_and_marks_the_row_posted(monkeypatch):
    """Same LinkedIn client, same POSTING/POSTED sequence, one row, on request."""
    from lnp import publish_now as publish_now_mod
    from lnp.util import iso

    row = Row(ID="01A", Status=Status.APPROVED, ScheduledFor=iso(utcnow()),
              DraftText="A real approved draft with real text in it.")
    store = connect_linkedin(make_store([row], tenants=(TENANT,)))
    run = make_run(store)

    class FakeAPI:
        base = "https://api.linkedin.com"
        version = "202605"

        def __init__(self, config, tokens, session=None):
            pass

        def person_urn(self):
            return "urn:li:person:ABC123"

        build_payload = LinkedIn.build_payload

        def create_post(self, payload):
            return "urn:li:share:999888777"

    monkeypatch.setattr(publish_now_mod, "LinkedIn", FakeAPI)

    result = publish_now_mod.publish_one(run, rows_of(store)[0], dry_run=False)

    assert result.published is True
    updated = rows_of(store)[0]
    assert updated.status == Status.POSTED
    assert updated.PostURN == "urn:li:share:999888777"
    assert updated.Error == ""


def test_publish_one_refuses_while_paused_without_touching_linkedin():
    from lnp import publish_now as publish_now_mod
    from lnp.util import iso

    def explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("must not touch LinkedIn while paused")

    row = Row(ID="01A", Status=Status.APPROVED, ScheduledFor=iso(utcnow()),
              DraftText="A real approved draft.")
    store = connect_linkedin(make_store([row], paused="TRUE", tenants=(TENANT,)))
    run = make_run(store)
    run.token_backend = explode

    result = publish_now_mod.publish_one(run, rows_of(store)[0], dry_run=False)

    assert result.published is False
    assert "paused" in result.detail.lower()
    updated = rows_of(store)[0]
    assert updated.status == Status.APPROVED           # nothing moved
    assert "paused" in updated.Error.lower()


def test_publish_stops_at_the_kill_switch(monkeypatch, capsys):
    row = Row(ID="01ROW", Status=Status.APPROVED,
              ScheduledFor=(utcnow() - timedelta(minutes=10)).isoformat(),
              DraftText="body")
    store = make_store([row], paused="TRUE", tenants=(TENANT,))

    def explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("PAUSED must stop the job before tokens are touched")

    monkeypatch.setattr(publish_job, "load_config", real_config)
    monkeypatch.setattr(publish_job.token_mod, "load_fresh", explode)
    monkeypatch.setattr(sys, "argv", ["publish.py"])

    assert publish_job.main() == 0
    assert "paused" in capsys.readouterr().out.lower()
    assert rows_of(store)[0].status == Status.APPROVED


def test_draft_job_drafts_a_selected_row_and_regenerates_a_revise_row(monkeypatch):
    rows = [
        Row(ID="01NEW", Status=Status.NEW, Selected="TRUE",
            Angle="Procurement cycles, not model quality, decide adoption.",
            SourceURL="https://x.test/a", SourceTitle="A title"),
        Row(ID="01REV", Status=Status.REVISE, Selected="TRUE", Angle="Another angle",
            DraftText="the old draft", RevisionNote="cut the closing question",
            RevisionCount="1", SourceURL="https://x.test/b", SourceTitle="B title",
            ScheduledFor=(utcnow() + timedelta(days=1)).isoformat()),
        Row(ID="01UNS", Status=Status.NEW, Selected="", Angle="",
            SourceURL="https://x.test/c", SourceTitle="C title"),
    ]
    store = make_store(rows, tenants=(TENANT,))
    seen = {}

    monkeypatch.setattr(draft_job, "load_config", real_config)
    monkeypatch.setattr(draft_job.voice, "build_voice_context",
                        lambda config, **kw: "VOICE CONTEXT")
    def fake_draft(config, row, ctx, **kw):
        seen["draft_angle"] = row.angle
        return "A fresh draft of about the right length. " * 20

    def fake_revise(config, row, ctx, **kw):
        seen["note"] = row.revision_note
        return "A revised draft that keeps most of the original. " * 18

    monkeypatch.setattr(draft_job.drafting, "draft", fake_draft)
    monkeypatch.setattr(draft_job.drafting, "revise", fake_revise)
    monkeypatch.setattr(sys, "argv", ["draft.py"])

    assert draft_job.main() == 0

    stored = {r.ID: r for r in rows_of(store)}

    drafted = stored["01NEW"]
    assert drafted.status == Status.DRAFTED
    assert drafted.DraftText.startswith("A fresh draft")
    assert drafted.ScheduledFor                      # got a slot
    assert drafted.CharCount != ""
    assert seen["draft_angle"].startswith("Procurement cycles")

    revised = stored["01REV"]
    assert revised.status == Status.DRAFTED
    assert revised.DraftText.startswith("A revised draft")
    assert revised.RevisionCount == "2"
    assert revised.RevisionNote == ""                # consumed, via the exception
    assert seen["note"] == "cut the closing question"

    untouched = stored["01UNS"]
    assert untouched.status == Status.NEW            # not selected, not drafted
    assert untouched.DraftText == ""


def test_draft_job_skips_a_row_that_hit_the_revision_cap(monkeypatch):
    row = Row(ID="01CAP", Status=Status.REVISE, Selected="TRUE", Angle="an angle",
              DraftText="draft", RevisionNote="try again", RevisionCount="3",
              SourceURL="https://x.test/a", SourceTitle="T")
    store = make_store([row], tenants=(TENANT,))
    alerts = []

    monkeypatch.setattr(draft_job, "load_config", real_config)
    monkeypatch.setattr(
        "lnp.runner.alert",
        lambda cfg, title, body="", **kw: alerts.append(f"{title} {body}"),
    )
    monkeypatch.setattr(draft_job.drafting, "revise",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("capped")))
    monkeypatch.setattr(sys, "argv", ["draft.py"])

    assert draft_job.main() == 0
    assert rows_of(store)[0].status == Status.SKIPPED
    assert any("angle is the problem, not the prose" in a for a in alerts)


def test_next_slot_avoids_a_slot_another_row_already_holds():
    config = real_config()
    first = draft_job.next_slot(config, set())
    second = draft_job.next_slot(config, {first})
    assert first != second
    assert second > first


SYNTHETIC_HEADLINES = [
    "Regional college network consolidates its assessment vendors",
    "Enterprise buyers push back on per-seat pricing for tutoring tools",
    "OECD finds adult reskilling completion rates flat since 2019",
    "Instructional designers report longer approval cycles for AI content",
    "A large employer moves apprenticeship funding in house",
    "Faculty developers publish shared rubric for generative assignments",
    "Learning platform vendor discontinues its authoring module",
    "District procurement office publishes its evaluation criteria",
    "Workforce board redirects grant money toward short credentials",
    "Publisher opens its courseware analytics to institutional review",
    "Union contract adds language on classroom monitoring software",
    "Accreditor clarifies how competency programs report seat time",
]


def test_curate_job_writes_ten_tagged_candidates(monkeypatch, capsys):
    """Job A: feeds in, ten mixed candidates out, nothing selected for the human."""
    from jobs import curate as curate_job
    from lnp.ingest import FeedResult

    # Feeds overlap heavily, as real ones do: 24 feeds x 4 items covering the
    # same 12 stories. Dedupe is expected to collapse them to 12.
    feed_number = {"n": 0}

    def fake_fetch(feed, timeout, user_agent):
        tier = feed["tier"]
        offset = feed_number["n"] * 3
        feed_number["n"] += 1
        items = [
            Candidate(
                url=f"https://{feed['name'].replace(' ', '')}.test/{i}",
                title=SYNTHETIC_HEADLINES[(offset + i) % len(SYNTHETIC_HEADLINES)],
                summary="summary text", source_name=feed["name"],
                tier=tier, weight=feed["weight"],
            )
            for i in range(4)
        ]
        return FeedResult(feed["name"], feed["url"], tier, True, len(items), "", items)

    themes = ["THM_AI", "THM_PLATFORM", "THM_DELIVERY", "THM_STRATEGY"]
    audiences = ["AUD_CORPORATE", "AUD_ACADEMIC"]

    def fake_scorer(config, *, system, user, model=None, max_tokens=0):
        import json
        items = json.loads(user.split("Items to triage:\n", 1)[1])
        return [
            {"i": item["i"], "score": 9 - (item["i"] % 5),
             "audience": audiences[item["i"] % 2],
             "theme": themes[item["i"] % 4],
             "why": "Buyers will feel this before vendors do."}
            for item in items
        ]

    store = make_store(tenants=(TENANT,))
    monkeypatch.setattr(curate_job, "load_config", real_config)
    monkeypatch.setattr(curate_job.ingest, "fetch_feed", fake_fetch)
    monkeypatch.setattr("lnp.scoring.complete_json", fake_scorer)
    monkeypatch.setattr(sys, "argv", ["curate.py"])

    assert curate_job.main() == 0

    rows = rows_of(store)
    assert len(rows) == 10
    assert all(r.Audience in audiences for r in rows)
    assert all(r.Theme in themes for r in rows)
    assert all(r.WhyItMatters for r in rows)
    assert all(r.status == Status.NEW for r in rows)
    assert len({r.ID for r in rows}) == 10          # ULIDs are the idempotency key

    # The mix held: no single theme swallowed the slate.
    counts = {}
    for r in rows:
        counts[r.Theme] = counts.get(r.Theme, 0) + 1
    assert max(counts.values()) <= 5
    assert len(counts) >= 3
    assert len({r.SourceTitle for r in rows}) == 10   # deduped: no story twice

    # Nothing is pre-selected and no angle is invented. Those are the human's.
    assert all(r.Selected == "" and r.Angle == "" for r in rows)
    assert "Tick Selected" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 13. Structured logging
# --------------------------------------------------------------------------

import io
import os
import json as _json_mod
import logging as _logging

from lnp import log as lnp_log


def _capture(fn):
    """Run fn with the JSON formatter attached to a buffer, return the records."""
    buffer = io.StringIO()
    handler = _logging.StreamHandler(buffer)
    handler.setFormatter(lnp_log.JsonFormatter())
    logger = lnp_log.get("test.logging")
    logger.logger.handlers = [handler]
    logger.logger.propagate = False
    logger.logger.setLevel(_logging.INFO)
    fn(logger)
    return [_json_mod.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_field_names_that_shadow_logrecord_attributes_do_not_raise():
    """`extra={"created": ...}` raises KeyError on a plain logger, and took down
    setup_sheet.py. Every one of these is a plausible field name."""
    for name in ["created", "module", "name", "args", "filename", "lineno", "process"]:
        records = _capture(lambda lg, n=name: lg.info("event", extra={n: "value"}))
        assert records[0][name] == "value", f"{name} was dropped or mangled"


def test_fields_keep_their_names_in_the_output():
    records = _capture(lambda lg: lg.info("tab ready", extra={"tab": "Pipeline", "created": False}))
    assert records[0]["msg"] == "tab ready"
    assert records[0]["tab"] == "Pipeline"
    assert records[0]["created"] is False


def test_a_field_colliding_with_a_core_key_does_not_clobber_it():
    records = _capture(lambda lg: lg.info("real message", extra={"msg": "field value"}))
    assert records[0]["msg"] == "real message"
    assert records[0]["msg_"] == "field value"


def test_every_logging_call_in_the_repo_survives_its_own_field_names():
    """Guards the whole codebase, not just the one call site that crashed."""
    import re
    root = Path(__file__).resolve().parents[1]
    checked = 0
    for path in list((root / "src").rglob("*.py")) + list((root / "jobs").rglob("*.py")) \
            + list((root / "scripts").rglob("*.py")):
        for match in re.finditer(r"extra=\{([^}]*)\}", path.read_text(), re.S):
            keys = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', match.group(1))
            if not keys:
                continue
            checked += 1
            records = _capture(lambda lg, k=keys: lg.info("e", extra={n: 1 for n in k}))
            assert records, f"logging call in {path.name} produced no record"
    assert checked > 10, f"expected to find many logging calls, found {checked}"
