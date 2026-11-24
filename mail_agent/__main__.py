"""CLI:
  python -m mail_agent auth      one-time Gmail sign-in
  python -m mail_agent ingest    fetch → triage → draft (hourly task)
  python -m mail_agent review    ingest, then the 18:00 approval round (daily task)
  python -m mail_agent demo      full flow offline, with fake Gmail/LLM
  python -m mail_agent stats     token usage and draft counts
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import ROOT, Settings, get_settings
from .db import DB

log = logging.getLogger("mail_agent")


@dataclass
class Deps:
    settings: Settings
    db: DB
    gmail: object
    llm: object


def setup_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(settings.data_dir / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "googleapiclient", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_deps(settings: Settings, demo: bool) -> Deps:
    db = DB(settings.db_path)
    if demo:
        from .fakes import FakeGmail, FakeLLM
        return Deps(settings, db, FakeGmail(), FakeLLM())
    from .gmail_client import GmailClient
    from .llm import LLM
    return Deps(settings, db, GmailClient(settings), LLM(settings, db))


def run_ingest(deps: Deps) -> list[int]:
    from .graphs import build_ingest_graph
    graph = build_ingest_graph(deps)
    result = graph.invoke({}, {"max_concurrency": deps.settings.workers})
    return result.get("drafted", [])


def run_review(deps: Deps, resume: bool) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    from .graphs import build_review_graph
    from .notify import notify
    from .review_server import ReviewSession, serve_until_done

    s = deps.settings
    saver = SqliteSaver(sqlite3.connect(str(s.checkpoint_path), check_same_thread=False))
    graph = build_review_graph(deps, saver)

    thread_id = deps.db.get_meta("last_review_thread") if resume else None
    if not thread_id:
        thread_id = f"review-{datetime.now():%Y%m%d-%H%M%S}"
        deps.db.set_meta("last_review_thread", thread_id)

    session = ReviewSession(graph, thread_id, s.workers)
    first = session.start(resume=resume)
    if first["status"] == "done":
        log.info("nothing to review")
        return
    n = len(first["drafts"])
    serve_until_done(
        session, s.review_port, s.review_timeout_min,
        on_ready=lambda url: notify("טיוטות מייל ממתינות", f"{n} טיוטות מחכות לאישור שלך", url),
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mail_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    sub.add_parser("ingest")
    r = sub.add_parser("review")
    r.add_argument("--resume", action="store_true", help="continue the last unfinished review round")
    r.add_argument("--skip-ingest", action="store_true")
    sub.add_parser("demo")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    if args.cmd == "demo":
        demo_dir = ROOT / "data" / "demo"
        shutil.rmtree(demo_dir, ignore_errors=True)
        settings = get_settings(demo_dir)
    else:
        settings = get_settings()
    setup_logging(settings)

    if args.cmd == "stats":
        db = DB(settings.db_path)
        print("drafts:", db.count_by_status())
        for row in db.usage_report():
            print(row)
        return

    deps = build_deps(settings, demo=args.cmd == "demo")
    if args.cmd == "auth":
        print("Signed in as", deps.gmail.my_address())
    elif args.cmd == "ingest":
        print("new drafts:", len(run_ingest(deps)))
    elif args.cmd in ("review", "demo"):
        if not getattr(args, "skip_ingest", False):
            try:
                run_ingest(deps)
            except Exception:
                log.exception("ingest before review failed; reviewing what is already pending")
        run_review(deps, resume=getattr(args, "resume", False))


if __name__ == "__main__":
    main()
