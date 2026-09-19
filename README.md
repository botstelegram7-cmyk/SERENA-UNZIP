<div align="center">

```
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║    ███████╗███████╗██████╗ ███████╗███╗   ██╗ █████╗        ║
║    ██╔════╝██╔════╝██╔══██╗██╔════╝████╗  ██║██╔══██╗       ║
║    ███████╗█████╗  ██████╔╝█████╗  ██╔██╗ ██║███████║       ║
║    ╚════██║██╔══╝  ██╔══██╗██╔══╝  ██║╚██╗██║██╔══██║       ║
║    ███████║███████╗██║  ██║███████╗██║ ╚████║██║  ██║       ║
║    ╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝       ║
║                                                              ║
║              🌸  SERENA UNZIP BOT v2  🌸                     ║
║         Advanced Telegram File Processing Bot                ║
╚══════════════════════════════════════════════════════════════╝
```

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.x-green?style=for-the-badge)](https://pyrogram.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-Motor-brightgreen?style=for-the-badge&logo=mongodb)](https://mongodb.com)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)
[![Telegram](https://img.shields.io/badge/Bot-@SerenaBotHere-blue?style=for-the-badge&logo=telegram)](https://t.me)

> **All-in-one Telegram bot for extracting archives, downloading media, compressing videos, and much more.**

---

</div>

## ✨ Features

| Category | Features |
|----------|----------|
| 📦 **Archives** | ZIP, RAR, 7z, TAR extract · Password auto-detect · Nested archives · Preview before extract |
| 🎬 **Video** | Compress (CRF control) · Resize · Watermark · Merge · Split · Screenshot · Subtitle extract |
| 📥 **Downloader** | Instagram Photos/Reels/Stories/Highlights · YouTube · Twitter · TikTok · Facebook · M3U8 |
| ☁️ **Upload** | Telegram · GoFile cloud · Auto thumbnail · Custom caption |
| 📦 **ZIP Queue** | Batch extract up to 100 ZIPs · Sequence preserved · Progress ETA · Auto cache delete |
| 🔒 **Security** | Per-user rate limit · Daily task/size limit · Premium system · Ban/unban users |
| 🛠️ **Utilities** | Rename · PDF tools · Audio extract · File info · Auto watermark · Google Drive |

---

## 🚀 Quick Deploy

### 🟣 Render (Recommended — Free Tier Available)

1. Fork this repo on GitHub
2. Go to [render.com](https://render.com) → **New Web Service**
3. Connect your GitHub repo
4. Set **Build Command**: `pip install -r requirements.txt`
5. Set **Start Command**: `uvicorn server:fastapi_app --host 0.0.0.0 --port $PORT`
6. Add all [Environment Variables](#-environment-variables) in Render dashboard
7. Click **Deploy** ✅

---

### 🔵 Railway

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Init project
railway init

# 4. Deploy
railway up
```

Add env variables in Railway dashboard → **Variables** tab.

---

### 🟠 Fly.io

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Launch (first time)
fly launch

# Set secrets
fly secrets set API_ID=your_id API_HASH=your_hash BOT_TOKEN=your_token MONGO_URI=your_mongo

# Deploy
fly deploy
```

---

### 🐳 Docker (Any VPS)

```bash
# 1. Clone repo
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 2. Copy env file
cp .env.example .env
nano .env   # fill in your values

# 3. Build & run
docker build -t serena-bot .
docker run -d --name serena --env-file .env -p 8000:8000 serena-bot

# View logs
docker logs -f serena
```

---

### 🖥️ Ubuntu / Debian VPS (Manual)

```bash
# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Install dependencies
sudo apt install -y python3 python3-pip python3-venv ffmpeg p7zip-full unrar git

# 3. Clone repo
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 4. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 5. Install Python packages
pip install -r requirements.txt

# 6. Setup environment
cp .env.example .env
nano .env   # fill in your values

# 7. Run with screen (stays alive after SSH disconnect)
screen -S serena
python -m uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
# Press Ctrl+A then D to detach

# Reconnect later:
screen -r serena
```

**Auto-restart with systemd:**
```bash
sudo nano /etc/systemd/system/serena.service
```
```ini
[Unit]
Description=Serena Unzip Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/serena-unzip-bot
Environment="PATH=/home/ubuntu/serena-unzip-bot/.venv/bin"
ExecStart=/home/ubuntu/serena-unzip-bot/.venv/bin/uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable serena
sudo systemctl start serena
sudo systemctl status serena  # check status
```

---

### 💻 Local PC (Windows / Mac / Linux)

```bash
# 1. Install Python 3.10+ from python.org
# 2. Install FFmpeg:
#    Windows: https://ffmpeg.org/download.html (add to PATH)
#    Mac:     brew install ffmpeg
#    Linux:   sudo apt install ffmpeg

# 3. Clone
git clone https://github.com/youruser/serena-unzip-bot
cd serena-unzip-bot

# 4. Install deps
pip install -r requirements.txt

# 5. Setup .env
cp .env.example .env
# Edit .env with your values

# 6. Run
uvicorn server:fastapi_app --host 0.0.0.0 --port 8000
```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | ✅ | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | ✅ | Bot token from [@BotFather](https://t.me/BotFather) |
| `MONGO_URI` | ✅ | MongoDB connection string (free at [mongodb.com](https://mongodb.com)) |
| `TERABOX_COOKIE` | ➖ | The `ndus` cookie from a logged-in TeraBox session. Without it TeraBox answers errno 140 (IP walled) to datacenter servers. Chrome → F12 → Application → Cookies → terabox.com → `ndus`. A full Netscape `cookies.txt` export, a `k=v; k=v` string, or the bare `ndus` value are all accepted. |
| `TERABOX_PROXY` | ➖ | HTTP/SOCKS proxy for TeraBox, e.g. `http://user:pass@host:port`. TeraBox withholds signed download links from datacenter IPs even with a valid cookie, so a residential proxy is the only reliable fix. |
| `OWNER_IDS` | ✅ | Comma-separated owner user IDs, e.g. `12345678,87654321`. Admin commands are disabled if unset. |
| `LOG_CHANNEL` | ✅ | Channel ID for bot logs (e.g. `-100xxxxxxxxx`) |
| `FORCE_SUB_CHANNEL` | ⬜ | Channel username users must join |
| `INSTAGRAM_COOKIES` | ⚠️ | Instagram cookies (Netscape or header format). **Strongly recommended** — Instagram blocks anonymous datacenter IPs. Required for Stories/Highlights. |
| `QUEUE_END_GIF` | ⬜ | Giphy MP4 URL or Telegram sticker file_id — sent after each ZIP extract |
| `TEMP_DIR` | ⬜ | Temp folder path (default: `./downloads`) |
| `MAX_FILE_SIZE_MB` | ⬜ | Max file size to process (default: `2000`) |
| `FREE_DAILY_TASK_LIMIT` | ⬜ | Daily task limit for free users (default: `30`) |
| `AUTO_DELETE_MINUTES` | ⬜ | Delete temp files after N minutes (default: `30`) |

---

## 📸 Instagram Downloader

Instagram media is handled by a dedicated module (`utils/instagram.py`) instead of
relying on yt-dlp alone — yt-dlp fails on photo posts with *"There is no video in
this post"* and only ever returns the first item of a carousel.

**What works now**

| Content | Result |
|---------|--------|
| 📷 Photo post | Full-resolution image (not a thumbnail crop) |
| 🎠 Carousel | **Every** photo/video, in order, delivered as one Telegram album |
| 🎬 Reel / video | Highest quality with audio |
| 📲 Story / ⭐ Highlight | Downloaded (requires cookies) |
| 🔗 Plain link pasted in chat | Auto-detected — no `/ytdl` needed |

**How it works** — five strategies are tried in order, first success wins:

1. **Web GraphQL** (`/graphql/query`) — full carousel, highest resolution
2. **Web API v1** (`/api/v1/media/<id>/info/`) — shortcode is converted to a numeric media id
3. **Embed page** (`/p/<code>/embed/captioned/`) — works with no auth at all
4. **OpenGraph tags** — last-resort single item
5. **yt-dlp** — fallback for reels, stories and highlights

Images are fetched straight from Instagram's CDN in parallel, with the highest
resolution picked from each `*_versions` list. Truncated/placeholder files are
rejected automatically.

### Image normalisation (fixes `PHOTO_EXT_INVALID`)

Instagram's CDN often serves **WebP or HEIC bytes from a URL ending in `.jpg`**.
Telegram inspects the real content and rejects such uploads with:

```
[400 PHOTO_EXT_INVALID] The photo extension is invalid
```

Every downloaded image is therefore checked by **magic bytes, never by filename**,
and normalised before upload:

| Situation | Action |
|-----------|--------|
| WebP / HEIC bytes | Re-encoded to real JPEG (quality 90) |
| Wrong extension (PNG named `.jpg`) | Renamed to the correct one |
| Alpha channel | Flattened to RGB |
| Over 10 MB | Re-compressed until it fits |
| `width + height > 10000` px | Downscaled (Lanczos) |

If conversion somehow fails, the file is sent as a **document** instead of a photo,
and any upload still rejected by Telegram is automatically retried as a document —
so media is never silently lost.

> `pillow-heif` is included in `requirements.txt` for HEIC support. If it is
> missing the module degrades gracefully and simply sends those rare files as
> documents.

**No confirmation buttons** — an Instagram link starts downloading immediately.
There is no quality menu to tap through (Instagram serves one quality anyway).

### Captions

Every post's text is attached to the media. The description is wrapped in a
Telegram **expandable blockquote**, so long captions collapse behind a
"Show more" tap instead of flooding the chat:

```
<b>Title</b>                                  ← if the post has one
👤 <b>Full Name</b> @username                 ← links to the profile
❤️ 15,234  👁 98,000                          ← stats when available

<blockquote expandable>Full description…</blockquote>

🔗 Open on Instagram
```

Caption text is HTML-escaped. Length is budgeted on the **visible** text in
UTF-16 units — exactly what Telegram counts — so markup and escaped characters
(`<`, `>`, `&`) no longer eat into the limit.

**Long reel descriptions** are never lost: the media keeps a trimmed caption and
the complete text follows in separate messages (up to 4096 chars each), each one
still inside its own expandable quote. Splitting happens on word boundaries with
byte-exact reassembly, verified against 50 000-character inputs.

The title is rendered in a bold sans-serif Unicode font, with the author,
stats and link in bold entities.
Metadata is read from the GraphQL/v1 responses, and falls back to scraping the
embed page or `og:` tags, then to yt-dlp's info JSON.

### Albums

Carousels are always delivered as Telegram **albums**, never as loose messages.
Posts with more than 10 items are split into consecutive groups of 10 (the API
maximum). The caption rides on the first item. If an album is rejected, its
items are re-sent individually so nothing is ever lost.

**Commands**

```
/insta <url>     — Instagram downloader (aliases: /ig, /instagram)
<paste link>     — Auto-detected, downloads straight away
```

---

## 🍪 Instagram Cookies Setup

Instagram now blocks **anonymous requests from datacenter IPs** (Render, Railway,
Heroku, VPS…) with `401 login_required` / `429`. Cookies are therefore
**strongly recommended** — and mandatory for Stories, Highlights and private content.

1. Install the [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) or [Cookie Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) extension
2. Open Instagram and log in (use a **throwaway account** — never your main one)
3. Export the cookies in **Netscape** format
4. Copy the exported text
5. In Render/Railway → Environment Variables → `INSTAGRAM_COOKIES` → paste it

Both formats are accepted:

* Netscape TSV (`domain  TRUE  /  TRUE  0  sessionid  abc...`)
* Header style (`sessionid=abc; csrftoken=xyz`)

> ⚠️ Sessions expire. If downloads start failing with a "login required" message,
> export fresh cookies and update the variable.

---

## 📋 Bot Commands

```
/start       — Welcome message
/help        — Full command list
/unzip       — Extract archive (reply to file)
/zipqueue    — Start ZIP batch queue (up to 100 files)
/zq          — Short alias for /zipqueue
/zqpass      — Set queue password
/cancelqueue — Cancel active queue
/insta       — Instagram photos, carousels, reels, stories (alias: /ig)
Paste any link  — YouTube, TikTok, Twitter/X, Facebook, Spotify, SoundCloud,
                  Pinterest, Reddit, Rumble and 1800+ other sites via yt-dlp;
                  direct file URLs; m3u8/DASH streams; Google Drive; and file
                  hosts such as MEGA, MediaFire and Terabox. Links without a
                  file extension are probed at runtime and routed correctly.
/profile     — Bulk-download a profile's latest posts (alias: /bulk)
/story       — Download a user's active stories
/version     — Build, changelog and live health (alias: /changelog)
/terabox     — Download a TeraBox share link (alias: /tb)
/tbtest      — Owner: probe TeraBox mirrors
/clearcache  — Owner: drop the Instagram file_id cache
/ytdl        — Download from Twitter/TikTok/Facebook etc.
/compress    — Compress video (reply to video)
/resize      — Resize video
/merge       — Merge multiple videos
/split       — Split video by time
/audio       — Extract audio from video
/watermark   — Add text watermark
/screenshot  — Capture frame at timestamp
/subs        — Extract subtitles
/rename      — Rename file
/info        — Show file metadata
/pdf         — PDF tools
/zip         — Create ZIP from files
/mystats     — Your usage statistics
/cancel      — Cancel current task
```

---

## 🗂️ Project Structure

```
serena-unzip-bot/
├── bot.py              # Main bot — all handlers
├── server.py           # FastAPI health check server
├── config.py           # Environment config
├── database.py         # MongoDB operations
├── requirements.txt    # Python dependencies
├── Dockerfile          # Docker deployment
├── .env.example        # Environment template
└── utils/
    ├── media_tools.py  # FFmpeg video processing
    ├── ytdl_tools.py   # yt-dlp download wrapper
    ├── extractors.py   # Archive extraction
    ├── progress.py     # Upload/download progress
    ├── cloud_upload.py # GoFile upload
    ├── cleanup.py      # Temp file management
    ├── zip_creator.py  # ZIP creation
    ├── pdf_tools.py    # PDF operations
    ├── m3u8_tools.py   # M3U8/HLS download
    ├── http_downloader.py
    ├── link_parser.py
    ├── file_splitter.py
    ├── gdrive.py
    └── password_list.py
```

---

## ⚡ Suggested Improvements

- **`/preview`** — List files inside ZIP before extracting
- **`/convert`** — Convert between archive formats (ZIP ↔ TAR ↔ 7z)
- **Batch Instagram** — Download last N posts from a profile
- **Auto-thumbnail** — Generate video preview grid
- **Webhook mode** — Faster response via webhook instead of polling
- **Scheduled tasks** — Queue tasks for later
- **File deduplication** — Skip duplicate files in ZIP
- **Password manager** — Save frequently used passwords per user

---

## 🛠️ Built With

- **[Pyrogram](https://pyrogram.org)** — Telegram MTProto client
- **[FFmpeg](https://ffmpeg.org)** — Video/audio processing
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — Media downloader
- **[Motor](https://motor.readthedocs.io)** — Async MongoDB driver
- **[FastAPI](https://fastapi.tiangolo.com)** — Health check server
- **[py7zr](https://py7zr.readthedocs.io)** — 7-zip support

---

## 👨‍💻 Credits

<div align="center">

| Role | Contact |
|------|---------|
| 💻 **Developer & Owner** | [![Telegram](https://img.shields.io/badge/Telegram-@TechnicalSerena-blue?logo=telegram)](https://t.me/TechnicalSerena) |
| 🎨 **Co-Developer** | [![Telegram](https://img.shields.io/badge/Telegram-@Xioqui__Xin-blue?logo=telegram)](https://t.me/Xioqui_Xin) |
| 📸 **Instagram** | [![Instagram](https://img.shields.io/badge/Instagram-@Prince572002-E4405F?logo=instagram)](https://instagram.com/Prince572002) |

</div>

---

## 🤝 Contributing

Bug reports and pull requests are welcome — see
**[CONTRIBUTING.md](CONTRIBUTING.md)** for local setup, code style and how to
add support for a new platform.

When reporting a bug, please include the output of `/version` (it shows the
exact commit that is running, so it's clear whether the fix you're missing is
even deployed) and, for Instagram or TeraBox issues, `/igtest` or
`/tbtest <link>`.

---

## 📄 License

Released under the **[MIT License](LICENSE)** — free to use, modify, self-host
and distribute.

### ⚠️ Please keep the credit

Forking and redeploying this bot is absolutely fine. If you do, please:

- **Keep the copyright line in [`LICENSE`](LICENSE) intact.**
- **Keep the credit** in this README and in the bot's `/start` and `/version`
  output.
- Feel free to **add your own name alongside** it — just don't replace the
  original.

Retaining the copyright notice isn't only courtesy: the MIT licence requires
it to be included in all copies or substantial portions of the software, so
removing it is a licence violation.

<div align="center">

**Made with ❤️ by [@TechnicalSerena](https://t.me/TechnicalSerena)**

*Star ⭐ this repo if it helped you!*

</div>
