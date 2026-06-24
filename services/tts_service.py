"""
Persian text-to-speech using Microsoft Edge TTS (``edge-tts``).

Flow:
1. Synthesize Farsi text → MP3 (temp file) via edge-tts.
2. Convert MP3 → OGG Opus with ffmpeg directly (no pydub needed).
3. Return the path; caller is responsible for cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from config.settings import settings
from utils.helpers import chunk_text, ensure_dir, text_for_tts

logger = logging.getLogger(__name__)

VoiceGender = Literal["female", "male"]
SUPPORTED_GENDERS: tuple[VoiceGender, ...] = ("female", "male")


class TTSError(Exception):
    """Raised when synthesis or audio conversion fails."""


class TTSService:
    """Synthesize Farsi text into a Telegram-ready OGG Opus voice note."""

    def __init__(self, voices: dict | None = None) -> None:
        self._voices = dict(voices or settings.tts_voices)
        self._temp_dir = ensure_dir(settings.temp_dir)
        self._check_ffmpeg()

    # ------------------------------------------------------------------ #
    # Startup Check
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_ffmpeg() -> None:
        """Warn at startup if ffmpeg is missing."""
        if shutil.which("ffmpeg") is None:
            logger.warning(
                "ffmpeg not found on PATH! TTS conversion will fail. "
                "Install from https://ffmpeg.org and add to PATH."
            )
        else:
            logger.debug("ffmpeg found: %s", shutil.which("ffmpeg"))

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def synthesize(
        self,
        text: str,
        gender: VoiceGender = "female",
    ) -> Path:
        """Convert ``text`` to an OGG Opus voice note.

        Args:
            text:   Farsi summary to narrate. Markdown stripped internally.
            gender: Which Neural voice to use ("female" or "male").

        Returns:
            Path to the generated ``.ogg`` file (caller must delete it).

        Raises:
            TTSError: on synthesis or conversion failure.
        """
        if gender not in SUPPORTED_GENDERS:
            raise TTSError(f"Unsupported voice gender: {gender!r}")

        clean = text_for_tts(text)
        if not clean.strip():
            raise TTSError("متن برای تبدیل به صدا خالی است.")

        voice = self._voices.get(gender) or self._voices["female"]
        job_id = uuid.uuid4().hex[:12]
        mp3_path = self._temp_dir / f"tts_{job_id}.mp3"
        ogg_path = self._temp_dir / f"tts_{job_id}.ogg"

        try:
            await self._render_mp3(clean, voice, mp3_path)
            await self._convert_to_ogg(mp3_path, ogg_path)
            logger.info(
                "TTS ready: %s (%d chars, voice=%s)",
                ogg_path.name, len(clean), voice,
            )
            return ogg_path
        finally:
            # Intermediate MP3 is never needed again
            _safe_remove(mp3_path)

    # ------------------------------------------------------------------ #
    # Synthesis
    # ------------------------------------------------------------------ #
    async def _render_mp3(
        self, text: str, voice: str, out_path: Path
    ) -> None:
        """Stream Farsi speech to an MP3 file via edge-tts."""
        try:
            import edge_tts  # type: ignore
        except ImportError as exc:
            raise TTSError("edge-tts is not installed.") from exc

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=settings.tts_rate,
            volume=settings.tts_volume,
        )
        try:
            await communicate.save(str(out_path))
        except Exception as exc:
            raise TTSError(f"edge-tts synthesis failed: {exc}") from exc

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise TTSError("edge-tts produced an empty audio file.")

    # ------------------------------------------------------------------ #
    # Conversion  (ffmpeg — no pydub)
    # ------------------------------------------------------------------ #
    async def _convert_to_ogg(
        self, mp3_path: Path, ogg_path: Path
    ) -> None:
        """Transcode MP3 → OGG Opus (Telegram voice format) via ffmpeg."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self._ffmpeg_to_ogg, mp3_path, ogg_path
            )
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(
                f"تبدیل صدا ناموفق بود: {exc}"
            ) from exc

        if not ogg_path.exists() or ogg_path.stat().st_size == 0:
            raise TTSError("Audio conversion produced an empty file.")

    @staticmethod
    def _ffmpeg_to_ogg(mp3_path: Path, ogg_path: Path) -> None:
        """Blocking MP3 → OGG Opus via ffmpeg subprocess."""

        # ── guard ────────────────────────────────────────────────────────
        if shutil.which("ffmpeg") is None:
            raise TTSError(
                "ffmpeg not found on PATH. "
                "Download from https://ffmpeg.org and add bin/ to PATH, "
                "then restart your terminal."
            )

        cmd = [
            "ffmpeg",
            "-y",                 # overwrite output silently
            "-i", str(mp3_path),  # input
            "-c:a", "libopus",    # Opus codec (Telegram voice note)
            "-b:a", "64k",        # bitrate
            "-vn",                # strip any video stream
            str(ogg_path),        # output
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise TTSError("ffmpeg executable not found.") from exc
        except subprocess.TimeoutExpired as exc:
            raise TTSError("ffmpeg conversion timed out (>60 s).") from exc

        if result.returncode != 0:
            # Log full stderr so you can debug codec/format issues
            logger.error("ffmpeg stderr:\n%s", result.stderr)
            raise TTSError(
                f"ffmpeg exited with code {result.returncode}. "
                f"See logs for details."
            )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @property
    def available_voices(self) -> dict:
        """Return a copy of the configured voice mapping."""
        return dict(self._voices)

    @staticmethod
    def safe_text_length(text: str, limit: int = 4_000) -> str:
        """Truncate text to keep synthesis under edge-tts practical limit."""
        if len(text) <= limit:
            return text
        chunks = chunk_text(text, limit)
        kept, total = [], 0
        for chunk in chunks:
            if total + len(chunk) > limit:
                remaining = limit - total
                if remaining > 80:
                    kept.append(chunk[:remaining].rsplit(" ", 1)[0])
                break
            kept.append(chunk)
            total += len(chunk)
        return " ".join(kept).strip()


def _safe_remove(path: Path) -> None:
    """Best-effort file removal — never raises."""
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.debug("Could not remove %s: %s", path, exc)