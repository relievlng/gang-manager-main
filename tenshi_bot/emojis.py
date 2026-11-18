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
    "active":  ("gm_active_w", "🔥"),
    "account": ("gm_account", "🗓️"),
    "joined":  ("gm_joined",  "📥"),
    "vouch":   ("gm_vouch",   "🤝"),
    "montage": ("gm_montage", "🎬"),
    "vote":    ("gm_vote",    "🗳️"),
    "play":    ("gm_play",    "▶️"),
    "yes":     ("gm_yes",     "✅"),
    "no":      ("gm_no",      "❌"),
    "clock":   ("gm_clock",   "⏱️"),
    "accept":  ("gm_accept_w", "🏆"),
    "deny":    ("gm_deny_w",  "🔒"),
    # status-card icons
    "members": ("gm_members", "👥"),
    "servers": ("gm_servers", "🏴"),
    "wars":    ("gm_wars",    "⚔️"),
    "tickets": ("gm_tickets", "🎫"),
    "bots":    ("gm_bots",    "🤖"),
    "dot_on":  ("gm_dot_on",  "🟢"),
    "dot_off": ("gm_dot_off", "⚫"),
    "dot_down": ("gm_dot_down", "🔴"),
    # general icons (auto-substituted into embeds by gmify)
    "warn":    ("gm_warn",    "⚠️"),
    "skull":   ("gm_skull",   "💀"),
    "globe":   ("gm_globe",   "🌐"),
    "card":    ("gm_card",    "💳"),
    "radio":   ("gm_radio",   "📻"),
    "list":    ("gm_list",    "📋"),
    "trash":   ("gm_trash",   "🗑️"),
    "refresh": ("gm_refresh", "🔄"),
    "shield":  ("gm_shield",  "🛡️"),
    "money":   ("gm_money",   "💰"),
    "crown":   ("gm_crown",   "👑"),
    "key":     ("gm_key",     "🔑"),
    "home":    ("gm_home",    "🏠"),
    "alert":   ("gm_alert",   "🚨"),
    "hammer":  ("gm_hammer",  "🔨"),
    "cart":    ("gm_cart",    "🛒"),
    "chart":   ("gm_chart",   "📊"),
    "wave":    ("gm_wave",    "👋"),
    "edit":    ("gm_edit",    "📝"),
    "party":   ("gm_party",   "🎉"),
    "gun":     ("gm_gun",     "🔫"),
    "tools":   ("gm_tools",   "🛠️"),
    "puzzle":  ("gm_puzzle",  "🧩"),
    "n1":      ("gm_n1",      "1️⃣"),
    "n2":      ("gm_n2",      "2️⃣"),
    "n3":      ("gm_n3",      "3️⃣"),
    # progress-bar segments (no unicode fallback — _vote_bar falls back to squares)
    "vbar_lf": ("vbar_lf", ""),
    "vbar_le": ("vbar_le", ""),
    "vbar_mf": ("vbar_mf", ""),
    "vbar_me": ("vbar_me", ""),
    "vbar_rf": ("vbar_rf", ""),
    "vbar_re": ("vbar_re", ""),
    "vbar_tip": ("vbar_tip", ""),
}

# app_id -> {key: "<:name:id>"}
_CACHE: dict[int, dict[str, str]] = {}

# unicode emoji -> key, built from the fallbacks (bar pieces excluded). Used by
# gmify() to swap plain emojis for custom ones across every embed.
_UNICODE_EMOJI_MAP: dict[str, str] = {
    fb: key for key, (_name, fb) in EMOJI.items() if fb and not key.startswith("vbar")
}
# extra unicode variants that should resolve to an existing custom glyph
_UNICODE_EMOJI_MAP.update({
    "✉": "app", "✉️": "app",
    "✖️": "no", "✖": "no", "✕": "no", "❎": "no",
    "✓": "yes", "☑️": "yes", "✔️": "yes", "✔": "yes",
    "🛠": "tools",
})


def gmify(text: str, client) -> str:
    """Swap known unicode emojis in `text` for the posting bot's custom ones.
    A safe no-op before the bot has uploaded them (em returns the same glyph)."""
    if not text or client is None:
        return text
    for uni, key in _UNICODE_EMOJI_MAP.items():
        base = uni.rstrip("️")
        if base in text:
            cust = em(client, key)
            if cust != uni:
                text = text.replace(uni, cust).replace(base, cust)
    return text


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


def _key_for_emoji(e) -> "str | None":
    if e is None:
        return None
    s = str(e)
    return _UNICODE_EMOJI_MAP.get(s) or _UNICODE_EMOJI_MAP.get(s.rstrip("️"))


def _swap_component_emoji(obj, client) -> None:
    e = getattr(obj, "emoji", None)
    if e is None or getattr(e, "id", None) is not None:  # empty or already custom
        return
    key = _key_for_emoji(e)
    if not key:
        return
    cust = em(client, key)
    if cust.startswith("<:"):
        try:
            obj.emoji = discord.PartialEmoji.from_str(cust)
        except Exception:  # noqa: BLE001
            pass


def customize_view(view, client):
    """Swap unicode emojis on a view/modal's buttons and select options for the
    posting bot's custom ones. Handles modal Labels that wrap a Select too.
    Best-effort; call it just before the view/modal is sent."""
    if view is None or client is None:
        return view
    try:
        for child in getattr(view, "children", []) or []:
            _swap_component_emoji(child, client)
            comp = getattr(child, "component", None)  # modal Label wraps a component
            for target in (child, comp):
                if target is None:
                    continue
                _swap_component_emoji(target, client)
                for opt in getattr(target, "options", []) or []:
                    _swap_component_emoji(opt, client)
    except Exception:  # noqa: BLE001
        pass
    return view


__all__ = ["EMOJI", "sync_app_emojis", "em", "gmify", "customize_view"]
