# config.py
import os
from dotenv import load_dotenv

load_dotenv()


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
    OWNER_IDS         = {6518065496, 1598576202}
    OWNER_USERNAME    = os.getenv("OWNER_USERNAME", "technicalserena")

    # ── General ──────────────────────────────────────────────────
    BOT_NAME  = "Serena Unzip"
    START_PIC = os.getenv("START_PIC", None)
    TEMP_DIR  = os.getenv("TEMP_DIR", "downloads")

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
    REFERRAL_REQUIRED    = int(os.getenv("REFERRAL_REQUIRED", "5"))
    REFERRAL_REWARD_DAYS = int(os.getenv("REFERRAL_REWARD_DAYS", "7"))
