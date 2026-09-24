"""规则 A/B 对照实验 (rule A/B comparison experiments).

The caller submits 2-4 named *rule profiles* (auto/reject/margin thresholds
and the token weight) together with 1-100 queries.  Every profile runs the
existing matcher (``app.matcher.match_address`` -- raw scoring, no review
consensus, nothing written to ``match_logs``) against the **same snapshot** of
the standard-address table.

Validation is itemised: an invalid profile parameter or a duplicated query is
reported per item and, if anything is wrong, the experiment never runs.

Results are persisted in ``rule_experiments`` and can be replayed by experiment
id.  The matcher is deterministic, so the same profiles and the same query
order against the same table always produce the same result.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from typing import Any

from . import db
from .config import Settings
from .matcher import match_address, result_to_dict

# Parameter ranges are deliberately wide so users can experiment, but values
# must stay finite numbers in [0, 1].
_PARAM_BOUNDS = {
    "auto_threshold": (0.0, 1.0),
    "reject_threshold": (0.0, 1.0),
    "margin_threshold": (0.0, 1.0),
    "token_weight": (0.0, 1.0),
}

_PARAM_LABELS = {
    "auto_threshold": "自动命中阈值",
    "reject_threshold": "拒绝阈值",
    "margin_threshold": "分差阈值",
    "token_weight": "词元权重",
}


class ExperimentValidationError(Exception):
    """At least one item of the request is invalid; nothing was executed.

    ``errors`` carries one entry per problem so the client can mark every
    offending profile/query at once.
    """

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(f"{len(errors)} 项校验错误")
        self.errors = errors


def _defaulted(name: str | None, fallback: str, used: set[str]) -> str:
    base = (name or "").strip() or fallback
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base} ({n})"
        n += 1
    used.add(candidate)
    return candidate


def validate_request(payload: Any, settings: Settings) -> dict[str, Any]:
    """Validate a parsed experiment request item by item.

    Raises :class:`ExperimentValidationError` with *every* problem found when
    the request cannot be executed.
    """
    errors: list[dict[str, Any]] = []

    profiles_raw = payload.get("profiles")
    queries_raw = payload.get("queries")

    profiles_out: list[dict[str, Any]] = []
    queries_out: list[str] = []

    names_seen: dict[str, int] = {}

    for idx, p in enumerate(profiles_raw or []):
        loc = f"profiles[{idx}]"
        params: dict[str, float] = {}
        for key in _PARAM_BOUNDS:
            value = p.get(key)
            if value is None:
                continue
            label = _PARAM_LABELS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append({
                    "location": f"{loc}.{key}", "index": idx, "field": key,
                    "message": f"{label}必须是数字",
                })
                continue
            value = float(value)
            if not math.isfinite(value):
                errors.append({
                    "location": f"{loc}.{key}", "index": idx, "field": key,
                    "message": f"{label}不能是 NaN 或无穷大",
                })
                continue
            low, high = _PARAM_BOUNDS[key]
            if not low <= value <= high:
                errors.append({
                    "location": f"{loc}.{key}", "index": idx, "field": key,
                    "message": f"{label}必须在 {low:g}～{high:g} 之间（收到 {value:g}）",
                })
                continue
            params[key] = value

        name = (p.get("name") or "").strip()
        if name:
            if name in names_seen:
                errors.append({
                    "location": f"{loc}.name", "index": idx, "field": "name",
                    "message": f"档案名称“{name}”与第 {names_seen[name] + 1} 个档案重复",
                })
            else:
                names_seen[name] = idx
        profiles_out.append({"name_raw": name, "params": params})

    # Cross-field checks use the effective (defaulted) values so that omitting
    # a parameter still validates consistently.
    for idx, p in enumerate(profiles_out):
        auto = p["params"].get("auto_threshold", settings.auto_threshold)
        reject = p["params"].get("reject_threshold", settings.reject_threshold)
        if reject > auto:
            errors.append({
                "location": f"profiles[{idx}]", "index": idx,
                "field": "reject_threshold",
                "message": (
                    f"拒绝阈值（{reject:g}）不能高于自动命中阈值（{auto:g}），"
                    "否则两个区间之间没有待确认空间"
                ),
            })

    # Queries: trim, reject blanks, reject duplicates (case/space sensitive on
    # the trimmed text -- the matcher itself is case-insensitive, but the
    # caller asked for duplicate detection on submitted items).
    query_seen: dict[str, int] = {}
    for idx, q in enumerate(queries_raw or []):
        loc = f"queries[{idx}]"
        if not isinstance(q, str):
            errors.append({
                "location": loc, "index": idx, "field": "query",
                "message": "查询必须是文本字符串",
            })
            continue
        text = q.strip()
        if not text:
            errors.append({
                "location": loc, "index": idx, "field": "query",
                "message": "查询不能为空",
            })
            continue
        if text in query_seen:
            errors.append({
                "location": loc, "index": idx, "field": "query",
                "message": (
                    f"查询与第 {query_seen[text] + 1} 条重复："
                    f"“{text if len(text) <= 40 else text[:40] + '…'}”"
                ),
            })
            continue
        query_seen[text] = idx
        queries_out.append(text)

    if errors:
        raise ExperimentValidationError(errors)

    # Materialise final profiles with defaults resolved and unique display
    # names (unnamed profiles become 档案 A / B / ... in order).
    used_names: set[str] = set()
    final_profiles: list[dict[str, Any]] = []
    for idx, p in enumerate(profiles_out):
        params = p["params"]
        auto = params.get("auto_threshold", settings.auto_threshold)
        reject = params.get("reject_threshold", settings.reject_threshold)
        margin = params.get("margin_threshold", settings.margin_threshold)
        weight = params.get("token_weight", settings.token_weight)
        letter = chr(ord("A") + idx)
        final_profiles.append({
            "name": _defaulted(p["name_raw"], f"档案 {letter}", used_names),
            "auto_threshold": auto,
            "reject_threshold": reject,
            "margin_threshold": margin,
            "token_weight": weight,
        })

    return {
        "name": (payload.get("name") or "").strip() or None,
        "profiles": final_profiles,
        "queries": queries_out,
    }


# --------------------------------------------------------------------- runner


def _settings_for(profile: dict[str, Any], base: Settings) -> Settings:
    return replace(
        base,
        auto_threshold=float(profile["auto_threshold"]),
        reject_threshold=float(profile["reject_threshold"]),
        margin_threshold=float(profile["margin_threshold"]),
        token_weight=float(profile["token_weight"]),
    )


def _slim_candidate(cand: dict[str, Any], rank: int) -> dict[str, Any]:
    a = cand["address"]
    return {
        "rank": rank,
        "address_id": a["id"],
        "full_text": a["full_text"],
        "score": cand["score"],
        "token_f1": cand["token_f1"],
        "street_f1": cand["street_f1"],
        "locality_f1": cand["locality_f1"],
        "house_score": cand["house_score"],
        "matched_tokens": cand["matched_tokens"],
        "conflicts": cand["conflicts"],
    }


def _candidate_diff(base_cands: list[dict[str, Any]],
                    other_cands: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare another profile's candidate list with profile A's baseline."""
    base_map = {c["address_id"]: c for c in base_cands}
    other_map = {c["address_id"]: c for c in other_cands}

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    score_changed: list[dict[str, Any]] = []
    rank_changed: list[dict[str, Any]] = []

    for c in other_cands:
        old = base_map.get(c["address_id"])
        if old is None:
            added.append({"address_id": c["address_id"], "full_text": c["full_text"],
                          "rank": c["rank"], "score": c["score"]})
        else:
            delta = round(c["score"] - old["score"], 4)
            if delta:
                score_changed.append({
                    "address_id": c["address_id"], "full_text": c["full_text"],
                    "baseline_rank": old["rank"], "rank": c["rank"],
                    "baseline_score": old["score"], "score": c["score"],
                    "score_delta": delta,
                })
            if c["rank"] != old["rank"]:
                rank_changed.append({
                    "address_id": c["address_id"], "full_text": c["full_text"],
                    "baseline_rank": old["rank"], "rank": c["rank"],
                })

    for c in base_cands:
        if c["address_id"] not in other_map:
            removed.append({"address_id": c["address_id"], "full_text": c["full_text"],
                            "rank": c["rank"], "score": c["score"]})

    return {
        "added": added,
        "removed": removed,
        "score_changed": score_changed,
        "rank_changed": rank_changed,
    }


def run_experiment(conn: sqlite3.Connection, payload: dict[str, Any],
                   settings: Settings) -> dict[str, Any]:
    """Validate ``payload``, run every profile on one table snapshot, persist.

    The snapshot is taken and the experiment row is inserted inside a single
    ``BEGIN IMMEDIATE`` transaction: no other writer can change the address
    table between the snapshot and the run, and on any failure nothing is
    stored.
    """
    valid = validate_request(payload, settings)
    profiles = valid["profiles"]
    queries = valid["queries"]

    with db.immediate_transaction(conn):
        # The documented guarantee: all profiles run against this exact table.
        snapshot = db.address_table_snapshot(conn)
        rows = db.all_addresses(conn)

        profile_settings = [_settings_for(p, settings) for p in profiles]

        results: list[dict[str, Any]] = []
        disagreement_count = 0
        triggered_counts = {
            p["name"]: {"reject_threshold": 0, "margin_threshold": 0,
                        "auto_threshold": 0}
            for p in profiles
        }
        status_counts = {
            p["name"]: {"auto_matched": 0, "needs_review": 0, "no_match": 0}
            for p in profiles
        }

        for q_index, query in enumerate(queries):
            outcomes: list[dict[str, Any]] = []
            normalized_brief: dict[str, Any] | None = None
            for p, p_settings in zip(profiles, profile_settings):
                r = match_address(query, rows, p_settings)
                d = result_to_dict(r)
                if normalized_brief is None:
                    # Normalisation is profile-independent, so it is recorded
                    # once per query.
                    normalized_brief = {
                        "text": d["normalized"]["text"],
                        "house": d["normalized"]["house"],
                    }
                outcome = {
                    "profile": p["name"],
                    "index": len(outcomes),
                    "status": d["status"],
                    "reason": d["reason"],
                    "best_id": d["best_id"],
                    "best_score": d["best_score"],
                    "runner_up_id": d["runner_up_id"],
                    "runner_up_score": d["runner_up_score"],
                    "margin": d["margin"],
                    "triggered": d["triggered"],
                    "candidates": [
                        _slim_candidate(c, rank)
                        for rank, c in enumerate(d["candidates"])
                    ],
                }
                outcomes.append(outcome)
                status_counts[p["name"]][d["status"]] += 1
                if d["triggered"]:
                    triggered_counts[p["name"]][d["triggered"]] += 1

            baseline = outcomes[0]
            conclusions_consistent = all(
                o["status"] == baseline["status"] and o["best_id"] == baseline["best_id"]
                for o in outcomes[1:]
            )
            if not conclusions_consistent:
                disagreement_count += 1
            inconsistent_profiles = [
                o["profile"] for o in outcomes[1:]
                if o["status"] != baseline["status"]
                or o["best_id"] != baseline["best_id"]
            ]
            for o in outcomes:
                # The baseline row is marked too: its conclusion conflicts
                # with at least one other profile.
                o["conclusion_differs"] = (
                    not conclusions_consistent
                    and (
                        o["profile"] == baseline["profile"]
                        or o["profile"] in inconsistent_profiles
                    )
                )
                o["candidate_diff"] = (
                    None if o["index"] == 0
                    else _candidate_diff(baseline["candidates"], o["candidates"])
                )

            results.append({
                "query_index": q_index,
                "query": query,
                "normalized": normalized_brief,
                "conclusions_consistent": conclusions_consistent,
                "inconsistent_profiles": inconsistent_profiles,
                "outcomes": outcomes,
            })

        summary = {
            "queries": len(queries),
            "profiles": len(profiles),
            "disagreements": disagreement_count,
            "consensus_conclusions": len(queries) - disagreement_count,
            "status_counts": status_counts,
            "triggered_counts": triggered_counts,
        }

        experiment_id = db.insert_rule_experiment(
            conn,
            name=valid["name"],
            table_snapshot=snapshot,
            queries=queries,
            profiles=profiles,
            results=results,
            summary=summary,
        )

    return serialize_experiment(db.get_rule_experiment(conn, experiment_id), conn)


def serialize_experiment(row: sqlite3.Row,
                         conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Turn a stored experiment row into the API representation.

    When ``conn`` is given, the live address-table snapshot is compared with
    the one captured at run time so callers know whether replay is still
    against the same table.
    """
    snapshot_current = (
        db.address_table_snapshot(conn) if conn is not None else None
    )
    snapshot_stored = row["table_snapshot"]
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "query_count": row["query_count"],
        "profile_count": row["profile_count"],
        "table_snapshot": json.loads(snapshot_stored),
        "snapshot_current": snapshot_current == snapshot_stored
        if snapshot_current is not None
        else None,
        "profiles": json.loads(row["profiles_json"]),
        "queries": json.loads(row["queries_json"]),
        "summary": json.loads(row["summary_json"]),
        "results": json.loads(row["results_json"]),
    }


def serialize_experiment_summary(row: sqlite3.Row,
                                 conn: sqlite3.Connection | None = None
                                 ) -> dict[str, Any]:
    """List-view variant without the (potentially large) per-query results."""
    snapshot_current = (
        db.address_table_snapshot(conn) if conn is not None else None
    )
    snapshot_stored = row["table_snapshot"]
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "query_count": row["query_count"],
        "profile_count": row["profile_count"],
        "table_snapshot": json.loads(snapshot_stored),
        "snapshot_current": snapshot_current == snapshot_stored
        if snapshot_current is not None
        else None,
        "profiles": json.loads(row["profiles_json"]),
        "summary": json.loads(row["summary_json"]),
    }
