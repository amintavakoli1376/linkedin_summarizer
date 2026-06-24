"""
Inline keyboards for the Telegram bot.

Keeping keyboard construction in one module makes the copy/labels easy to
audit and localize. All user-facing strings are in Farsi.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Callback-data constants (keep short — Telegram limits callback_data length).
CB_VOICE_FEMALE = "voice:female"
CB_VOICE_MALE = "voice:male"


def voice_gender_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for choosing the podcast voice gender."""
    keyboard = [
        [
            InlineKeyboardButton("👩 زنانه (دلارا)", callback_data=CB_VOICE_FEMALE),
            InlineKeyboardButton("👨 مردانه (فرید)", callback_data=CB_VOICE_MALE),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)
