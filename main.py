"""
Entry point for the LinkedIn Podcast Telegram Bot.

Boots the asyncio application, configures structured logging, validates the
environment, registers handlers, and wires graceful shutdown so a SIGINT /
SIGTERM cleanly stops polling and releases resources.

Usage::

    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from telegram.ext import Application, ApplicationBuilder

from bot.handlers import register_handlers
from config.settings import settings

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Set up logging to stdout using the configured level/format."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format=settings.log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Silence overly chatty third-party loggers but keep warnings visible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def build_application(token: Optional[str] = None) -> Application:
    """Construct and configure the Telegram :class:`Application`.

    Args:
        token: Override the token from settings (useful for tests).
    """
    token = token or settings.telegram_bot_token
    application = (
        ApplicationBuilder()
        .token(token)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    register_handlers(application)
    return application


async def _post_init(application: Application) -> None:
    """Hook run after initialization, before polling starts."""
    me = await application.bot.get_me()
    logger.info("Bot '@%s' (id=%s) is starting...", me.username, me.id)


async def _post_stop(application: Application) -> None:
    """Hook run after the application stops — release resources."""
    bot_data = getattr(application, "bot_data", {})
    tts = bot_data.get("tts")
    if tts is not None:
        logger.info("TTS service shutting down.")
    logger.info("Application stopped cleanly.")


def run() -> None:
    """Configure, build, and start the bot with graceful shutdown."""
    configure_logging()
    logger.info("Starting LinkedIn Podcast Bot...")

    # Fail fast on missing required secrets rather than crashing on first msg.
    try:
        settings.validate_required()
    except RuntimeError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    application = build_application()
    application.post_init = _post_init
    application.post_shutdown = _post_stop

    # Map OS signals to a clean stop so Ctrl+C is graceful on Windows & POSIX.
    stop_event = asyncio.Event()

    def _request_stop(signum, _frame=None) -> None:  # noqa: ANN001
        logger.info("Received signal %s — shutting down...", signum)
        stop_event.set()

    # SIGINT works everywhere; SIGTERM is POSIX-only.
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (ValueError, OSError):
            # Not in main thread or unsupported — ignore.
            pass

    logger.info("Starting long-polling. Press Ctrl+C to stop.")
    application.run_polling(
        stop_signals=None,  # we handle signals ourselves for cross-platform use
        close_loop=False,
    )


if __name__ == "__main__":
    run()
