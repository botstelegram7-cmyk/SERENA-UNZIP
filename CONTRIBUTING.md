# Contributing to Serena Unzip

Thanks for taking an interest in the project. Bug reports, fixes and new
platform support are all welcome.

## Credit / attribution

**Please do not remove the original author's credit.**

This project is created and maintained by **[@botstelegram7-cmyk](https://github.com/botstelegram7-cmyk)**.
If you fork, redeploy, rehost or build on this bot:

- Keep the copyright line in [`LICENSE`](LICENSE) intact.
- Keep the credit in the README and in the bot's `/start` and `/version`
  output.
- You are very welcome to add your own name alongside it — just don't
  replace the original.

This is also what the MIT licence requires: the copyright notice must be
included in all copies or substantial portions of the software. Stripping
it is a licence violation, not just bad manners.

## Reporting a bug

Please include:

1. What you did (the exact command or the link you sent — redact anything
   private).
2. What you expected, and what actually happened.
3. The output of `/version` — it reports the running commit, so it is
   obvious whether the fix you are missing is even deployed.
4. For Instagram, Google Drive or TeraBox problems, also run the matching
   diagnostic: `/igtest`, `/tbtest <link>`.

A screenshot of the bot's error message is genuinely useful — the messages
are written to name the real cause.

## Development setup

```bash
git clone https://github.com/botstelegram7-cmyk/SERENA-UNZIP.git
cd SERENA-UNZIP
pip install -r requirements.txt
cp .env.example .env     # then fill it in
python bot.py
```

### Required environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `API_ID`, `API_HASH` | ✅ | From <https://my.telegram.org> |
| `BOT_TOKEN` | ✅ | From [@BotFather](https://t.me/BotFather) |
| `OWNER_IDS` | ✅ | Comma-separated Telegram user IDs |
| `MONGO_URI` | ➖ | Falls back to in-memory storage when unset |
| `INSTAGRAM_COOKIES` | ➖ | Needed for stories and `/profile` |
| `TERABOX_COOKIE` | ➖ | The `ndus` cookie; TeraBox walls anonymous IPs |

## Code style

The codebase is plain `asyncio` + [pyrofork](https://pypi.org/project/pyrofork/).
A few conventions that matter:

- **Never use a bare `except:`.** It swallows `asyncio.CancelledError`,
  which silently breaks `/cancel`. Use `except Exception:`.
- **User-facing strings are Hinglish.** Keep that voice.
- **Error messages must name the real cause.** Don't tell someone their
  cookies expired when the actual problem is a rate-limited IP — that
  sends them chasing the wrong fix. If you can distinguish two failures,
  say which one happened.
- **Don't cache signed URLs.** Instagram and TeraBox links expire within
  minutes; resolve them right before use.
- Budget Telegram captions with `utils.instagram._visible_len()`. Telegram
  counts the *parsed* text in UTF-16 units, not raw HTML length.

## Before you open a PR

- Run a syntax check across the tree:
  ```bash
  for f in bot.py config.py database.py server.py utils/*.py; do
      python -c "import ast; ast.parse(open('$f').read())" || echo "FAIL $f"
  done
  ```
- Confirm the bot still imports and that handlers register:
  ```bash
  python -c "import bot"
  ```
  Handlers defined *below* `asyncio.run(main())` are never registered —
  keep the `MAIN` block at the end of the file.
- Say in the PR what you actually tested, and what you couldn't. "Parsing
  is tested, the live download path is not" is a useful, honest note.

## Adding a new platform

1. Detect it in `utils/link_parser.py` (`classify_link`). Match on the
   **hostname**, not a substring — `"ok.co" in url` also matches
   `ex.com`.
2. If yt-dlp supports the site, adding the domain to `YTDL_DOMAINS` is
   usually enough. Check first:
   ```bash
   yt-dlp --list-extractors | grep -i <site>
   ```
3. If it doesn't, write a resolver in `utils/` (see `utils/terabox.py`)
   and route to it from `handle_links_download_all`.

Please don't add support for sites that exist mainly to distribute
pirated material.
