"""
AI summarization with provider fallback.

Primary: **Groq** (``llama-3.3-70b-versatile``) — fast and generous free tier.
Fallback: **Google Gemini** (``gemini-1.5-flash``) — used when Groq errors out
(rate limit, downtime, missing key).

Both providers are called through small adapter classes that share a common
interface (``summarize(text) -> str``). The orchestrator (:class:`AISummarizer`)
walks ``settings.ai_provider_order`` and returns the first successful result,
retrying transient failures with exponential backoff.

Each SDK is imported lazily so a missing/optional dependency never breaks the
whole module — the bot will simply skip that provider and try the next.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from config.settings import settings

logger = logging.getLogger(__name__)


class AISummaryError(Exception):
    """Raised when every configured provider fails to summarize."""


# The exact system prompt from the project specification.
SYSTEM_PROMPT = """\
شما یک دستیار هوشمند فارسی هستید.
متن زیر از یک پست لینکدین به زبان انگلیسی یا فارسی است.
لطفاً:
1. خلاصه‌ای روان و حرفه‌ای به زبان فارسی بنویس (۱۵۰-۲۰۰ کلمه)
2. نکات کلیدی را با بولت پوینت مشخص کن
3. یک جمله نتیجه‌گیری جذاب اضافه کن
4. لحن: حرفه‌ای اما قابل فهم برای عموم
"""

USER_PROMPT_TEMPLATE = """\
متن پست لینکدین:

\"\"\"
{content}
\"\"\"

لطفاً بر اساس دستورالعمل بالا خلاصه کنید."""


class _Provider(Protocol):
    """Minimal interface every AI backend must satisfy."""

    name: str

    def is_available(self) -> bool: ...

    def summarize(self, text: str) -> str: ...


# ---------------------------------------------------------------------- #
# Groq adapter
# ---------------------------------------------------------------------- #
class GroqProvider:
    """Summarization via the Groq Chat Completions API."""

    name = "groq"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model
        self._client = None  # built lazily

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from groq import Groq  # type: ignore
        except ImportError as exc:  # pragma: no cover - import guard
            logger.warning("groq SDK not installed: %s", exc)
            raise
        self._client = Groq(api_key=self._api_key, timeout=settings.ai_request_timeout)
        return self._client

    def summarize(self, text: str) -> str:
        client = self._ensure_client()
        logger.debug("Groq: summarizing %d chars with model %s",
                     len(text), self._model)
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(content=text)},
            ],
            temperature=0.4,
            max_tokens=600,
        )
        return _extract_text(response)


# ---------------------------------------------------------------------- #
# Gemini adapter
# ---------------------------------------------------------------------- #
class GeminiProvider:
    """Summarization via Google's ``google-generativeai`` SDK."""

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model_name = model
        self._model = None

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as exc:  # pragma: no cover - import guard
            logger.warning("google-generativeai SDK not installed: %s", exc)
            raise
        genai.configure(api_key=self._api_key)
        # ``system_instruction`` only exists on GenerativeModel from
        # google-generativeai >= 0.5.0. Probe the constructor so we work
        # across versions instead of raising TypeError on older SDKs.
        supports_sys_instr = _supports_kwarg(
            genai.GenerativeModel, "system_instruction"
        )
        if supports_sys_instr:
            self._model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=SYSTEM_PROMPT,
            )
        else:
            logger.warning(
                "google-generativeai lacks system_instruction support; "
                "folding system prompt into the user message."
            )
            self._model = genai.GenerativeModel(model_name=self._model_name)
        # Remember whether to inline the prompt at summarize() time.
        self._inline_system = not supports_sys_instr
        return self._model

    def summarize(self, text: str) -> str:
        model = self._ensure_model()
        logger.debug("Gemini: summarizing %d chars with model %s",
                     len(text), self._model_name)
        # When the SDK can't take a system instruction, prepend it to the
        # user prompt so the model still sees the directive.
        prompt = USER_PROMPT_TEMPLATE.format(content=text)
        if getattr(self, "_inline_system", False):
            prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
        # Build a typed GenerationConfig when supported (>= ~0.3) and fall
        # back to a plain dict for very old/new signatures.
        config = self._build_generation_config()
        response = model.generate_content(prompt, generation_config=config)
        return _extract_text(response)

    @staticmethod
    def _build_generation_config():
        """Return a GenerationConfig object or a dict, matching the SDK."""
        try:
            from google.generativeai.types import GenerationConfig  # type: ignore
        except Exception:  # noqa: BLE001 — dict is the universal fallback
            return {"temperature": 0.4, "max_output_tokens": 600}
        try:
            return GenerationConfig(temperature=0.4, max_output_tokens=600)
        except Exception:  # noqa: BLE001
            return {"temperature": 0.4, "max_output_tokens": 600}


def _supports_kwarg(func, kwarg: str) -> bool:
    """Return ``True`` if ``func`` accepts ``kwarg`` in its signature.

    Used to detect SDK-version-specific parameters (e.g. ``system_instruction``
    on ``GenerativeModel``, added in google-generativeai >= 0.5.0) without
    raising ``TypeError``. Falls back to ``False`` on any inspection error so
    callers degrade gracefully (e.g. inline the prompt instead).
    """
    import inspect

    try:
        signature = inspect.signature(func)
    except (ValueError, TypeError):
        return False
    params = signature.parameters
    return kwarg in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _extract_text(response) -> str:
    """Robustly pull a text string out of either SDK's response object."""
    # Groq / OpenAI-style response.
    choices = getattr(response, "choices", None)
    if choices:
        for choice in choices:
            msg = getattr(choice, "message", None)
            content = getattr(msg, "content", None) if msg else None
            if content:
                return content.strip()
    # Gemini-style response: iterable of candidates/parts.
    candidates = getattr(response, "candidates", None)
    if candidates:
        try:
            parts = candidates[0].content.parts
            return "".join(p.text for p in parts if getattr(p, "text", None)).strip()
        except Exception:  # noqa: BLE001 — fall through to .text
            pass
    # Last resort: many SDKs expose a ``.text`` convenience property.
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise AISummaryError("AI provider returned an empty response.")


# ---------------------------------------------------------------------- #
# Orchestrator
# ---------------------------------------------------------------------- #
class AISummarizer:
    """Coordinate primary/fallback providers with retry logic."""

    def __init__(self) -> None:
        self._providers: list[_Provider] = []
        # Build the chain from settings; order defines priority.
        factories = {
            "groq": lambda: GroqProvider(settings.groq_api_key, settings.groq_model),
            "gemini": lambda: GeminiProvider(settings.gemini_api_key,
                                             settings.gemini_model),
        }
        for name in settings.ai_provider_order:
            factory = factories.get(name)
            if not factory:
                continue
            provider = factory()
            if provider.is_available():
                self._providers.append(provider)
                logger.info("AI provider registered: %s", provider.name)
            else:
                logger.warning(
                    "AI provider '%s' skipped (missing API key).", name
                )

        if not self._providers:
            logger.warning("No AI provider is configured — summarization "
                           "will fail until a key is set.")

    @property
    def has_provider(self) -> bool:
        """True if at least one backend is ready to serve requests."""
        return bool(self._providers)

    async def summarize(self, text: str) -> str:
        """Summarize ``text`` to Farsi, falling back across providers.

        Runs each provider's blocking SDK call inside a worker thread so the
        asyncio event loop is never blocked. Retries transient failures.
        """
        if not self.has_provider:
            raise AISummaryError(
                "هیچ سرویس هوش مصنوعی پیکربندی نشده است. لطفاً GROQ_API_KEY "
                "یا GEMINI_API_KEY را تنظیم کنید."
            )
        if not text or len(text.strip()) < 50:
            raise AISummaryError("متن برای خلاصه‌سازی کوتاه یا خالی است.")

        errors: list[str] = []
        loop = asyncio.get_running_loop()

        for provider in self._providers:
            last_exc: Exception | None = None
            for attempt in range(1, settings.ai_max_retries + 1):
                try:
                    summary = await loop.run_in_executor(
                        None, provider.summarize, text
                    )
                    if summary and len(summary.strip()) >= 30:
                        logger.info("Summarized with '%s' on attempt %d.",
                                    provider.name, attempt)
                        return summary.strip()
                    logger.warning("Provider '%s' returned thin summary.",
                                   provider.name)
                except Exception as exc:  # noqa: BLE001 — try next provider
                    last_exc = exc
                    wait = min(2 ** attempt, 8)
                    logger.warning(
                        "Provider '%s' attempt %d failed: %s — retrying in %ds",
                        provider.name, attempt, exc, wait,
                    )
                    await asyncio.sleep(wait)
            errors.append(f"{provider.name}: {last_exc}")

        raise AISummaryError(
            "تمام سرویس‌های هوش مصنوعی ناموفق بودند. " +
            " | ".join(errors)
        )
