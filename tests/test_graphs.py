"""End-to-end graph tests with fake Gmail/LLM (requires langgraph)."""
import tempfile
import unittest
from pathlib import Path

try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    HAVE_LANGGRAPH = True
except ImportError:  # pragma: no cover
    HAVE_LANGGRAPH = False


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class GraphFlow(unittest.TestCase):
    def setUp(self):
        from mail_agent.__main__ import Deps
        from mail_agent.config import get_settings
        from mail_agent.db import DB
        from mail_agent.fakes import FakeGmail, FakeLLM

        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings(Path(self.tmp.name))
        self.deps = Deps(s, DB(s.db_path), FakeGmail(), FakeLLM())

    def tearDown(self):
        self.deps.db.conn.close()
        self.tmp.cleanup()

    def _pending(self, graph, cfg):
        snap = graph.get_state(cfg)
        return next((t.interrupts[0].value for t in snap.tasks if t.interrupts), None)

    def test_ingest_filters_and_drafts(self):
        from mail_agent.graphs import build_ingest_graph
        g = build_ingest_graph(self.deps)
        out = g.invoke({})
        self.assertEqual(len(out["drafted"]), 2)          # m1, m2
        self.assertEqual(out["skipped"], 1)                # newsletter pre-filtered, no LLM
        self.assertEqual(self.deps.llm.calls["triage"], 1)  # one batched call
        self.assertEqual(g.invoke({}).get("drafted", []), [])  # nothing re-processed

    def test_review_loop_approve_reject_until_manual(self):
        from mail_agent.graphs import build_ingest_graph, build_review_graph
        build_ingest_graph(self.deps).invoke({})
        g = build_review_graph(self.deps, InMemorySaver())
        cfg = {"configurable": {"thread_id": "t"}}

        g.invoke({}, cfg)
        p = self._pending(g, cfg)
        ids = [d["id"] for d in p["drafts"]]
        a, b = ids
        # round 1: approve A with a manual edit, reject B with edit + comment
        g.invoke(Command(resume={
            str(a): {"action": "approve", "text": "edited by me"},
            str(b): {"action": "reject", "text": "my edit", "comment": "shorter"},
        }), cfg)
        self.assertEqual(self.deps.gmail.sent[0]["body"], "edited by me")
        p = self._pending(g, cfg)
        self.assertEqual((p["round"], [d["id"] for d in p["drafts"]]), (2, [b]))
        self.assertIn("my edit", p["drafts"][0]["body"])      # manual edit kept as base
        self.assertEqual(p["drafts"][0]["attempt"], 2)

        # rounds 2 and 3: reject again → after attempt 3 it becomes manual
        g.invoke(Command(resume={str(b): {"action": "reject", "comment": "more formal"}}), cfg)
        self.assertEqual(self._pending(g, cfg)["drafts"][0]["attempt"], 3)
        g.invoke(Command(resume={str(b): {"action": "reject", "comment": "no"}}), cfg)
        self.assertIsNone(self._pending(g, cfg))
        self.assertEqual(self.deps.db.get_draft(b)["status"], "manual")
        self.assertEqual(self.deps.llm.calls["redraft"], 2)
        stats = g.get_state(cfg).values["stats"]
        self.assertEqual((stats["sent"], stats["manual"], stats["redrafted"]), (1, 1, 2))

    def test_deferred_and_invalid_decisions_stay_pending(self):
        from mail_agent.graphs import build_ingest_graph, build_review_graph
        build_ingest_graph(self.deps).invoke({})
        g = build_review_graph(self.deps, InMemorySaver())
        cfg = {"configurable": {"thread_id": "t2"}}
        g.invoke({}, cfg)
        g.invoke(Command(resume={"999": {"action": "approve"}, "x": 1}), cfg)
        self.assertIsNone(self._pending(g, cfg))
        self.assertEqual(len(self.deps.db.pending_drafts()), 2)
        self.assertEqual(self.deps.gmail.sent, [])


if __name__ == "__main__":
    unittest.main()
