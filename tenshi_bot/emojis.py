"""Custom application emojis for Gang Manager.

Each bot — the main bot and every pool bot — uploads this set to *its own*
application on startup, because Discord application emojis only render for the
application that owns them. Everything falls back to a plain unicode glyph when
the upload hasn't happened yet or failed, so the bot always works.

The PNGs live in ``assets/emoji/<name>.png`` (generated, branded tiles).
"""
from __future__ import annotations

import logging
import os

import discord

log = logging.getLogger("gang.emoji")

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "emoji")

# key -> (emoji name uploaded to the app, unicode fallback)
EMOJI: dict[str, tuple[str, str]] = {
    "app":     ("gm_app",     "📨"),
    "role":    ("gm_role",    "🎯"),
    "active":  ("gm_active",  "🔥"),
    "account": ("gm_account", "🗓️"),
    "joined":  ("gm_joined",  "📥"),
    "vouch":   ("gm_vouch",   "🤝"),
    "montage": ("gm_montage", "🎬"),
    "vote":    ("gm_vote",    "🗳️"),
    "play":    ("gm_play",    "▶️"),
    "yes":     ("gm_yes",     "✅"),
    "no":      ("gm_no",      "❌"),
    "clock":   ("gm_clock",   "⏱️"),
    "accept":  ("gm_accept",  "🏆"),
    "deny":    ("gm_deny",    "🔒"),
}

# app_id -> {key: "<:name:id>"}
_CACHE: dict[int, dict[str, str]] = {}


def _app_id(client) -> int | None:
    if client is None:
        return None
    aid = getattr(client, "application_id", None)
    if aid:
        return int(aid)
    user = getattr(client, "user", None)
    return int(user.id) if user else None


async def sync_app_emojis(client: discord.Client) -> None:
    """Upload any missing Gang Manager emojis to this bot's application and cache
    their mention strings. Idempotent (skips ones already uploaded) and
    best-effort — a failure just leaves the unicode fallbacks in place."""
    try:
        app_id = _app_id(client)
        if not app_id:
            return
        existing = {e.name: e for e in await client.fetch_application_emojis()}
        cache = _CACHE.setdefault(app_id, {})
        made = 0
        for key, (name, _fb) in EMOJI.items():
            em_obj = existing.get(name)
            if em_obj is None:
                path = os.path.join(_ASSET_DIR, f"{name}.png")
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, "rb") as fh:
                        em_obj = await client.create_application_emoji(name=name, image=fh.read())
                    made += 1
                except discord.HTTPException as exc:
                    log.warning("emoji: upload %s failed: %s", name, exc)
                    continue
            cache[key] = f"<:{em_obj.name}:{em_obj.id}>"
        if made:
            log.info("emoji: uploaded %d new emoji(s) to application %s", made, app_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("emoji: sync failed: %s", exc)


def em(client, key: str) -> str:
    """The custom emoji for ``key`` as the given bot renders it, or its unicode
    fallback. ``client`` is the bot that will post the message (or its app id)."""
    name, fallback = EMOJI.get(key, ("", key))
    app_id = client if isinstance(client, int) else _app_id(client)
    if app_id is not None:
        got = _CACHE.get(int(app_id), {}).get(key)
        if got:
            return got
    return fallback


__all__ = ["EMOJI", "sync_app_emojis", "em"]
