# utils/botapi.py
"""
Direct Bot API calls over HTTP.

Pyrogram speaks MTProto, which does not carry some newer Bot-API-only
fields. Where a feature exists in the Bot API but not in the installed
library, we can still reach it by calling api.telegram.org ourselves with
the same bot token.

Verified against the installed pyrofork (2.3.69):

  * copy_text  (Bot API 7.11) — SUPPORTED natively, no HTTP needed.
  * style      (Bot API 9.4, coloured buttons) — the MTProto layer has no
    colour flag on any of its 16 KeyboardButton types, so it cannot be
    sent by Pyrogram at all. It CAN be sent over HTTP, but a message sent
    this way is a separate send: it cannot add colour to a keyboard that
    Pyrogram already delivered.

So this module is used for messages that want Bot-API-only styling, and
falls back cleanly when Telegram rejects an unknown field.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from config import Config

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"

# Set once we learn whether this Bot API server understands `style`.
_STYLE_SUPPORTED: Optional[bool] = None


def _url(method: str) -> str:
    return f"{API_ROOT}/bot{Config.BOT_TOKEN}/{method}"


async def call(method: str, payload: Dict[str, Any],
               timeout: int = 30) -> Optional[Dict]:
    """Invoke a Bot API method. Returns the `result` object, or None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(method), json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
        if data.get("ok"):
            return data.get("result")
        log.debug("botapi %s failed: %s", method, data.get("description"))
        return None
    except Exception as e:
        log.debug("botapi %s error: %s", method, e)
        return None


def styled_button(text: str, *, callback_data: str = None, url: str = None,
                  copy_text: str = None, style: str = None,
                  icon_custom_emoji_id: str = None) -> Dict[str, Any]:
    """Build an inline button dict for the raw Bot API.

    `style` is one of "primary", "success" or "danger" (Bot API 9.4).
    """
    btn: Dict[str, Any] = {"text": text}
    if callback_data is not None:
        btn["callback_data"] = callback_data
    elif url is not None:
        btn["url"] = url
    elif copy_text is not None:
        btn["copy_text"] = {"text": copy_text}
    if style:
        btn["style"] = style
    if icon_custom_emoji_id:
        btn["icon_custom_emoji_id"] = icon_custom_emoji_id
    return btn


async def send_message(chat_id: int, text: str,
                       keyboard: Optional[List[List[Dict]]] = None,
                       reply_to: Optional[int] = None,
                       parse_mode: str = "HTML",
                       disable_preview: bool = True) -> Optional[Dict]:
    """Send a message with a (optionally styled) inline keyboard.

    If the server rejects `style` — older Bot API — the buttons are resent
    without it rather than the message failing outright.
    """
    global _STYLE_SUPPORTED

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "link_preview_options": {"is_disabled": bool(disable_preview)},
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to,
                                       "allow_sending_without_reply": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}

    res = await call("sendMessage", payload)
    if res is not None:
        if keyboard and _STYLE_SUPPORTED is None:
            _STYLE_SUPPORTED = any("style" in b for row in keyboard for b in row)
        return res

    # Retry once with every `style` key stripped
    if keyboard and any("style" in b for row in keyboard for b in row):
        _STYLE_SUPPORTED = False
        plain = [[{k: v for k, v in b.items() if k != "style"} for b in row]
                 for row in keyboard]
        payload["reply_markup"] = {"inline_keyboard": plain}
        return await call("sendMessage", payload)
    return None


async def get_me() -> Optional[Dict]:
    return await call("getMe", {})


async def probe_style_support() -> bool:
    """Report whether this Bot API server accepts the `style` field.

    Determined from real traffic: the first styled send that succeeds sets
    it True, the first rejection sets it False. Unknown until then.
    """
    return bool(_STYLE_SUPPORTED)
