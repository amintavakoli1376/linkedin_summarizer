"""
Small, dependency-light helper functions shared across the application.

These utilities are intentionally pure (no I/O side effects beyond the
explicit ``cleanup_file``) so they are trivial to unit test.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Final
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Hosts considered "LinkedIn". The feed, posts, and articles all live under
# linkedin.com (and the ``feed`` subpath variants).
_LINKEDIN_HOSTS: Final = {"linkedin.com", "www.linkedin.com"}

# Matches a LinkedIn post/activity, feed update, or pulse article URL.
_LINKEDIN_PATH_RE: Final = re.compile(
    r"^/(posts|feed|pulse|in/[^/]+/(posts|activity|recent-activity)|"
    r"company/[^/]+|events)/?.*",
    re.IGNORECASE,
)


def is_valid_linkedin_url(url: str) -> bool:
    """Return ``True`` if ``url`` looks like a public LinkedIn post URL.

    Accepts the common shapes produced by the LinkedIn share button::

        https://www.linkedin.com/posts/...1234.../
        https://www.linkedin.com/feed/update/urn:li:activity:...
        https://www.linkedin.com/pulse/.../
        https://www.linkedin.com/in/<user>/posts/.../

    A bare ``https://linkedin.com`` (no path) is rejected.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    if (parsed.hostname or "").lower() not in _LINKEDIN_HOSTS:
        return False
    if not parsed.path or parsed.path == "/":
        return False

    return bool(_LINKEDIN_PATH_RE.match(parsed.path))


def normalize_url(url: str) -> str:
    """Return a cleaned-up URL (https, trailing slash trimmed).

    Strips tracking query params that LinkedIn appends to shared links so the
    scraper sees the canonical post.
    """
    parsed = urlparse(url.strip())
    scheme = "https"
    netloc = parsed.netloc.lower()
    if netloc and not netloc.startswith("www.") and netloc == "linkedin.com":
        netloc = "www." + netloc

    # Drop noisy tracking parameters; keep everything else.
    tracking_prefixes = ("utm_", "li_", "trk", "trackingId", "sid", "src")
    kept_query_pairs = [
        (k, v)
        for k, v in _parse_qsl(parsed.query)
        if not any(k.lower().startswith(p) for p in tracking_prefixes)
    ]
    query = "&".join(f"{k}={v}" for k, v in kept_query_pairs)

    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def _parse_qsl(query: str):
    """Lightweight ``urllib.parse.parse_qsl`` replacement (no extra imports)."""
    if not query:
        return []
    pairs = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            key, val = chunk.split("=", 1)
        else:
            key, val = chunk, ""
        pairs.append((key, val))
    return pairs


# --- Text helpers ---------------------------------------------------------

# Markdown bullet markers (-, *, •) possibly leading whitespace.
_BULLET_RE: Final = re.compile(r"^\s*[-*•▪◦‣]\s+", re.MULTILINE)
# Headers (## Title), bold (**text**), italic (_text_), inline code (`code`).
_MD_HEADER_RE: Final = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BOLD_RE: Final = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE: Final = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_MD_CODE_RE: Final = re.compile(r"`([^`]+)`")
# Collapse 3+ newlines to a single paragraph break.
_MULTIBLANK_RE: Final = re.compile(r"\n{3,}")
# Strip emojis (broad emoji unicode block) for cleaner narration.
_EMOJI_RE: Final = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def strip_markdown(text: str) -> str:
    """Remove Markdown formatting so a sentence reads naturally when spoken."""
    if not text:
        return ""
    out = _MD_HEADER_RE.sub("", text)
    out = _BULLET_RE.sub("", out)
    out = _MD_BOLD_RE.sub(r"\1", out)
    out = _MD_ITALIC_RE.sub(r"\1", out)
    out = _MD_CODE_RE.sub(r"\1", out)
    out = _EMOJI_RE.sub("", out)
    out = _MULTIBLANK_RE.sub("\n\n", out)
    return out.strip()


def text_for_tts(summary: str) -> str:
    """Prepare an AI summary for narration.

    Removes bullets/headers, drops pure decoration, and converts remaining
    dashes to natural pauses (commas) so the TTS engine reads fluidly.
    """
    clean = strip_markdown(summary)
    # Convert standalone line breaks into sentence-ending pauses so the
    # narration sounds like paragraphs rather than a run-on list.
    clean = re.sub(r"[ \t]*\n[ \t]*", ". ", clean)
    clean = re.sub(r"\.{2,}", ".", clean)  # collapse accidental ellipses
    clean = re.sub(r"\s{2,}", " ", clean)
    return clean.strip()


def cleanup_file(path: str | Path) -> None:
    """Best-effort deletion of a temporary file; never raises."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
            logger.debug("Removed temp file %s", p)
    except Exception as exc:  # noqa: BLE001 — cleanup must never break flow
        logger.warning("Could not remove temp file %s: %s", path, exc)


def chunk_text(text: str, max_chars: int = 3000) -> list[str]:
    """Split ``text`` into chunks no longer than ``max_chars`` on boundaries.

    TTS / API providers often cap request size; this keeps splits on sentence
    boundaries where possible. Used by the TTS service for long summaries.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for sentence in re.split(r"(?<=[.!?؟])\s+", text):
        if buf_len + len(sentence) + 1 > max_chars and buf:
            chunks.append(" ".join(buf))
            buf, buf_len = [], 0
        buf.append(sentence)
        buf_len += len(sentence) + 1
    if buf:
        chunks.append(" ".join(buf))
    return chunks


def ensure_dir(path: str | Path) -> Path:
    """Create ``path`` (and parents) if it doesn't exist; return as ``Path``."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment flag (``true``/``1``/``yes``)."""
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}
