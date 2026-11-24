"""Settings loaded from environment / .env (no external dependency)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FALLBACKS = "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.split(" #", 1)[0]
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    owner_name: str
    profile_text: str
    triage_model: str
    draft_model: str
    max_attempts: int
    gmail_query: str
    max_fetch: int
    body_chars: int
    triage_chars: int
    triage_batch: int
    review_port: int
    review_timeout_min: int
    workers: int
    draft_fallbacks: tuple[str, ...]
    lite_after_hour: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def credentials_path(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"


def get_settings(data_dir: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    env = os.getenv
    d = Path(data_dir or env("AGENT_DATA_DIR") or ROOT / "data")
    d.mkdir(parents=True, exist_ok=True)
    profile_path = Path(env("PROFILE_PATH") or ROOT / "profile.md")
    profile = profile_path.read_text(encoding="utf-8").strip() if profile_path.exists() else ""
    return Settings(
        data_dir=d,
        owner_name=env("OWNER_NAME", "the owner"),
        profile_text=profile,
        triage_model=env("TRIAGE_MODEL", "gemini-flash-lite-latest"),
        draft_model=env("DRAFT_MODEL", "gemini-flash-latest"),
        max_attempts=int(env("MAX_ATTEMPTS", "3")),
        # Gmail search: primary inbox, last 2 days, not from me. Dedup is done in the DB.
        gmail_query=env("GMAIL_QUERY", "in:inbox category:primary newer_than:2d -from:me"),
        max_fetch=int(env("MAX_FETCH", "50")),
        body_chars=int(env("BODY_CHARS", "2500")),
        triage_chars=int(env("TRIAGE_CHARS", "500")),
        triage_batch=int(env("TRIAGE_BATCH", "20")),
        review_port=int(env("REVIEW_PORT", "8765")),
        review_timeout_min=int(env("REVIEW_TIMEOUT_MIN", "300")),
        workers=int(env("WORKERS", "6")),
        # Tried in order when the draft model is busy / over quota. "*lite*" models are a last
        # resort: before LITE_AFTER_HOUR the draft is postponed to the next hourly run instead.
        draft_fallbacks=tuple(m.strip() for m in env("DRAFT_FALLBACKS", DEFAULT_FALLBACKS if env("DRAFT_MODEL", "gemini").startswith("gemini") else "").split(",") if m.strip()),
        lite_after_hour=int(env("LITE_AFTER_HOUR", "17")),
    )
