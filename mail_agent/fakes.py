"""Offline stand-ins for Gmail and OpenAI: used by `demo` and by the tests."""
from __future__ import annotations

SAMPLE_EMAILS = [
    {"id": "m1", "thread_id": "t1", "from": "Yossi Levi <yossi@example.com>", "subject": "פגישה בשבוע הבא",
     "body": "שלום, אפשר לקבוע פגישה בשבוע הבא לגבי הצעת המחיר? אילו ימים נוחים לך?", "labels": ["INBOX"]},
    {"id": "m2", "thread_id": "t2", "from": "Dana Supplier <orders@supplier.example>", "subject": "Invoice #4471",
     "body": "Hi, could you confirm the delivery address for invoice #4471 before Thursday?", "labels": ["INBOX"]},
    {"id": "m3", "thread_id": "t3", "from": "Weekly Digest <no-reply@news.example>", "subject": "Your weekly digest",
     "body": "Top stories this week…", "labels": ["INBOX"], "flags": {"list_unsubscribe": "<mailto:x>"}},
    {"id": "m4", "thread_id": "t4", "from": "Avi Cohen <avi@example.com>", "subject": "תודה",
     "body": "תודה רבה, קיבלתי.", "labels": ["INBOX"]},
]


def _complete(e: dict) -> dict:
    base = {"reply_to": None, "date": "", "rfc_message_id": f"<{e['id']}@example>", "references": None,
            "is_reply": False, "attachments": [], "thread_context": [], "flags": {}}
    return {**base, **e}


class FakeGmail:
    def __init__(self, emails=None):
        self.emails = [_complete(e) for e in (emails if emails is not None else SAMPLE_EMAILS)]
        self.sent: list[dict] = []

    def my_address(self) -> str:
        return "me@example.com"

    def list_message_ids(self, query, max_results):
        return [e["id"] for e in self.emails][:max_results]

    def get_messages(self, ids):
        return [dict(e) for e in self.emails if e["id"] in ids]

    def send_reply(self, draft):
        self.sent.append(draft)
        return f"sent-{len(self.sent)}"


class FakeLLM:
    def __init__(self):
        self.calls = {"triage": 0, "draft": 0, "redraft": 0}

    def triage(self, emails):
        self.calls["triage"] += 1
        out = {}
        for e in emails:
            short_thanks = len(e["body"]) < 25 and "תודה" in e["body"]
            out[e["id"]] = {"id": e["id"], "needs_reply": not short_thanks,
                            "priority": "high" if "Thursday" in e["body"] else "normal"}
        return out

    def draft(self, email):
        self.calls["draft"] += 1
        name = email["from"].split("<")[0].strip()
        return f"שלום {name},\n\nתודה על פנייתך. [טיוטת דמו]\n\nבברכה,\nהבעלים"

    def redraft(self, draft, instruction, history):
        self.calls["redraft"] += 1
        return f"{draft['body']}\n\n[עודכן לפי ההערה: {instruction}]"
