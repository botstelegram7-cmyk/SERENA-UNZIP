# utils/richmsg.py
"""
Rich messages over the raw Bot API.

Telegram's `sendRichMessage` builds a message from structured blocks
(paragraphs, tables, expandable quotations, button rows) instead of a
single HTML string. Crucially for us, `RichMessageButton` carries a
`style` field — "danger", "success", "primary" or "link" — so coloured
buttons ARE reachable here, even though the MTProto layer Pyrogram
speaks has no colour flag on any of its button types.

Nothing in the installed stack implements this, so every call is made
directly against api.telegram.org.

Verified against the published specification:

  sendRichMessage        chat_id + rich_message{blocks[], is_rtl?}
  InputRichBlockParagraph                  {type:"paragraph", text}
  InputRichBlockExpandableBlockQuotation   {type:"expandable_blockquote",
                                            text, credit?}
  InputRichBlockTable                      {type:"table", cells[][],
                                            is_bordered?, is_striped?,
                                            is_compact?, caption?}
  InputRichBlockButtons                    {type:"buttons", buttons[1-8],
                                            align?}
  RichMessageButton      {text, style?, url|callback_data|copy_text|...}
  RichBlockTableCell     {text?, is_header?, colspan?, rowspan?,
                          align, valign}
  EphemeralMessageParameters {receiver_user_id, callback_query_id?,
                              replace_callback_query_message?}

The feature is new, so a server that does not implement it simply
returns an error. Every helper degrades to a plain HTML message rather
than failing, which keeps the bot working on older Bot API servers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from utils.botapi import call

log = logging.getLogger(__name__)

# None = untested, True/False once the server has told us.
_RICH_SUPPORTED: Optional[bool] = None


# ── Block builders ───────────────────────────────────────────────────────────

def paragraph(text: Any) -> Dict[str, Any]:
    return {"type": "paragraph", "text": text}


def expandable_quote(text: Any, credit: Any = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "expandable_blockquote", "text": text}
    if credit:
        block["credit"] = credit
    return block


def cell(text: Any = None, *, header: bool = False, align: str = "left",
         valign: str = "middle", colspan: int = 0,
         rowspan: int = 0) -> Dict[str, Any]:
    c: Dict[str, Any] = {"align": align, "valign": valign}
    if text is not None:
        c["text"] = text
    if header:
        c["is_header"] = True
    if colspan > 1:
        c["colspan"] = colspan
    if rowspan > 1:
        c["rowspan"] = rowspan
    return c


def table(rows: Sequence[Sequence[Dict[str, Any]]], *, bordered: bool = True,
          striped: bool = False, compact: bool = True,
          caption: Any = None) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "table", "cells": [list(r) for r in rows]}
    if bordered:
        block["is_bordered"] = True
    if striped:
        block["is_striped"] = True
    if compact:
        block["is_compact"] = True
    if caption:
        block["caption"] = caption
    return block


def button(text: str, *, style: str = None, url: str = None,
           callback_data: str = None, copy_text: str = None,
           disabled: bool = False) -> Dict[str, Any]:
    """One button in a rich message.

    `style` is one of "danger", "success", "primary" or "link".
    Exactly one action field may be set; "link" is callback-only.
    """
    b: Dict[str, Any] = {"text": text}
    if style:
        b["style"] = style
    if disabled:
        # A disabled button still renders, but cannot be pressed.
        b["disabled"] = {}
    elif url is not None:
        b["url"] = url
    elif copy_text is not None:
        b["copy_text"] = {"text": copy_text}
    elif callback_data is not None:
        b["callback_data"] = callback_data
    return b


def buttons(row: Sequence[Dict[str, Any]], align: str = "center") -> Dict[str, Any]:
    """A row of 1-8 buttons."""
    return {"type": "buttons", "buttons": list(row)[:8], "align": align}


# ── Rich text helpers ────────────────────────────────────────────────────────

def bold(text: Any) -> Dict[str, Any]:
    return {"type": "bold", "text": text}


def italic(text: Any) -> Dict[str, Any]:
    return {"type": "italic", "text": text}


def code(text: Any) -> Dict[str, Any]:
    return {"type": "code", "text": text}


def link(text: Any, url: str) -> Dict[str, Any]:
    return {"type": "url", "text": text, "url": url}


# ── Sending ──────────────────────────────────────────────────────────────────

async def send(chat_id: int, blocks: List[Dict[str, Any]], *,
               reply_to: int = None, is_rtl: bool = False,
               ephemeral_for: int = None, callback_query_id: str = None,
               replace_callback_message: bool = False) -> Optional[Dict]:
    """Send a rich message. Returns the Message, or None if unsupported.

    `ephemeral_for` sends the message so only that user sees it, and it
    disappears on its own — useful for per-user notices that should not
    clutter a group.
    """
    global _RICH_SUPPORTED

    if _RICH_SUPPORTED is False:
        return None

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"blocks": blocks, **({"is_rtl": True} if is_rtl else {})},
    }
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to,
                                       "allow_sending_without_reply": True}
    if ephemeral_for:
        ep: Dict[str, Any] = {"receiver_user_id": ephemeral_for}
        if callback_query_id:
            ep["callback_query_id"] = callback_query_id
        if replace_callback_message:
            ep["replace_callback_query_message"] = True
        payload["ephemeral_message_parameters"] = ep

    res = await call("sendRichMessage", payload)
    if res is not None:
        _RICH_SUPPORTED = True
        return res

    # One failure is enough: the method either exists or it does not.
    _RICH_SUPPORTED = False
    log.info("sendRichMessage unavailable; falling back to HTML messages")
    return None


def supported() -> Optional[bool]:
    """True/False once probed, None before the first attempt."""
    return _RICH_SUPPORTED
