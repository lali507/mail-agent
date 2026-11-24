"""Cheap, deterministic text processing. Everything here exists to send fewer tokens."""
from __future__ import annotations

import re
from email.utils import parseaddr
from html.parser import HTMLParser

# ---------- HTML -> text ----------

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "blockquote"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return "".join(p.parts)


# ---------- quoted history / signature removal ----------

_WROTE = re.compile(r"(wrote|כתב|כתבה)\s*:\s*$", re.I)
_ON_LINE = re.compile(r"^(On|בתאריך|ב-?\s?\d)", re.I)
_ORIGINAL = re.compile(r"^-{2,}\s*(Original Message|הודעה מקורית)\s*-{2,}", re.I)
_OUTLOOK_FROM = re.compile(r"^(From|מאת)\s*:", re.I)
_OUTLOOK_NEXT = re.compile(r"^(Sent|Date|To|נשלח|תאריך|אל)\s*:", re.I)
_SIG = re.compile(r"^(--\s*|Sent from my .+|Get Outlook for .+|נשלח מה.+)$", re.I)


def strip_quoted(text: str) -> str:
    """Keep only the newest message: drop quoted replies, '>' lines and signatures."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _ORIGINAL.match(line) or _SIG.match(line):
            break
        if _WROTE.search(line) and (_ON_LINE.match(line) or (out and _ON_LINE.match(out[-1].strip()))):
            if out and _ON_LINE.match(out[-1].strip()) and not _ON_LINE.match(line):
                out.pop()  # "On Mon ... <x@y>" wrapped onto two lines
            break
        if _OUTLOOK_FROM.match(line) and _OUTLOOK_NEXT.match(nxt):
            break
        if line.startswith(">"):
            continue
        out.append(raw.rstrip())
    return compact("\n".join(out))


def compact(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(None, 1)[0]
    return cut + " […]"


# ---------- automated-mail pre-filter (zero tokens) ----------

_NOREPLY = re.compile(
    r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce|notifications?@|alerts?@|newsletter)",
    re.I,
)
_SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "SPAM"}


def email_address(header_value: str) -> str:
    return parseaddr(header_value or "")[1].lower()


def automated_reason(msg: dict, my_address: str) -> str | None:
    """Return why a message should be skipped without calling the LLM, or None."""
    sender = email_address(msg.get("from", ""))
    h = msg.get("flags", {})
    if not sender:
        return "no-sender"
    if my_address and sender == my_address.lower():
        return "self"
    if _NOREPLY.search(sender):
        return "noreply"
    if h.get("list_unsubscribe") or h.get("list_id"):
        return "bulk"
    if (h.get("precedence") or "").lower() in {"bulk", "list", "junk"}:
        return "bulk"
    auto = (h.get("auto_submitted") or "no").lower()
    if auto != "no":
        return "auto"
    if _SKIP_LABELS & set(msg.get("labels", [])):
        return "category"
    return None
