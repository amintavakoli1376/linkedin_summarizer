"""
Telegram message & callback handlers (python-telegram-bot v20+, async).

This module wires together the three services (scraper → AI summarizer → TTS)
into the user-facing conversation:

* ``/start`` & ``/help``  — onboarding text
* ``/settings``           — choose voice gender (persisted per-user)
* any text message        — treated as a LinkedIn URL to process

Per-user preferences (voice gender) are stored in an in-memory dict keyed by
``user_id``. For a single-instance bot this is sufficient; swap for a store
like Redis if you ever scale horizontally.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.keyboards import (
    CB_VOICE_FEMALE,
    CB_VOICE_MALE,
    voice_gender_keyboard,
)
from config.settings import settings
from services.ai_summarizer import AISummarizer, AISummaryError
from services.scraper import ScrapeError, scrape_async
from services.tts_service import TTSError, TTSService, VoiceGender
from utils.helpers import cleanup_file, is_valid_linkedin_url, normalize_url

logger = logging.getLogger(__name__)

# In-memory per-user settings. Key: user_id (int), value: {"voice": "female"}.
# Persisted to bot_data so it survives across handler modules in tests.
_USER_PREFS_KEY = "user_prefs"

WELCOME_TEXT = (
    "👋 سلام! به ربات <b>پادکست لینکدین</b> خوش آمدید.\n\n"
    "🔍 یک <b>لینک پست لینکدین</b> را برای من بفرستید تا:\n"
    "  ۱️⃣ محتوای آن را استخراج کنم\n"
    "  ۲️⃣ خلاصه‌ای روان به فارسی بسازم\n"
    "  ۳️⃣ آن را به یک <b>پادکست صوتی</b> تبدیل کنم 🎙️\n\n"
    "برای تغییر صدای پادکست از /settings استفاده کنید."
)

HELP_TEXT = (
    "📚 <b>راهنمای استفاده</b>\n\n"
    "1. یک پست عمومی در لینکدین باز کنید و لینک آن را کپی نمایید.\n"
    "2. لینک را همین‌جا بفرستید.\n"
    "3. چند لحظه صبر کنید تا خلاصه و سپس فایل صوتی برایتان آماده شود.\n\n"
    "⚙️ <b>تنظیمات</b>\n"
    "• <code>/settings</code> — انتخاب جنسیت صدا (زنانه/مردانه)\n"
    "• <code>/start</code> — پیام خوش‌آمد\n"
    "• <code>/help</code> — همین راهنما\n\n"
    "⚠️ توجه: پست‌های خصوصی یا محافظت‌شده قابل استخراج نیستند."
)

PROCESSING_LINK_TEXT = "🔄 در حال پردازش لینک شما..."
MAKING_PODCAST_TEXT = "🎙️ در حال ساخت پادکست..."


# ---------------------------------------------------------------------- #
# Per-user preference helpers
# ---------------------------------------------------------------------- #
def _prefs(context: ContextTypes.DEFAULT_TYPE) -> dict[int, dict[str, Any]]:
    """Return (and lazily create) the shared user-preferences store."""
    store = context.application.bot_data.setdefault(_USER_PREFS_KEY, {})
    if not isinstance(store, dict):
        store = {}
        context.application.bot_data[_USER_PREFS_KEY] = store
    return store


def get_user_voice(context: ContextTypes.DEFAULT_TYPE,
                   user_id: int) -> VoiceGender:
    """Return the saved voice gender for ``user_id`` (default: female)."""
    prefs = _prefs(context)
    return prefs.get(user_id, {}).get("voice", "female")


def set_user_voice(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                   voice: VoiceGender) -> None:
    """Persist the chosen voice gender for ``user_id``."""
    prefs = _prefs(context)
    prefs.setdefault(user_id, {})["voice"] = voice


# ---------------------------------------------------------------------- #
# Command handlers
# ---------------------------------------------------------------------- #
async def start_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — show the welcome message."""
    if not update.message:
        return
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)


async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` — show usage instructions."""
    if not update.message:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def settings_command(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/settings`` — present the voice-gender inline keyboard."""
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    current = get_user_voice(context, user_id)
    current_label = "👩 زنانه" if current == "female" else "👨 مردانه"
    text = (
        "⚙️ <b>تنظیمات پادکست</b>\n\n"
        f"صدای فعلی شما: {current_label}\n"
        "لطفاً جنسیت صدا را انتخاب کنید:"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=voice_gender_keyboard(),
    )


async def voice_callback(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-keyboard selection of voice gender."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if data == CB_VOICE_FEMALE:
        voice: VoiceGender = "female"
        label = "👩 زنانه (دلارا)"
    elif data == CB_VOICE_MALE:
        voice = "male"
        label = "👨 مردانه (فرید)"
    else:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    set_user_voice(context, user_id, voice)
    logger.info("User %s set voice to %s", user_id, voice)

    await query.edit_message_text(
        f"✅ صدای پادکست روی <b>{label}</b> تنظیم شد.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------- #
# Main pipeline
# ---------------------------------------------------------------------- #
async def handle_linkedin_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Process an incoming LinkedIn URL end-to-end."""
    if not update.message or not update.message.text:
        return

    raw_url = update.message.text.strip()
    user = update.effective_user

    # 1. Validate the URL up front.
    if not is_valid_linkedin_url(raw_url):
        await update.message.reply_text(
            "❌ لینک ارسالی معتبر نیست.\n\n"
            "لطفاً یک لینک پست عمومی لینکدین به این شکل بفرستید:\n"
            "<code>https://www.linkedin.com/posts/...</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    url = normalize_url(raw_url)
    logger.info("User %s requested summary for %s",
                user.id if user else "?", url)

    status_msg = await update.message.reply_text(PROCESSING_LINK_TEXT)
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    summarizer: AISummarizer = context.application.bot_data["summarizer"]
    tts: TTSService = context.application.bot_data["tts"]

    # 2. Scrape.
    try:
        result = await scrape_async(url)
    except ScrapeError as exc:
        await _edit_or_reply(status_msg, update,
                             _scrape_error_text(exc))
        return
    except Exception as exc:  # noqa: BLE001 — surface a friendly message
        logger.exception("Unexpected scrape failure for %s", url)
        await _edit_or_reply(
            status_msg, update,
            "⚠️ خطای غیرمنتظره هنگام دریافت محتوا رخ داد. لطفاً دوباره تلاش کنید.",
        )
        return

    # 3. Summarize.
    try:
        summary = await summarizer.summarize(result.text)
    except AISummaryError as exc:
        await _edit_or_reply(status_msg, update,
                             _ai_error_text(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected AI failure for %s", url)
        await _edit_or_reply(
            status_msg, update,
            "⚠️ ساخت خلاصه ناموفق بود. لطفاً کمی بعد دوباره تلاش کنید.",
        )
        return

    # 4. Send the text summary.
    await _edit_or_reply(status_msg, update, _summary_text(summary, result.title))

    # 5. Build the podcast voice note.
    podcast_status = await update.message.reply_text(MAKING_PODCAST_TEXT)
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE
    )

    voice = get_user_voice(context, user.id if user else 0)
    ogg_path: Path | None = None
    try:
        ogg_path = await tts.synthesize(summary, gender=voice)
        await _send_voice(update, context, ogg_path)
    except TTSError as exc:
        logger.warning("TTS failed for user %s: %s", user.id, exc)
        await _edit_or_reply(
            podcast_status, update, _tts_error_text(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected TTS failure for %s", url)
        await _edit_or_reply(
            podcast_status, update,
            "⚠️ ساخت فایل صوتی ناموفق بود. آیا ffmpeg نصب است؟",
        )
    finally:
        if ogg_path is not None:
            cleanup_file(ogg_path)
        # Clear the "making podcast" status if still showing.
        try:
            await podcast_status.delete()
        except Exception:  # noqa: BLE001
            pass


async def _send_voice(update: Update,
                      context: ContextTypes.DEFAULT_TYPE,
                      ogg_path: Path) -> None:
    """Send the generated OGG as a Telegram voice note."""
    chat_id = update.effective_chat.id
    caption = "🎧 پادکست شما آماده است!"
    with open(ogg_path, "rb") as audio:
        await context.bot.send_voice(
            chat_id=chat_id,
            voice=audio,
            caption=caption,
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )


# ---------------------------------------------------------------------- #
# Message formatting helpers (Farsi)
# ---------------------------------------------------------------------- #
def _summary_text(summary: str, title: str | None) -> str:
    """Wrap the AI summary with a header (and post title if available)."""
    header = "📝 <b>خلاصه پست:</b>\n\n"
    if title:
        header = f"📝 <b>خلاصه پست</b>\n<i>{_escape(title)}</i>\n\n"
    return header + summary


def _scrape_error_text(exc: Exception) -> str:
    return (
        "❌ دریافت محتوای پست ناموفق بود.\n\n"
        "دلایل احتمالی:\n"
        "• پست خصوصی است یا نیاز به ورود دارد\n"
        "• لینک اشتباه یا منقضی شده است\n"
        "• لینکدین دسترسی را موقتاً مسدود کرده است\n\n"
        "لطفاً لینک دیگری امتحان کنید."
    )


def _ai_error_text(exc: Exception) -> str:
    return (
        "⚠️ سرویس هوش مصنوعی در حال حاضر در دسترس نیست.\n"
        "ممکن است سقف استفاده‌ی رایانه پر شده باشد.\n"
        "لطفاً چند دقیقه بعد دوباره تلاش کنید."
    )


def _tts_error_text(exc: Exception) -> str:
    return (
        "⚠️ تبدیل متن به صدا ناموفق بود.\n"
        "لطفاً دوباره تلاش کنید یا با /settings صدای دیگری انتخاب نمایید."
    )


def _escape(text: str) -> str:
    """Escape HTML-special characters for safe inclusion in messages."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _edit_or_reply(status_msg, update: Update, text: str) -> None:
    """Edit the status message, falling back to a new reply on failure."""
    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001 — edit may fail if content unchanged
        if update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------- #
# Registration
# ---------------------------------------------------------------------- #
def register_handlers(application: Application) -> None:
    """Attach every handler to the given :class:`Application`.

    Called once from :mod:`main` during startup. Also seeds shared services
    into ``bot_data`` so handlers can reach them without globals.
    """
    # Shared singletons — built once, reused for every update.
    application.bot_data["summarizer"] = AISummarizer()
    application.bot_data["tts"] = TTSService()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(voice_callback))

    # Catch-all for plain text messages (treated as LinkedIn URLs).
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND,
                       handle_linkedin_message)
    )

    logger.info("Handlers registered; %d AI provider(s) available.",
                1 if application.bot_data["summarizer"].has_provider else 0)
