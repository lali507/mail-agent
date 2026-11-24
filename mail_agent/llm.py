"""LLM calls. Small model for batched triage, strong model for writing.
Uses the OpenAI SDK directly (no LangChain wrapper). Gemini models ("gemini-*") go through
Google's OpenAI-compatible endpoint, so the same code serves both providers."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from . import prompts

log = logging.getLogger(__name__)


class TriageItem(BaseModel):
    id: str
    needs_reply: bool
    priority: Literal["high", "normal", "low"] = "normal"


class TriageBatch(BaseModel):
    items: list[TriageItem] = Field(default_factory=list)


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Gemini models "think" before answering and those tokens count toward the output limit,
# so give them headroom on top of the visible answer budget.
GEMINI_THINKING_HEADROOM = 2048


# Errors that mean "this model can't serve us right now", so the next model in the chain is tried:
# 404 retired model, 429 quota, 503 overloaded, 529 overloaded.
SKIP_TO_NEXT = (404, 429, 503, 529)


class DraftDeferred(RuntimeError):
    """Only lite models were available and it is still early: try again on the next run."""
    deferred = True


def _is_lite(model: str) -> bool:
    return "lite" in model


def _is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def _is_reasoning(model: str) -> bool:
    return model.startswith(("o1", "o3", "o4", "gpt-5"))


def _gemini_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


class LLM:
    def __init__(self, settings, db):
        from openai import OpenAI

        self.s = settings
        self.db = db
        self._clients: dict[str, OpenAI] = {}
        self._chain = list(dict.fromkeys([settings.draft_model, *settings.draft_fallbacks]))
        for model in {settings.triage_model, *self._chain}:
            provider = "gemini" if _is_gemini(model) else "openai"
            if provider in self._clients:
                continue
            if provider == "gemini":
                key = _gemini_key()
                if not key:
                    raise RuntimeError("Gemini model selected but GEMINI_API_KEY is not set in .env")
                self._clients[provider] = OpenAI(api_key=key, base_url=GEMINI_BASE_URL, max_retries=1, timeout=60)
            else:
                self._clients[provider] = OpenAI(max_retries=3, timeout=60)
        self._triage_sys = prompts.TRIAGE_SYSTEM.format(owner=settings.owner_name)
        self._draft_sys = prompts.DRAFT_SYSTEM.format(
            owner=settings.owner_name, profile=settings.profile_text or "(no profile given)")

    # -- helpers --
    def _client(self, model: str):
        return self._clients["gemini" if _is_gemini(model) else "openai"]

    def _record(self, model: str, usage) -> None:
        if not usage:
            return
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        self.db.add_usage(model, usage.prompt_tokens, cached, usage.completion_tokens)

    def _kwargs(self, model: str, max_tokens: int, temperature: float) -> dict:
        if _is_gemini(model):
            return {"model": model, "max_tokens": max_tokens + GEMINI_THINKING_HEADROOM,
                    "temperature": temperature, "reasoning_effort": "low"}
        kw = {"model": model, "max_completion_tokens": max_tokens}
        if not _is_reasoning(model):
            kw["temperature"] = temperature
        return kw

    def _text(self, user: str, max_tokens: int = 700, allow_lite: bool = True) -> str:
        """Try the draft model, then each fallback. Lite models only if allow_lite."""
        from openai import APIConnectionError, APIStatusError

        messages = [{"role": "system", "content": self._draft_sys}, {"role": "user", "content": user}]
        last_exc: Exception | None = None
        skipped_lite = False
        for model in self._chain:
            if _is_lite(model) and not allow_lite:
                skipped_lite = True
                continue
            try:
                resp = self._client(model).chat.completions.create(
                    messages=messages, **self._kwargs(model, max_tokens, 0.3))
            except APIStatusError as exc:
                if exc.status_code not in SKIP_TO_NEXT:
                    raise
                log.warning("%s unavailable (%s), trying next model", model, exc.status_code)
                last_exc = exc
                continue
            except APIConnectionError as exc:  # includes timeouts: a hung model, move on
                log.warning("%s did not respond (%s), trying next model", model, type(exc).__name__)
                last_exc = exc
                continue
            if model != self._chain[0]:
                log.info("drafted with fallback model %s", model)
            self._record(model, resp.usage)
            return (resp.choices[0].message.content or "").strip()
        if skipped_lite:
            raise DraftDeferred("strong models busy; postponed to the next run")
        raise last_exc or RuntimeError("no draft model available")

    # -- public API --
    def triage(self, emails: list[dict]) -> dict[str, dict]:
        """Classify many emails per call. Short numeric keys instead of Gmail ids save tokens."""
        model = self.s.triage_model
        client = self._client(model)
        parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
        out: dict[str, dict] = {}
        n = self.s.triage_batch
        for start in range(0, len(emails), n):
            chunk = emails[start:start + n]
            keyed = {str(i + 1): e for i, e in enumerate(chunk)}
            user = prompts.triage_user([(k, e, self.s.triage_chars) for k, e in keyed.items()])
            resp = parse(
                messages=[{"role": "system", "content": self._triage_sys}, {"role": "user", "content": user}],
                response_format=TriageBatch,
                **self._kwargs(model, 40 + 30 * len(chunk), 0.0),
            )
            self._record(model, resp.usage)
            parsed = resp.choices[0].message.parsed or TriageBatch()
            for item in parsed.items:
                if item.id in keyed:
                    out[keyed[item.id]["id"]] = item.model_dump()
        return out

    def draft(self, email: dict) -> str:
        # Early in the day, wait for a strong model; from LITE_AFTER_HOUR accept a lite one
        # so every email has a draft by the 18:00 review.
        allow_lite = datetime.now().hour >= self.s.lite_after_hour
        return self._text(prompts.draft_user(email), allow_lite=allow_lite)

    def redraft(self, draft: dict, instruction: str, history: list[str]) -> str:
        return self._text(prompts.redraft_user(draft, instruction, history))
