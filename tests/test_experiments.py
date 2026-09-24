"""Tests for the rule A/B comparison experiment feature."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_conn


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _run(client, profiles, queries, name=None):
    return client.post("/api/experiments", json={
        "name": name, "profiles": profiles, "queries": queries,
    })


# --------------------------------------------------------------- validation


def test_invalid_profile_params_are_itemised_and_nothing_runs(client):
    before = client.get("/api/experiments").json()["total"]
    r = _run(client, [
        {"name": "P1", "auto_threshold": 1.5},
        {"name": "P2", "reject_threshold": -0.1, "token_weight": 2},
    ], ["some query"])
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    fields = {(e["index"], e["field"]) for e in errors}
    assert (0, "auto_threshold") in fields
    assert (1, "reject_threshold") in fields
    assert (1, "token_weight") in fields
    # validation failure must not persist an experiment
    assert client.get("/api/experiments").json()["total"] == before


def test_reject_above_auto_reported_per_profile(client):
    r = _run(client, [
        {"name": "ok"},
        {"name": "bad", "auto_threshold": 0.5, "reject_threshold": 0.8},
    ], ["123 main st ny"])
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    assert len(errors) == 1
    assert errors[0]["index"] == 1
    assert "拒绝阈值" in errors[0]["message"]


def test_duplicate_profile_names_rejected(client):
    r = _run(client, [{"name": "同"}, {"name": "同"}], ["x"])
    assert r.status_code == 400
    errors = r.json()["detail"]["errors"]
    assert any(e["field"] == "name" for e in errors)


def test_duplicate_and_blank_queries_itemised(client):
    r = _run(client, [{"name": "A"}, {"name": "B"}],
             ["123 Main St", "  123 Main St  ", "   "])
    assert r.status_code == 400
    messages = [e["message"] for e in r.json()["detail"]["errors"]]
    assert any("重复" in m for m in messages)
    assert any("不能为空" in m for m in messages)


def test_profile_count_bounds(client):
    r = _run(client, [{"name": "only"}], ["q"])
    assert r.status_code == 422
    r = _run(client, [{"name": str(i)} for i in range(5)], ["q"])
    assert r.status_code == 422


def test_query_count_bounds(client):
    r = _run(client, [{"name": "A"}, {"name": "B"}], [])
    assert r.status_code == 422
    r = _run(client, [{"name": "A"}, {"name": "B"}], [f"q{i}" for i in range(101)])
    assert r.status_code == 422


def test_nan_and_non_numeric_params_rejected(client):
    # Wrong JSON type -> pydantic structural rejection.
    r = client.post("/api/experiments", json={
        "profiles": [{"name": "A", "auto_threshold": "high"},
                     {"name": "B"}],
        "queries": ["q"],
    })
    assert r.status_code == 422

    # NaN slips through JSON typing but must be caught by the itemised
    # semantic validation and reported without executing.
    r = client.post(
        "/api/experiments",
        content='{"profiles":[{"name":"A"},{"name":"B","margin_threshold":NaN}],'
                '"queries":["q"]}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert any(
        e["field"] == "margin_threshold" and "NaN" in e["message"]
        for e in r.json()["detail"]["errors"]
    )


# -------------------------------------------------------------------- run


def test_experiment_runs_all_profiles_on_same_snapshot(client):
    r = _run(client, [
        {"name": "现行"},
        {"name": "宽松", "auto_threshold": 0.7},
    ], ["123 main st ny", "zzqxkw marsport 99999"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["profile_count"] == 2
    assert data["query_count"] == 2
    assert data["snapshot_current"] is True
    snap = data["table_snapshot"]
    assert {"count", "max_id", "digest"} <= set(snap)

    # each query carries one outcome per profile, in submitted profile order
    for row in data["results"]:
        assert [o["profile"] for o in row["outcomes"]] == ["现行", "宽松"]
        assert row["outcomes"][0]["candidate_diff"] is None  # baseline profile
        assert row["outcomes"][1]["candidate_diff"] is not None
        # normalisation is profile-independent and recorded once per query
        assert "text" in row["normalized"]

    # the gibberish query is no_match for both, and the reject rule fired
    gib = data["results"][1]
    assert gib["conclusions_consistent"] is True
    assert all(o["status"] == "no_match" for o in gib["outcomes"])
    assert all(o["triggered"] == "reject_threshold" for o in gib["outcomes"])
    # a no-match verdict has no winning candidate even if rows were scored
    assert all(o["best_score"] is not None and o["best_score"] < 0.35
               for o in gib["outcomes"])


def test_experiment_marks_disagreement_and_trigger(client):
    # Defaults: 0.80 auto -> 123 main st ny is auto_matched;
    # auto=0.95 forces it below the line -> needs_review (same best id, but
    # the status differs, so it is still a disagreement).
    r = _run(client, [
        {"name": "A"},
        {"name": "严格", "auto_threshold": 0.95},
    ], ["123 main st ny"])
    data = r.json()
    row = data["results"][0]
    assert row["conclusions_consistent"] is False
    assert row["inconsistent_profiles"] == ["严格"]
    outcomes = {o["profile"]: o for o in row["outcomes"]}
    assert outcomes["A"]["status"] == "auto_matched"
    assert outcomes["严格"]["status"] == "needs_review"
    assert outcomes["严格"]["triggered"] == "auto_threshold"
    assert outcomes["A"]["conclusion_differs"] is True
    assert outcomes["严格"]["conclusion_differs"] is True
    assert data["summary"]["disagreements"] == 1
    # trigger tally
    assert data["summary"]["triggered_counts"]["A"]["auto_threshold"] >= 1
    assert data["summary"]["status_counts"]["严格"]["needs_review"] == 1


def test_candidate_diff_detects_score_changes(client):
    r = _run(client, [
        {"name": "A", "token_weight": 0.7},
        {"name": "B", "token_weight": 0.5},
    ], ["1010 Lake Dr, Austin TX"])
    data = r.json()
    diff = data["results"][0]["outcomes"][1]["candidate_diff"]
    # different token weight changes every composite score; ranking kept
    assert diff["score_changed"], diff
    assert diff["rank_changed"] == []
    changed = diff["score_changed"][0]
    assert set(changed) >= {
        "address_id", "baseline_score", "score", "score_delta",
        "baseline_rank", "rank",
    }


def test_margin_trigger_marked_for_tie_at_threshold(client):
    # "Hillcrest Road, Dallas": 50 vs 52 Hillcrest tie -> margin rule, but
    # only when the score is above the auto line.  With auto=0.5 the score
    # clears the line and the margin (0.0) is what blocks auto acceptance.
    r = _run(client, [
        {"name": "A", "auto_threshold": 0.5},
        {"name": "B", "auto_threshold": 0.5, "margin_threshold": 0.0},
    ], ["Hillcrest Road, Dallas"])
    data = r.json()
    a, b = data["results"][0]["outcomes"]
    assert a["status"] == "needs_review"
    assert a["triggered"] == "margin_threshold"
    assert a["margin"] == 0.0
    # a zero margin threshold accepts the tie automatically
    assert b["status"] == "auto_matched"
    assert data["results"][0]["conclusions_consistent"] is False


# ------------------------------------------------------------- persistence


def test_results_persisted_and_replay_is_stable(client):
    r = _run(client, [
        {"name": "A"},
        {"name": "B", "auto_threshold": 0.7, "margin_threshold": 0.02},
        {"name": "C", "token_weight": 0.9},
    ], ["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas",
        "742 Evergreen Tce", "1 Milky Way, Mars Base"],
        name="稳定性实验")
    exp_id = r.json()["id"]

    first = client.get(f"/api/experiments/{exp_id}").json()
    second = client.get(f"/api/experiments/{exp_id}").json()
    # same experiment id + input order -> byte-identical stored verdicts
    assert first["results"] == second["results"]
    assert first["profiles"] == second["profiles"]
    assert first["queries"] == second["queries"]
    assert first["name"] == "稳定性实验"

    # listing endpoint exposes summaries without the full results
    listing = client.get("/api/experiments").json()
    item = next(i for i in listing["items"] if i["id"] == exp_id)
    assert item["query_count"] == 4
    assert "results" not in item
    assert item["summary"]["profiles"] == 3


def test_replay_flags_changed_snapshot(client):
    r = _run(client, [{"name": "A"}, {"name": "B"}], ["123 main st ny"])
    exp_id = r.json()["id"]
    assert client.get(f"/api/experiments/{exp_id}").json()["snapshot_current"] is True

    # mutate the standard-address table after the run
    up = client.post("/api/addresses", json={"addresses": [
        {"address": "999 Experiment Boulevard, Experiment City, EC 12345",
         "external_id": "EXP-NEW-1"},
    ]})
    assert up.status_code == 200

    replayed = client.get(f"/api/experiments/{exp_id}").json()
    assert replayed["snapshot_current"] is False
    # stored results themselves are untouched
    assert replayed["results"][0]["outcomes"][0]["best_id"] == 1


def test_get_unknown_experiment_404(client):
    assert client.get("/api/experiments/999999").status_code == 404


def test_same_input_same_output_across_runs(client):
    payload = {
        "profiles": [
            {"name": "A"},
            {"name": "B", "auto_threshold": 0.72, "token_weight": 0.65},
        ],
        "queries": ["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas"],
    }
    r1 = client.post("/api/experiments", json=payload).json()
    r2 = client.post("/api/experiments", json=payload).json()
    assert r1["id"] != r2["id"]
    assert r1["results"] == r2["results"]
    assert r1["summary"] == r2["summary"]


def test_default_profile_names_assigned_when_blank(client):
    r = _run(client, [{}, {}], ["123 main st ny"])
    data = r.json()
    assert [p["name"] for p in data["profiles"]] == ["档案 A", "档案 B"]
    # missing parameters fall back to server defaults, echoed back
    p = data["profiles"][0]
    assert p["auto_threshold"] == 0.80
    assert p["reject_threshold"] == 0.35
    assert p["margin_threshold"] == 0.06
    assert p["token_weight"] == 0.7


def test_experiment_does_not_write_match_logs_or_apply_consensus(client):
    conn = get_conn()
    from app import db
    before = db.count_match_logs(conn)
    _run(client, [{"name": "A"}, {"name": "B"}],
         ["1010 Lake Dr, Austin TX", "1010 LAKE DR, AUSTIN TX"])
    assert db.count_match_logs(conn) == before
