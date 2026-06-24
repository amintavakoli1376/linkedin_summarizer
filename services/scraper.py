"""
LinkedIn post scraper.

Public LinkedIn content is aggressively protected against scraping, so this
module implements a layered strategy and returns the first usable result:

1. **Direct HTTP request** with rotating user-agents and realistic headers
   (fastest, works for many pulse articles and some public activity posts).
2. **JSON-LD / Open-Graph extraction** — LinkedIn embeds structured metadata
   that often contains the full post/article body even when the rendered DOM
   is gated.
3. **Playwright fallback** (optional) — a headless browser that renders the
   page fully, used when the static fetch returns a challenge page.

The public API is :class:`LinkedInScraper.fetch_text`, which returns the
plain-text content of the post suitable for summarization.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config.settings import settings

logger = logging.getLogger(__name__)


class ScrapeError(Exception):
    """Raised when post content cannot be retrieved by any strategy."""


@dataclass(frozen=True)
class ScrapeResult:
    """Outcome of a scraping attempt."""

    text: str
    title: str
    source: str  # which strategy succeeded ("direct", "metadata", "playwright")


class LinkedInScraper:
    """Scrape the main text content of a public LinkedIn post."""

    def __init__(
        self,
        timeout: int = settings.scrape_timeout,
        max_retries: int = settings.scrape_max_retries,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._user_agents = list(settings.user_agents)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def fetch_text(self, url: str) -> ScrapeResult:
        """Return the post's main text, trying each strategy in turn.

        Args:
            url: Canonical LinkedIn post/article URL.

        Returns:
            :class:`ScrapeResult` with at least 200 characters of body text.

        Raises:
            ScrapeError: if every strategy fails or yields too little text.
        """
        last_error: Optional[Exception] = None

        # Strategy 1 + 2: direct HTTP with metadata extraction.
        try:
            result = self._fetch_with_requests(url)
            if self._is_meaningful(result.text):
                logger.info("Scraped via requests (%s): %d chars",
                            result.source, len(result.text))
                return result
            logger.info("Direct fetch yielded thin content (%d chars); "
                        "trying playwright.", len(result.text))
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Direct HTTP scrape failed: %s", exc)

        # Strategy 3: headless browser (optional, may not be installed).
        try:
            result = self._fetch_with_playwright(url)
            if self._is_meaningful(result.text):
                logger.info("Scraped via playwright: %d chars",
                            len(result.text))
                return result
        except ScrapeError as exc:
            last_error = exc
            logger.warning("Playwright scrape failed/unavailable: %s", exc)

        raise ScrapeError(
            "Could not retrieve LinkedIn post content. The post may be "
            "private, deleted, or behind a login wall."
        ) from last_error

    # ------------------------------------------------------------------ #
    # Strategy 1 + 2: requests + BeautifulSoup
    # ------------------------------------------------------------------ #
    def _fetch_with_requests(self, url: str) -> ScrapeResult:
        """Fetch the URL with retries, rotating UA, and extract content."""
        html = self._http_get_with_retries(url)

        # LinkedIn sometimes returns a challenge/interstitial page.
        if self._looks_like_challenge(html):
            raise ScrapeError("Received anti-bot challenge page.")

        soup = BeautifulSoup(html, "html.parser")

        # Prefer structured metadata — most reliable for articles/posts.
        meta = self._extract_from_metadata(soup)
        if self._is_meaningful(meta.text):
            return meta

        # Fall back to DOM heuristics.
        text = self._extract_from_dom(soup)
        title = self._extract_title(soup)
        return ScrapeResult(text=text, title=title, source="direct")

    def _http_get_with_retries(self, url: str) -> str:
        """GET ``url`` with backoff retries and a rotating User-Agent."""
        headers = self._build_headers()
        last_exc: Optional[requests.RequestException] = None

        for attempt in range(1, self._max_retries + 1):
            headers["User-Agent"] = random.choice(self._user_agents)
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    allow_redirects=True,
                )
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8)
                logger.debug("GET attempt %d/%d failed: %s (retry in %ds)",
                             attempt, self._max_retries, exc, wait)
                import time
                time.sleep(wait)

        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _build_headers() -> dict:
        """Headers that mimic a real browser visit to LinkedIn."""
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    # ------------------------------------------------------------------ #
    # Extraction helpers
    # ------------------------------------------------------------------ #
    def _extract_from_metadata(self, soup: BeautifulSoup) -> ScrapeResult:
        """Pull post content from JSON-LD / OG / twitter meta tags."""
        # 1. JSON-LD blocks (Articles, SocialMediaPosting).
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or ""
            article_body = _find_in_jsonld(raw, ("articleBody", "text",
                                                 "caption", "description"))
            if article_body and self._is_meaningful(article_body):
                title = _find_in_jsonld(raw, ("headline", "name")) or ""
                return ScrapeResult(text=article_body, title=title,
                                    source="metadata")

        # 2. <meta property="og:description"> / twitter:description.
        text = _meta_content(soup, ("og:description", "twitter:description"))
        title = _meta_content(soup, ("og:title", "twitter:title")) or ""
        if text:
            return ScrapeResult(text=text, title=title, source="metadata")

        return ScrapeResult(text="", title=title, source="metadata")

    def _extract_from_dom(self, soup: BeautifulSoup) -> str:
        """Heuristic DOM extraction when metadata is empty."""
        # LinkedIn post body containers (class fragments vary by template).
        candidates = []
        for selector_attr, value in (
            ("class", "update-components-text"),
            ("class", "feed-shared-text"),
            ("class", "attributed-text-segment-list"),
            ("class", "break-words"),
            ("data-test-id", "main-feed-attachment-card"),
        ):
            for el in soup.find_all(attrs={selector_attr: value}):
                candidates.append(el.get_text(" ", strip=True))

        for tag in ("article", "main"):
            el = soup.find(tag)
            if el:
                candidates.append(el.get_text(" ", strip=True))

        text = max(candidates, key=len) if candidates else soup.get_text(" ",
                                                                         strip=True)
        return _normalize_text(text)

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        og = _meta_content(soup, ("og:title", "twitter:title"))
        if og:
            return og
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return ""

    # ------------------------------------------------------------------ #
    # Strategy 3: Playwright (optional)
    # ------------------------------------------------------------------ #
    def _fetch_with_playwright(self, url: str) -> ScrapeResult:
        """Render the page with a headless browser if Playwright is installed.

        Runs the blocking Playwright sync API inside the default executor so
        it can be awaited from async code without blocking the event loop.
        Importing Playwright lazily keeps it an optional dependency.
        """
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise ScrapeError("Playwright is not installed.") from exc

        def _render() -> str:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=random.choice(self._user_agents),
                    locale="en-US",
                )
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=self._timeout * 1000)
                    # Wait briefly for JS-rendered content to settle.
                    page.wait_for_timeout(2500)
                    return page.content()
                finally:
                    context.close()
                    browser.close()

        html = _render()
        soup = BeautifulSoup(html, "html.parser")
        text = _normalize_text(self._extract_from_dom(soup))
        title = self._extract_title(soup)
        return ScrapeResult(text=text, title=title, source="playwright")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_meaningful(text: str) -> bool:
        """A scraped body is useful once it has >=200 non-trivial chars."""
        return bool(text) and len(text.strip()) >= 200

    @staticmethod
    def _looks_like_challenge(html: str) -> bool:
        """Detect LinkedIn auth walls / bot-challenge interstitials."""
        if not html:
            return True
        needles = (
            "authwall",
            "Sign in to continue",
            "verify you are a human",
            "Just a moment...",
            "cdn.cookielaw.org",
        )
        sample = html[:4000].lower()
        return any(n.lower() in sample for n in needles)


# ---------------------------------------------------------------------- #
# Module-level helpers
# ---------------------------------------------------------------------- #
def _meta_content(soup: BeautifulSoup, names: tuple) -> str:
    """Return the first non-empty ``<meta>`` content for the given keys."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or \
            soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def _find_in_jsonld(raw: str, keys: tuple) -> str:
    """Naively extract a value for any of ``keys`` from a JSON-LD blob.

    We avoid full ``json.loads`` because LinkedIn's JSON-LD is sometimes
    malformed; a regex scan is more forgiving.
    """
    for key in keys:
        match = re.search(
            r'"%s"\s*:\s*"((?:\\.|[^"\\])*)"' % re.escape(key), raw
        )
        if match:
            return _decode_json_string(match.group(1))
    return ""


def _decode_json_string(value: str) -> str:
    """Unescape a JSON string literal value."""
    try:
        import json
        return json.loads('"%s"' % value)
    except Exception:  # noqa: BLE001
        return value.replace("\\n", "\n").replace('\\"', '"').replace(
            "\\\\", "\\")


def _normalize_text(text: str) -> str:
    """Collapse whitespace and drop boilerplate noise lines."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower() in {"sign in", "join now", "learn more",
                            "see more", "…see more", "see less"}:
            continue
        lines.append(line)
    return _BLANK_RE.sub(" ", " ".join(lines)).strip()


_BLANK_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------- #
# Async adapter
# ---------------------------------------------------------------------- #
async def scrape_async(url: str) -> ScrapeResult:
    """Async-friendly wrapper running the (blocking) scraper in a thread.

    The scraper uses ``requests`` / Playwright's sync API, so we offload it
    to the default executor to avoid stalling python-telegram-bot's loop.
    """
    scraper = LinkedInScraper()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, scraper.fetch_text, url)
