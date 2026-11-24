"""Draft model chain: fallback order, lite deferral, and non-skippable errors."""
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx2
from openai import APIStatusError

from mail_agent.llm import LLM, DraftDeferred


def _err(code):
    req = httpx2.Request("POST", "https://x")
    return APIStatusError("err", response=httpx2.Response(code, request=req), body=None)


EMAIL = {"id": "1", "from": "dana@example.com", "subject": "hi", "body": "hello", "snippet": "", "thread_snippets": []}


def _ok(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


class ChainTest(unittest.TestCase):
    def make(self, behaviour, hour=10):
        s = SimpleNamespace(owner_name="Lea", profile_text="", triage_model="gemini-flash-lite-latest",
                            draft_model="gemini-flash-latest",
                            draft_fallbacks=("gemini-3.5-flash", "gemini-flash-lite-latest"),
                            lite_after_hour=17)
        calls = []

        def create(model, **kw):
            calls.append(model)
            r = behaviour[model]
            if isinstance(r, Exception):
                raise r
            return _ok(r)

        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}):
            llm = LLM(s, db=SimpleNamespace(add_usage=lambda *a: None))
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        llm._clients = {"gemini": fake}
        clock = mock.patch("mail_agent.llm.datetime")
        dt = clock.start()
        self.addCleanup(clock.stop)
        dt.now.return_value = SimpleNamespace(hour=hour)
        return llm, calls

    def test_falls_back_to_next_strong_model(self):
        llm, calls = self.make({"gemini-flash-latest": _err(503), "gemini-3.5-flash": "draft B",
                                "gemini-flash-lite-latest": "lite"})
        self.assertEqual(llm.draft(EMAIL), "draft B")
        self.assertEqual(calls, ["gemini-flash-latest", "gemini-3.5-flash"])

    def test_defers_when_only_lite_left_early_in_day(self):
        llm, calls = self.make({"gemini-flash-latest": _err(503), "gemini-3.5-flash": _err(429),
                                "gemini-flash-lite-latest": "lite"}, hour=10)
        with self.assertRaises(DraftDeferred):
            llm.draft(EMAIL)
        self.assertNotIn("gemini-flash-lite-latest", calls)

    def test_uses_lite_late_in_day(self):
        llm, _ = self.make({"gemini-flash-latest": _err(503), "gemini-3.5-flash": _err(503),
                            "gemini-flash-lite-latest": "lite"}, hour=17)
        self.assertEqual(llm.draft(EMAIL), "lite")

    def test_redraft_always_allows_lite(self):
        llm, _ = self.make({"gemini-flash-latest": _err(503), "gemini-3.5-flash": _err(503),
                            "gemini-flash-lite-latest": "lite"}, hour=9)
        d = {"orig_from": "a", "orig_subject": "s", "orig_excerpt": "x", "body": "b"}
        self.assertEqual(llm.redraft(d, "shorter", []), "lite")

    def test_timeout_moves_to_next_model(self):
        from openai import APITimeoutError
        llm, calls = self.make({"gemini-flash-latest": APITimeoutError(request=httpx2.Request("POST", "https://x")),
                                "gemini-3.5-flash": "B", "gemini-flash-lite-latest": "lite"})
        self.assertEqual(llm.draft(EMAIL), "B")

    def test_other_errors_are_raised(self):
        llm, calls = self.make({"gemini-flash-latest": _err(400), "gemini-3.5-flash": "B",
                                "gemini-flash-lite-latest": "lite"})
        with self.assertRaises(APIStatusError):
            llm.draft(EMAIL)
        self.assertEqual(calls, ["gemini-flash-latest"])


if __name__ == "__main__":
    unittest.main()
