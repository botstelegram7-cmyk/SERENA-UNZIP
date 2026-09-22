# config.py
import os
from dotenv import load_dotenv

load_dotenv()


def _parse_owner_ids(raw: str) -> set:
    """Parse "123,456" into {123, 456}, ignoring junk entries."""
    ids = set()
    for piece in (raw or "").replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.add(int(piece))
        except ValueError:
            print(f"⚠️  OWNER_IDS: ignoring invalid entry {piece!r}")
    if not ids:
        print(
            "⚠️  OWNER_IDS is not set — admin commands are disabled.\n"
            "    Set it in your environment, e.g. OWNER_IDS=12345678,87654321"
        )
    return ids


class Config:
    # ── Telegram API ─────────────────────────────────────────────
    API_ID    = int(os.getenv("API_ID", "123456"))
    API_HASH  = os.getenv("API_HASH", "change_me")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "123:ABC")

    # ── MongoDB ──────────────────────────────────────────────────
    MONGO_URI = os.getenv("MONGO_URI", "")
    DB_NAME   = os.getenv("DB_NAME", "serena_unzip")

    # ── IDs ──────────────────────────────────────────────────────
    LOG_CHANNEL_ID    = int(os.getenv("LOG_CHANNEL_ID", "-1003286415377"))
    FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "serenaunzipbot")

    # Owner IDs come from the environment so they are never committed.
    # Set OWNER_IDS as a comma-separated list, e.g. "12345678,87654321".
    OWNER_IDS         = _parse_owner_ids(os.getenv("OWNER_IDS", ""))
    OWNER_USERNAME    = os.getenv("OWNER_USERNAME", "technicalserena")

    # ── General ──────────────────────────────────────────────────
    BOT_NAME  = "Serena Unzip"
    START_PIC = os.getenv("START_PIC", None)
    TEMP_DIR  = os.getenv("TEMP_DIR", "downloads")

    # ── Web App (Telegram Mini App) ───────────────────────────────
    # Set this in Render env vars: RENDER_EXTERNAL_URL=https://yourbot.onrender.com
    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

    # ── Rate limits ───────────────────────────────────────────────
    PROGRESS_UPDATE_INTERVAL = int(os.getenv("PROGRESS_UPDATE_INTERVAL", "5"))
    AUTO_DELETE_DEFAULT_MIN  = int(os.getenv("AUTO_DELETE_DEFAULT_MIN", "30"))
    FREE_DAILY_TASK_LIMIT    = int(os.getenv("FREE_DAILY_TASK_LIMIT",   "30"))
    FREE_DAILY_SIZE_MB       = int(os.getenv("FREE_DAILY_SIZE_MB",      "4096"))
    FREE_MIN_WAIT_SEC        = int(os.getenv("FREE_MIN_WAIT_SEC",       "300"))
    PREMIUM_MIN_WAIT_SEC     = int(os.getenv("PREMIUM_MIN_WAIT_SEC",    "10"))

    # ── File size caps ────────────────────────────────────────────
    MAX_ARCHIVE_SIZE_FREE_MB    = int(os.getenv("MAX_ARCHIVE_SIZE_FREE_MB",    "2048"))
    MAX_ARCHIVE_SIZE_PREMIUM_MB = int(os.getenv("MAX_ARCHIVE_SIZE_PREMIUM_MB", "10240"))
    AUTO_SPLIT_MB               = int(os.getenv("AUTO_SPLIT_MB", "1900"))

    # ── yt-dlp / downloader ───────────────────────────────────────
    INSTAGRAM_COOKIES  = os.getenv("INSTAGRAM_COOKIES", "")   # Full Netscape format

    # TeraBox: the `ndus` cookie from a logged-in session. Anonymous
    # datacenter IPs are walled (errno 140), so this is what makes
    # TeraBox downloads work reliably from a server.
    TERABOX_COOKIE     = os.getenv("TERABOX_COOKIE", "")

    # Optional HTTP/SOCKS proxy for TeraBox. TeraBox withholds signed
    # download links from datacenter IPs even when the cookie is valid and
    # the share lists fine, so a residential proxy is the only reliable
    # fix. Example: http://user:pass@host:port  or  socks5://host:1080
    TERABOX_PROXY      = os.getenv("TERABOX_PROXY", "").strip()

    # API-only mode for TeraBox. Cookies/direct scraping are intentionally not
    # used for downloads; xAPIverse is the supported transport.
    TERABOX_API_ONLY   = os.getenv("TERABOX_API_ONLY", "1").strip().lower() not in ("0", "false", "off", "no")

    # xAPIverse TeraBox API — resolves share links server-side, so the
    # address block that stops direct scraping does not apply.
    # Free tier is 100 credits/month per key, so several keys can be set
    # and the bot moves to the next one when a key runs out.
    # Get a key at https://xapiverse.com/marketplace/terabox
    XAPIVERSE_KEY      = os.getenv("XAPIVERSE_KEY", "").strip()
    XAPIVERSE_KEY_2    = os.getenv("XAPIVERSE_KEY_2", "").strip()
    XAPIVERSE_KEY_3    = os.getenv("XAPIVERSE_KEY_3", "").strip()
    XAPIVERSE_KEY_4    = os.getenv("XAPIVERSE_KEY_4", "").strip()
    XAPIVERSE_KEY_5    = os.getenv("XAPIVERSE_KEY_5", "").strip()

    # YouTube cookies, in Netscape cookies.txt format. YouTube increasingly
    # answers datacenter IPs with a "Sign in to confirm you're not a bot"
    # challenge; exporting cookies from a signed-in browser may clear it on
    # some hosts. The Apify API path below is preferred when configured.
    YOUTUBE_COOKIES    = os.getenv("YOUTUBE_COOKIES", "")
    YOUTUBE_COOKIE_FILE = "/tmp/yt_dlp_cookies.txt"

    # Apify YouTube Downloader API. The default actor id is the one supplied
    # by the operator. Several tokens can be configured; a token that returns
    # quota/rate/auth errors is parked briefly and the next one is tried.
    APIFY_API_TOKEN    = (os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN", "")).strip()
    APIFY_API_TOKEN_2  = (os.getenv("APIFY_API_TOKEN_2") or os.getenv("APIFY_TOKEN_2", "")).strip()
    APIFY_API_TOKEN_3  = (os.getenv("APIFY_API_TOKEN_3") or os.getenv("APIFY_TOKEN_3", "")).strip()
    APIFY_API_TOKEN_4  = (os.getenv("APIFY_API_TOKEN_4") or os.getenv("APIFY_TOKEN_4", "")).strip()
    APIFY_API_TOKEN_5  = (os.getenv("APIFY_API_TOKEN_5") or os.getenv("APIFY_TOKEN_5", "")).strip()
    APIFY_YOUTUBE_ACTOR_ID = os.getenv("APIFY_YOUTUBE_ACTOR_ID", "UUhJDfKJT2SsXdclR").strip()
    YOUTUBE_API_ONLY = os.getenv("YOUTUBE_API_ONLY", "1").strip().lower() not in ("0", "false", "off", "no")
    APIFY_YOUTUBE_DEFAULT_QUALITY = os.getenv("APIFY_YOUTUBE_DEFAULT_QUALITY", "720p").strip()
    APIFY_YOUTUBE_FORMAT = os.getenv("APIFY_YOUTUBE_FORMAT", "mp4").strip()
    # Matches the Apify snippet: None/null by default. If you want to force a
    # particular KV store, set APIFY_YOUTUBE_STORE_IN_KVSTORE to that store id.
    APIFY_YOUTUBE_STORE_IN_KVSTORE = os.getenv("APIFY_YOUTUBE_STORE_IN_KVSTORE", "").strip()
    APIFY_YOUTUBE_TRANSCRIPTION = (
        os.getenv("APIFY_YOUTUBE_TRANSCRIPTION")
        or os.getenv("YOUTUBE_API_TRANSCRIPTION", "ALWAYS_TRANSCRIBE")
    ).strip()
    APIFY_YOUTUBE_TIMEOUT_SEC = int(os.getenv("APIFY_YOUTUBE_TIMEOUT_SEC", "900"))

    # Optional proxy for yt-dlp (YouTube fallback and friends). Same format as
    # TERABOX_PROXY: http://user:pass@host:port or socks5://host:1080
    YTDL_PROXY         = os.getenv("YTDL_PROXY", "").strip()

    # Optional proxy for Instagram. The private API refuses hosted
    # addresses regardless of cookie validity, so this is the only
    # dependable fix for /story and for profiles beyond the embed window.
    INSTAGRAM_PROXY    = os.getenv("INSTAGRAM_PROXY", "").strip()

    # Additional Instagram sessions, so work can be spread across several
    # accounts instead of hammering one. Set INSTAGRAM_COOKIES_2,
    # INSTAGRAM_COOKIES_3 ... in the same format as INSTAGRAM_COOKIES.
    # Rotating lowers the per-account request rate, which is what triggers
    # the "suspicious activity" lock.
    INSTAGRAM_COOKIES_2 = os.getenv("INSTAGRAM_COOKIES_2", "")
    INSTAGRAM_COOKIES_3 = os.getenv("INSTAGRAM_COOKIES_3", "")
    INSTAGRAM_COOKIES_4 = os.getenv("INSTAGRAM_COOKIES_4", "")
    INSTAGRAM_COOKIES_5 = os.getenv("INSTAGRAM_COOKIES_5", "")
    YTDL_MAX_SIZE_MB   = int(os.getenv("YTDL_MAX_SIZE_MB", "2000"))
    YTDL_TIMEOUT_SEC   = int(os.getenv("YTDL_TIMEOUT_SEC", "600"))   # 10 min max per download
    COOKIE_FILE_PATH   = "/tmp/yt_cookies.txt"

    # ── GoFile cloud upload (provide GOFILE_ACCOUNT_TOKEN from your account) ──
    GOFILE_ACCOUNT_TOKEN = os.getenv("GOFILE_ACCOUNT_TOKEN", "")   # Account token (not API key)
    GOFILE_ACCOUNT_ID    = os.getenv("GOFILE_ACCOUNT_ID", "")      # Your account ID

    # ── Queue ─────────────────────────────────────────────────────
    MAX_QUEUE_PER_USER  = int(os.getenv("MAX_QUEUE_PER_USER", "5"))

    # ── Auto-password ─────────────────────────────────────────────
    ENABLE_AUTO_PASSWORD = os.getenv("ENABLE_AUTO_PASSWORD", "1") == "1"

    # ── Referral ──────────────────────────────────────────────────
    # ── ZIP Queue completion animation ─────────────────────────────
    # Set QUEUE_END_GIF in Render env:
    #   • Giphy MP4 URL  → sends as animation
    #   • Telegram sticker file_id → sends as sticker
    QUEUE_END_GIF  = os.getenv("QUEUE_END_GIF", "")   # GIF after each ZIP done
    PROGRESS_GIF  = os.getenv("PROGRESS_GIF", "")    # GIF shown during download/upload progress

    REFERRAL_REQUIRED    = int(os.getenv("REFERRAL_REQUIRED", "5"))
    REFERRAL_REWARD_DAYS = int(os.getenv("REFERRAL_REWARD_DAYS", "7"))
