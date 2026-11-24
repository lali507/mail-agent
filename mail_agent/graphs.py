"""The two LangGraph graphs.

Ingest (runs hourly + before review):
    START → fetch ─(nothing new)→ END
                 └→ triage ─(nothing to answer)→ END
                           └→ draft_one ×N (parallel, Send) → END

Review (runs daily at 18:00), with human-in-the-loop and a bounded redraft loop:
    START → load_pending ─(empty)→ END
                         └→ human_review [interrupt] → apply_decisions
                               ├─(rejected & attempts<max)→ redraft_one ×N (parallel) → next_round → human_review  ⟲
                               └─(otherwise)→ finalize → END
"""
from __future__ import annotations

import json
import logging
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from .text_utils import automated_reason

log = logging.getLogger(__name__)

DEFAULT_POLISH = "Polish this draft. Keep the owner's edits and meaning."


def merge_stats(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


# ============================== INGEST ==============================

class IngestState(TypedDict, total=False):
    emails: list[dict]
    to_draft: list[dict]
    drafted: Annotated[list[int], operator.add]
    skipped: int


class DraftTask(TypedDict):
    email: dict


def build_ingest_graph(deps):
    db, gmail, llm, s = deps.db, deps.gmail, deps.llm, deps.settings

    def fetch(state: IngestState) -> dict:
        ids = gmail.list_message_ids(s.gmail_query, s.max_fetch)
        done = db.processed_ids(ids)
        emails = gmail.get_messages([i for i in ids if i not in done])
        me = gmail.my_address()
        keep, skipped = [], 0
        for e in emails:
            reason = automated_reason(e, me)
            if reason:
                db.mark_processed(e["id"], f"skip:{reason}")
                skipped += 1
            else:
                keep.append(e)
        log.info("fetch: %d listed, %d new, %d pre-filtered, %d to triage", len(ids), len(emails), skipped, len(keep))
        return {"emails": keep, "skipped": skipped}

    def triage(state: IngestState) -> dict:
        results = llm.triage(state["emails"])
        to_draft = []
        for e in state["emails"]:
            r = results.get(e["id"])
            if r is None:
                continue  # model skipped it: leave unprocessed, retried next run
            if r["needs_reply"]:
                to_draft.append({**e, "priority": r["priority"]})
            else:
                db.mark_processed(e["id"], "no_reply")
        log.info("triage: %d need a reply", len(to_draft))
        return {"to_draft": to_draft}

    def fan_out(state: IngestState):
        if not state.get("to_draft"):
            return END
        return [Send("draft_one", {"email": e}) for e in state["to_draft"]]

    def draft_one(task: DraftTask) -> dict:
        e = task["email"]
        try:
            body = llm.draft(e)
            if not body:
                raise ValueError("empty draft")
        except Exception as ex:  # one failure must not sink the batch; retried next run
            if getattr(ex, "deferred", False):
                log.info("draft postponed for %s: %s", e["id"], ex)
            else:
                log.exception("draft failed for %s", e["id"])
            return {"drafted": []}
        return {"drafted": [db.insert_draft(e, body, e.get("priority", "normal"))]}

    g = StateGraph(IngestState)
    g.add_node("fetch", fetch)
    g.add_node("triage", triage)
    g.add_node("draft_one", draft_one)
    g.add_edge(START, "fetch")
    g.add_conditional_edges("fetch", lambda st: "triage" if st.get("emails") else END, ["triage", END])
    g.add_conditional_edges("triage", fan_out, ["draft_one", END])
    g.add_edge("draft_one", END)
    return g.compile()


# ============================== REVIEW ==============================

class ReviewState(TypedDict, total=False):
    queue: list[int]
    round: int
    decisions: dict
    to_redraft: list[int]
    stats: Annotated[dict, merge_stats]


class RedraftTask(TypedDict):
    draft_id: int


def draft_view(d: dict) -> dict:
    """What the reviewer sees (no Gmail ids, no headers)."""
    return {
        "id": d["id"], "from": d["orig_from"], "subject": d["orig_subject"],
        "excerpt": d["orig_excerpt"], "body": d["body"], "priority": d["priority"],
        "attempt": d["attempts"], "feedback": [f["comment"] for f in json.loads(d["feedback"]) if f["comment"]],
        "error": d.get("error"),
    }


def normalize_decisions(raw, valid_ids: set[int]) -> dict[str, dict]:
    """Accept only well-formed decisions; anything else means 'defer to tomorrow'."""
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for key, dec in raw.items():
        try:
            did = int(key)
        except (TypeError, ValueError):
            continue
        if did not in valid_ids or not isinstance(dec, dict):
            continue
        action = dec.get("action")
        if action not in ("approve", "reject"):
            continue
        out[str(did)] = {
            "action": action,
            "text": str(dec.get("text") or "")[:20000],
            "comment": str(dec.get("comment") or "").strip()[:2000],
        }
    return out


def build_review_graph(deps, checkpointer):
    db, gmail, llm, s = deps.db, deps.gmail, deps.llm, deps.settings

    def load_pending(state: ReviewState) -> dict:
        ids = [d["id"] for d in db.pending_drafts()]
        log.info("review: %d pending drafts", len(ids))
        return {"queue": ids, "round": 1, "stats": {}}

    def human_review(state: ReviewState) -> dict:
        drafts = [d for d in (db.get_draft(i) for i in state["queue"]) if d and d["status"] == "pending"]
        if not drafts:
            return {"decisions": {}}
        payload = {"round": state.get("round", 1), "max_attempts": s.max_attempts,
                   "drafts": [draft_view(d) for d in drafts]}
        raw = interrupt(payload)  # ← pauses here until the reviewer submits
        return {"decisions": normalize_decisions(raw, {d["id"] for d in drafts})}

    def apply_decisions(state: ReviewState) -> dict:
        decisions = state.get("decisions") or {}
        stats = {"sent": 0, "manual": 0, "deferred": 0, "send_failed": 0}
        to_redraft: list[int] = []
        for did in state["queue"]:
            d = db.get_draft(did)
            if not d or d["status"] != "pending":
                continue  # already handled (makes this node safe to re-run)
            dec = decisions.get(str(did))
            if not dec:
                stats["deferred"] += 1
                continue
            text = dec["text"].strip() or d["body"]
            edited = text != d["body"].strip()
            if edited:
                db.update_body(did, text, new_attempt=False)
            if dec["action"] == "approve":
                try:
                    gmail.send_reply({**d, "body": text})
                    db.set_status(did, "sent")
                    stats["sent"] += 1
                except Exception as ex:
                    log.exception("send failed for draft %s", did)
                    db.set_status(did, "pending", error=f"send failed: {ex}")
                    stats["send_failed"] += 1
                continue
            db.add_feedback(did, dec["comment"] or (DEFAULT_POLISH if edited else ""), edited)
            if d["attempts"] >= s.max_attempts:
                db.set_status(did, "manual")
                stats["manual"] += 1
            else:
                to_redraft.append(did)
        return {"to_redraft": to_redraft, "stats": stats}

    def route_after_apply(state: ReviewState):
        if state.get("to_redraft"):
            return [Send("redraft_one", {"draft_id": i}) for i in state["to_redraft"]]
        return "finalize"

    def redraft_one(task: RedraftTask) -> dict:
        d = db.get_draft(task["draft_id"])
        fb = json.loads(d["feedback"])
        instruction = (fb[-1]["comment"] if fb else "") or DEFAULT_POLISH
        history = [f["comment"] for f in fb[:-1] if f["comment"]]
        try:
            body = llm.redraft(d, instruction, history)
            if not body:
                raise ValueError("empty redraft")
            db.update_body(d["id"], body, new_attempt=True)
            return {"stats": {"redrafted": 1}}
        except Exception as ex:
            log.exception("redraft failed for %s", d["id"])
            db.set_status(d["id"], "pending", error=f"redraft failed: {ex}")
            return {"stats": {"redraft_failed": 1}}

    def next_round(state: ReviewState) -> dict:
        return {"queue": state["to_redraft"], "round": state.get("round", 1) + 1,
                "decisions": {}, "to_redraft": []}

    def finalize(state: ReviewState) -> dict:
        log.info("review finished: %s", state.get("stats"))
        return {}

    g = StateGraph(ReviewState)
    g.add_node("load_pending", load_pending)
    g.add_node("human_review", human_review)
    g.add_node("apply_decisions", apply_decisions)
    g.add_node("redraft_one", redraft_one)
    g.add_node("next_round", next_round)
    g.add_node("finalize", finalize)
    g.add_edge(START, "load_pending")
    g.add_conditional_edges("load_pending", lambda st: "human_review" if st.get("queue") else END,
                            ["human_review", END])
    g.add_edge("human_review", "apply_decisions")
    g.add_conditional_edges("apply_decisions", route_after_apply, ["redraft_one", "finalize"])
    g.add_edge("redraft_one", "next_round")
    g.add_edge("next_round", "human_review")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
