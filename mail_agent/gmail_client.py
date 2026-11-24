"""Gmail API adapter. Uses HTTP batch requests so fetching 50 mails is ~1 round-trip."""
from __future__ import annotations

import base64
import logging
from email.message import EmailMessage

from .text_utils import compact, html_to_text, strip_quoted, truncate

log = logging.getLogger(__name__)

# gmail.modify covers read + send (no delete). Nothing broader is requested.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict) -> str:
    for h in part.get("headers", []):
        if h["name"].lower() == "content-type" and "charset=" in h["value"].lower():
            return h["value"].lower().split("charset=")[1].split(";")[0].strip(' "\'') or "utf-8"
    return "utf-8"


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[str]) -> None:
    mime = part.get("mimeType", "")
    if part.get("filename"):
        attachments.append(part["filename"])
        return
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        try:
            text = _b64(data).decode(_charset(part), errors="replace")
        except LookupError:
            text = _b64(data).decode("utf-8", errors="replace")
        (plain if mime == "text/plain" else html).append(text)
    for sub in part.get("parts", []) or []:
        _walk(sub, plain, html, attachments)


def parse_message(m: dict, body_chars: int) -> dict:
    payload = m.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    _walk(payload, plain, html, attachments)
    raw = plain[0] if plain else (html_to_text(html[0]) if html else m.get("snippet", ""))
    body = truncate(strip_quoted(raw) or compact(raw), body_chars)
    return {
        "id": m["id"],
        "thread_id": m["threadId"],
        "from": headers.get("from", ""),
        "reply_to": headers.get("reply-to"),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "rfc_message_id": headers.get("message-id"),
        "references": headers.get("references") or headers.get("in-reply-to"),
        "is_reply": bool(headers.get("in-reply-to")),
        "labels": m.get("labelIds", []),
        "attachments": attachments,
        "body": body,
        "thread_context": [],
        "flags": {
            "list_unsubscribe": headers.get("list-unsubscribe"),
            "list_id": headers.get("list-id"),
            "precedence": headers.get("precedence"),
            "auto_submitted": headers.get("auto-submitted"),
        },
    }


class GmailClient:
    def __init__(self, settings):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        self.settings = settings
        creds = None
        if settings.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(settings.token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not settings.credentials_path.exists():
                    raise FileNotFoundError(
                        f"Missing {settings.credentials_path}. Download an OAuth 'Desktop app' client JSON "
                        "from Google Cloud Console and save it there.")
                flow = InstalledAppFlow.from_client_secrets_file(str(settings.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)
            settings.token_path.write_text(creds.to_json(), encoding="utf-8")
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._me: str | None = None

    def my_address(self) -> str:
        if self._me is None:
            self._me = self.svc.users().getProfile(userId="me").execute()["emailAddress"].lower()
        return self._me

    def list_message_ids(self, query: str, max_results: int) -> list[str]:
        resp = self.svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return [m["id"] for m in resp.get("messages", [])]

    def _batch(self, requests: list) -> list[dict]:
        results: dict[int, dict] = {}

        def cb(request_id, response, exception):
            if exception:
                log.warning("Gmail batch item failed: %s", exception)
            else:
                results[int(request_id)] = response

        for start in range(0, len(requests), 40):
            batch = self.svc.new_batch_http_request(callback=cb)
            for i, req in enumerate(requests[start:start + 40], start):
                batch.add(req, request_id=str(i))
            batch.execute()
        return [results[i] for i in sorted(results)]

    def get_messages(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        msgs = self.svc.users().messages()
        raw = self._batch([msgs.get(userId="me", id=i, format="full") for i in ids])
        emails = [parse_message(m, self.settings.body_chars) for m in raw]
        # For replies, add short snippets of earlier thread messages (format=minimal → cheap).
        replies = [e for e in emails if e["is_reply"]]
        if replies:
            threads = self.svc.users().threads()
            tdata = self._batch([threads.get(userId="me", id=e["thread_id"], format="minimal") for e in replies])
            by_id = {t["id"]: t for t in tdata}
            for e in replies:
                t = by_id.get(e["thread_id"], {})
                prev = [m.get("snippet", "") for m in t.get("messages", []) if m["id"] != e["id"]]
                e["thread_context"] = [s for s in prev[-2:] if s]
        return emails

    def send_reply(self, draft: dict) -> str:
        msg = EmailMessage()
        msg["To"] = draft["to_addr"]
        msg["Subject"] = draft["subject"]
        if draft.get("rfc_message_id"):
            msg["In-Reply-To"] = draft["rfc_message_id"]
            msg["References"] = f"{draft.get('references_hdr') or ''} {draft['rfc_message_id']}".strip()
        msg.set_content(draft["body"])
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = self.svc.users().messages().send(
            userId="me", body={"raw": raw, "threadId": draft["thread_id"]}).execute()
        return sent["id"]
