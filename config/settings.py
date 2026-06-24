"""
Central configuration for the LinkedIn Podcast Bot.

All runtime values (API keys, tokens, tunables) are read from environment
variables — typically loaded from a local ``.env`` file by ``python-dotenv``
in :func:`main`. Nothing is hardcoded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load variables from .env (if present) into os.environ early so that the
# dataclass defaults below pick them up at import time.
load_dotenv()

# --- Project paths ---------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
TEMP_DIR: Path = PROJECT_ROOT / "temp"


def _read_int(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` on any error."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_list(name: str, default: List[str]) -> List[str]:
    """Read a comma-separated env var into a list of strings."""
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable container holding all application configuration."""

    # --- Telegram ---------------------------------------------------------
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # --- AI providers -----------------------------------------------------
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")

    # Primary / fallback provider order: "groq" -> "gemini"
    ai_provider_order: tuple = ("groq", "gemini")

    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    # Network retries / timeouts
    ai_max_retries: int = _read_int("AI_MAX_RETRIES", 3)
    ai_request_timeout: int = _read_int("AI_REQUEST_TIMEOUT", 60)

    # --- Text-to-Speech ---------------------------------------------------
    # edge-tts voice ids for Farsi (fa-IR)
    tts_voices: dict = field(
        default_factory=lambda: {
            "female": os.getenv("TTS_VOICE_FEMALE", "fa-IR-DilaraNeural"),
            "male": os.getenv("TTS_VOICE_MALE", "fa-IR-FaridNeural"),
        }
    )
    # Negative values = slower speech (e.g. "-5%" slightly slower).
    tts_rate: str = os.getenv("TTS_RATE", "-5%")
    tts_volume: str = os.getenv("TTS_VOLUME", "+0%")

    # --- Scraping ---------------------------------------------------------
    scrape_timeout: int = _read_int("SCRAPE_TIMEOUT", 20)
    scrape_max_retries: int = _read_int("SCRAPE_MAX_RETRIES", 3)
    user_agents: List[str] = field(
        default_factory=lambda: _read_list(
            "USER_AGENTS",
            [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Safari/605.1.15",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ],
        )
    )

    # --- Misc -------------------------------------------------------------
    temp_dir: Path = TEMP_DIR
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: str = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    # --- Validation -------------------------------------------------------
    def validate_required(self) -> None:
        """Raise ``RuntimeError`` if mandatory secrets are missing.

        Non-fatal for optional providers (Gemini is a fallback), but the
        Telegram token must always be present to start the bot.
        """
        missing: List[str] = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not (self.groq_api_key or self.gemini_api_key):
            missing.append("GROQ_API_KEY and/or GEMINI_API_KEY")
        if missing:
            raise RuntimeError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Please configure your .env file."
            )


# Shared singleton instance imported across the app.
settings = Settings()

# Ensure the temp directory exists at import time so services can write to it
# without having to create it themselves.
settings.temp_dir.mkdir(parents=True, exist_ok=True)
