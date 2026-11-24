"""Tests that need only the standard library."""
import tempfile
import unittest
from pathlib import Path

from mail_agent.db import DB
from mail_agent.fakes import SAMPLE_EMAILS, _complete
from mail_agent.text_utils import automated_reason, html_to_text, strip_quoted, truncate


class TextUtils(unittest.TestCase):
    def test_strip_gmail_quote_two_lines(self):
        t = "Sounds good, Tuesday works.\n\nOn Mon, Nov 23, 2026 at 10:00 Dana <d@x.com>\nwrote:\n> old text"
        self.assertEqual(strip_quoted(t), "Sounds good, Tuesday works.")

    def test_strip_gmail_quote_one_line(self):
        t = "מעולה, תודה\nOn Mon, Nov 23, 2026 at 10:00 Dana <d@x.com> wrote:\n> old"
        self.assertEqual(strip_quoted(t), "מעולה, תודה")

    def test_strip_hebrew_quote_and_outlook(self):
        self.assertEqual(strip_quoted("כן\nבתאריך יום ב׳, 23 בנוב׳ 2026, דנה כתבה:\nישן"), "כן")
        self.assertEqual(strip_quoted("OK\n\nFrom: Dana\nSent: Monday\nold"), "OK")

    def test_strip_signature_and_gt_lines(self):
        self.assertEqual(strip_quoted("Hi\n> quoted\nThanks\n-- \nDana\nCEO"), "Hi\nThanks")

    def test_from_line_in_body_is_kept(self):
        self.assertIn("From: the team", strip_quoted("Hello\nFrom: the team we heard\nmore"))

    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>a</p><style>x{}</style><div>b</div>").split(), ["a", "b"])

    def test_truncate(self):
        self.assertTrue(truncate("word " * 100, 20).endswith("[…]"))
        self.assertEqual(truncate("short", 20), "short")

    def test_prefilter(self):
        e = [_complete(x) for x in SAMPLE_EMAILS]
        self.assertIsNone(automated_reason(e[0], "me@example.com"))
        self.assertEqual(automated_reason(e[2], "me@example.com"), "noreply")
        self.assertEqual(automated_reason({**e[0], "from": "Me <ME@example.com>"}, "me@example.com"), "self")
        self.assertEqual(automated_reason({**e[0], "flags": {"auto_submitted": "auto-replied"}}, ""), "auto")


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_draft_lifecycle(self):
        e = _complete(SAMPLE_EMAILS[0])
        did = self.db.insert_draft(e, "body", "high")
        self.assertEqual(self.db.insert_draft(e, "again"), did)  # idempotent
        self.assertEqual(self.db.processed_ids(["m1", "m2"]), {"m1"})
        d = self.db.get_draft(did)
        self.assertEqual(d["subject"], "Re: פגישה בשבוע הבא")
        self.db.add_feedback(did, "shorter", edited=True)
        self.db.update_body(did, "new", new_attempt=True)
        d = self.db.get_draft(did)
        self.assertEqual((d["attempts"], d["body"]), (2, "new"))
        self.db.set_status(did, "sent")
        self.assertEqual(self.db.pending_drafts(), [])
        self.assertIsNotNone(self.db.get_draft(did)["sent_at"])

    def test_usage(self):
        self.db.add_usage("m", 100, 50, 10)
        self.db.add_usage("m", 100, 0, 10)
        row = self.db.usage_report()[0]
        self.assertEqual((row["calls"], row["prompt"], row["cached"]), (2, 200, 50))


if __name__ == "__main__":
    unittest.main()
