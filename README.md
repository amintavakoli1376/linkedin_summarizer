# 🎙️ LinkedIn Podcast Bot (ربات پادکست لینکدین)

A production-ready Telegram bot that takes a **LinkedIn post URL**, scrapes
its content, summarizes it into **natural Farsi** with a free AI model, and
converts the summary into a **voice podcast** using free Farsi Neural TTS.

> Flow: `LinkedIn URL` → 🔎 Scrape → 🤖 AI Summary (Farsi) → 🎙️ Voice podcast

---

## ✨ Features

- ⚡ **Fully async** (python-telegram-bot v20 style)
- 🤖 **Groq** as primary AI (Llama 3.3 70B) + **Google Gemini** fallback
- 🗣️ **edge-tts** Farsi Neural voices (DilaraNeural / FaridNeural) — best free quality
- 🔎 Layered scraping: requests + metadata → optional Playwright fallback
- 🔁 Retry logic with exponential backoff on AI + scrape calls
- 🧹 Automatic temp-file cleanup after every voice note
- 🇮🇷 All user-facing messages in **Farsi**
- 🧰 Type hints, docstrings, structured logging — no hardcoded secrets

---

## 📁 Project structure

```
linkedin_summarizer/
├── main.py                 # Entry point
├── bot/
│   ├── __init__.py
│   ├── handlers.py         # Telegram message handlers
│   └── keyboards.py        # Inline keyboards
├── services/
│   ├── __init__.py
│   ├── scraper.py          # LinkedIn scraper
│   ├── ai_summarizer.py    # AI summarization (Groq + Gemini)
│   └── tts_service.py      # Text-to-speech
├── config/
│   ├── __init__.py
│   └── settings.py         # Config & API keys
├── utils/
│   ├── __init__.py
│   └── helpers.py          # Utility functions
├── temp/                   # Temporary audio files (auto-created, gitignored)
├── .env.example            # Copy to .env
├── requirements.txt
└── README.md
```

---

## 🚀 Setup (step by step)

### 1. Clone & create a virtual environment

```bash
git clone <your-repo-url> linkedin_summarizer
cd linkedin_summarizer

python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install ffmpeg (required for MP3 → OGG conversion)

ffmpeg must be **on your system PATH** — pydub shells out to it.

- **Windows (winget)**: `winget install Gyan.FFmpeg`
- **Windows (manual)**: download from <https://www.gyan.dev/ffmpeg/builds/>, unzip, and add the `bin` folder to your `PATH`.
- **macOS**: `brew install ffmpeg`
- **Ubuntu / Debian**: `sudo apt-get install -y ffmpeg`

Verify:

```bash
ffmpeg -version
```

### 4. (Optional) Install Playwright browser

Playwright is an optional fallback for pages that block static requests.
If you want it enabled:

```bash
playwright install chromium
```

Without this, the bot still works — it just skips the headless-browser
strategy and relies on requests + metadata extraction.

### 5. Get your FREE API keys

<details>
<summary><b>🤖 Telegram bot token</b></summary>

1. Open Telegram → search **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy the token like `123456789:ABCdefGhI...`
</details>

<details>
<summary><b>⚡ Groq API key (primary AI)</b></summary>

1. Go to <https://console.groq.com>
2. Sign in → **API Keys** → **Create API Key**
3. Free tier: very fast, generous rate limits
</details>

<details>
<summary><b>💎 Google Gemini API key (fallback AI)</b></summary>

1. Go to <https://aistudio.google.com/app/apikey>
2. **Create API key**
3. Free tier: 15 req/min, 1500 req/day on `gemini-1.5-flash`
</details>

### 6. Configure `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhI...
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=AIza...
```

> You only need **one** AI key to run, but providing both enables the
> automatic fallback chain.

---

## ▶️ Run the bot

```bash
python main.py
```

You should see:

```
2026-... | INFO     | __main__ | Starting LinkedIn Podcast Bot...
2026-... | INFO     | __main__ | Bot '@yourbot' (id=...) is starting...
2026-... | INFO     | bot.handlers | Handlers registered; 2 AI provider(s) available.
```

Open Telegram, find your bot, send `/start`, then send a LinkedIn post URL. 🎉

---

## 🧭 Bot commands

| Command     | Description                              |
|-------------|------------------------------------------|
| `/start`    | Welcome message + instructions           |
| `/help`     | Usage guide                              |
| `/settings` | Choose voice gender (female / male)      |

Then just **send any public LinkedIn post URL** as a message.

---

## 🔧 Configuration reference

All values are environment variables (read from `.env`). Optional ones have
sensible defaults — only the three tokens above are required.

| Variable             | Default                  | Description                       |
|----------------------|--------------------------|-----------------------------------|
| `TELEGRAM_BOT_TOKEN` | *(required)*             | Bot token from @BotFather         |
| `GROQ_API_KEY`       | *(required or Gemini)*   | Primary AI provider key           |
| `GROQ_MODEL`         | `llama-3.3-70b-versatile`| Groq model id                     |
| `GEMINI_API_KEY`     | *(required or Groq)*     | Fallback AI provider key          |
| `GEMINI_MODEL`       | `gemini-1.5-flash`       | Gemini model id                   |
| `TTS_VOICE_FEMALE`   | `fa-IR-DilaraNeural`     | Female Farsi Neural voice         |
| `TTS_VOICE_MALE`     | `fa-IR-FaridNeural`      | Male Farsi Neural voice           |
| `TTS_RATE`           | `-5%`                    | Speech rate (negative = slower)   |
| `TTS_VOLUME`         | `+0%`                    | Volume offset                     |
| `AI_MAX_RETRIES`     | `3`                      | Retry attempts per provider       |
| `SCRAPE_TIMEOUT`     | `20`                     | HTTP scrape timeout (seconds)     |
| `LOG_LEVEL`          | `INFO`                   | Logging verbosity                 |

---

## 🧪 Testing

### Smoke test individual components

```bash
# 1. Verify settings load and keys are present
python -c "from config.settings import settings; settings.validate_required(); print('config OK')"

# 2. Test the AI summarizer with sample text
python -c "import asyncio; from services.ai_summarizer import AISummarizer; print(asyncio.run(AISummarizer().summarize('AI is transforming how we work and collaborate across industries...')))"
```

### Manual end-to-end test

1. Run `python main.py`
2. In Telegram, send `/start` → confirm the welcome message
3. Send `/settings` → tap a voice option → confirm confirmation
4. Send a **public** LinkedIn post URL, e.g.:
   `https://www.linkedin.com/posts/someuser_something-1234/`
5. Expect, in order:
   - `🔄 در حال پردازش لینک شما...`
   - The Farsi summary (text)
   - `🎙️ در حال ساخت پادکست...`
   - A voice note you can play inline

### Common issues

| Symptom                                          | Fix                                                    |
|--------------------------------------------------|--------------------------------------------------------|
| `Configuration error: Missing ... variable`      | Fill in `.env` (copy from `.env.example`)             |
| TTS error mentions ffmpeg                        | Install ffmpeg and ensure it's on `PATH`              |
| Scrape fails / empty content                     | Post is private/login-walled — try a public one       |
| `No AI provider is configured`                   | Set `GROQ_API_KEY` or `GEMINI_API_KEY`                |
| 429 rate limit                                    | Fallback kicks in automatically; wait & retry         |

---

## 🏗️ Architecture notes

- **Provider chain**: `services/ai_summarizer.py` walks
  `settings.ai_provider_order` (`groq` → `gemini`) and returns the first
  successful result, retrying each with exponential backoff.
- **Async hygiene**: blocking work (requests, Playwright, pydub, Groq/Gemini
  SDKs) runs in `loop.run_in_executor(...)` so the event loop never stalls.
- **Per-user settings**: voice gender is stored in
  `application.bot_data["user_prefs"]` (in-memory). Swap for Redis/SQLite to
  scale across instances.
- **Temp cleanup**: every voice path is removed in a `finally` block after
  sending (see `handlers.handle_linkedin_message`).
- **Scraping reality**: LinkedIn aggressively gates content. The metadata
  (JSON-LD / Open-Graph) extraction is the most reliable path for public
  articles; Playwright helps with rendered feed posts.

---

## 📜 License

MIT — free to use, modify, and distribute.
