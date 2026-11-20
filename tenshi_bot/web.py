"""Optional web roster manager.

A small aiohttp server, run inside the bot process, that lets leadership view
the roster in a browser and remove members - which takes their gang roles off
in Discord through the live bot connection.

It only turns on when BOTH are set:
  * PORT             - Railway sets this for a web service; without it the
                       server stays off and the bot runs exactly as before.
  * WEB_ADMIN_KEY    - a secret you choose. Every page load and API call must
                       present it, so the role-removing endpoints are never
                       open to the public.

Nothing here changes the bot's Discord behaviour; it's purely an extra door
into the same roster data and role actions the /roster commands already use.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time

from .core import *  # noqa: F401,F403
from .billing import (
    subscription_state,
    effective_state,
    is_active,
    peek_active,
    move_subscription,
    grant,
    expire,
    is_owner,
    guild_for_sub,
    web_base_url,
    note_base_url,
    PLANS,
    DEFAULT_PLAN,
    is_banned,
    ban_user,
    unban_user,
    list_bans,
    has_admin_access,
    is_admin_staff,
    add_admin_staff,
    remove_admin_staff,
    list_admin_staff,
    set_admin_staff_name,
    shop_log_add,
    shop_log_list,
    owner_ids,
    audit_log_add,
    audit_log_list,
)
from .settings import CONFIG_SETTINGS, _SETTING_EMOJI, _LOG_KEYS, ensure_log_channels
from .pool import (
    assign_bot_for_guild,
    pool_overview,
    guild_bot_status,
    smoothed_latency_ms,
    add_tokens,
    free_bot,
    remove_bot,
    restart_bot,
    disable_bot,
    enable_bot,
    disable_all_bots,
    enable_all_bots,
    restart_all_bots,
    set_pool_enabled,
    set_admin_guild,
    admin_guild,
    hub_commands_off,
    set_hub_commands_off,
)

try:  # aiohttp ships with discord.py; guard so the package still imports without it
    from aiohttp import web as _aioweb
except Exception:  # pragma: no cover - only in trimmed test envs
    _aioweb = None

try:
    import aiohttp as _aiohttp
except Exception:  # pragma: no cover
    _aiohttp = None


# --------------------------------------------------------------------------- #
# Roster data / actions (pure-ish; unit-tested without a running server)
# --------------------------------------------------------------------------- #

def _gang_role_ids(guild: "discord.Guild", store: dict) -> set[int]:
    """Every role that counts as 'in the gang': the base roster role + each
    configured rank role."""
    ids: set[int] = set()
    rid = store.get("roster_role_id")
    if rid:
        ids.add(rid)
    for r in store.get("ranks", []):
        if r.get("role_id"):
            ids.add(r["role_id"])
    return ids


# Fixed rank hierarchy for the website, highest first. Ranks not listed here
# show after these, ordered by their Discord position. Edit this list to reorder.
_RANK_ORDER = ["bot", "gov", "shodai", "leadership", "hierarchy", "honorary"]
# Roles never shown on the website - not as chips, rank groups, or add options.
_HIDDEN_ROLES = {".", "cee fuck", "ceefu"}


def _hidden_role(name: str) -> bool:
    return (name or "").strip().lower() in _HIDDEN_ROLES


def _rank_idx(name: str) -> int:
    """Index in the fixed hierarchy (0 = top); unlisted ranks sort after."""
    try:
        return _RANK_ORDER.index((name or "").strip().lower())
    except ValueError:
        return len(_RANK_ORDER)


def _manageable(guild, role) -> bool:
    """Can the bot add/remove this role? (its own top role must sit above it)"""
    me = getattr(guild, "me", None)
    return me is not None and role.position < me.top_role.position


def _top_rank_name(guild: "discord.Guild", store: dict, member) -> str | None:
    """The member's top rank by the fixed hierarchy (then Discord position),
    among configured ranks they hold, ignoring hidden roles."""
    member_role_ids = {r.id for r in getattr(member, "roles", [])}
    cands = []
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        name = r.get("name") or (role.name if role is not None else "")
        if (
            role is not None
            and role.id in member_role_ids
            and not _hidden_role(name)
            and not _hidden_role(role.name)
        ):
            cands.append((_rank_idx(name), -role.position, name))
    if not cands:
        return None
    cands.sort()
    return cands[0][2]


def _roster_members(guild: "discord.Guild", store: dict) -> list[dict]:
    """Everyone on the roster: holders of any gang role, plus anyone still on the
    stored roster list. Each gang role is returned as {id,name,manageable},
    ordered by the fixed hierarchy, with hidden roles dropped."""
    role_ids = _gang_role_ids(guild, store)
    found: dict[int, object] = {}

    for rid in role_ids:
        role = guild.get_role(rid)
        if role is not None:
            for m in role.members:
                found[m.id] = m

    for entry in store.get("roster", []):
        mid = entry.get("id")
        if mid and mid not in found:
            m = guild.get_member(mid)
            if m is not None:
                found[mid] = m

    out = []
    for m in found.values():
        gang = [
            r
            for r in getattr(m, "roles", [])
            if r.id in role_ids and not _hidden_role(r.name)
        ]
        gang.sort(key=lambda r: (_rank_idx(r.name), -r.position))
        roles = [
            {"id": str(r.id), "name": r.name, "manageable": _manageable(guild, r)}
            for r in gang
        ]
        out.append(
            {
                "id": str(m.id),
                "name": m.display_name,
                "username": getattr(m, "name", "") or "",
                "avatar": str(m.display_avatar.url),
                "rank": _top_rank_name(guild, store, m),
                "roles": roles,
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


def _rank_options(guild: "discord.Guild", store: dict) -> list[dict]:
    """Gang roles a member can be *given* from the web (base roster role + ranks),
    limited to ones the bot can manage, hidden roles dropped, ordered by hierarchy."""
    opts, seen = [], set()
    rid = store.get("roster_role_id")
    base = guild.get_role(rid) if rid else None
    if base is not None and _manageable(guild, base) and not _hidden_role(base.name):
        opts.append({"id": str(base.id), "name": base.name, "_i": _rank_idx(base.name)})
        seen.add(base.id)
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if (
            role is not None
            and role.id not in seen
            and _manageable(guild, role)
            and not _hidden_role(role.name)
            and not _hidden_role(r.get("name"))
        ):
            opts.append({"id": str(role.id), "name": r["name"], "_i": _rank_idx(r["name"])})
            seen.add(role.id)
    opts.sort(key=lambda o: (o["_i"], o["name"].lower()))
    return [{"id": o["id"], "name": o["name"]} for o in opts]


def _overview(guild: "discord.Guild", store: dict) -> dict:
    """Live counts for the dashboard strip (mirrors /gang dashboard)."""
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    ranked: set[int] = set()
    for r in store.get("ranks", []):
        role = guild.get_role(r.get("role_id"))
        if role is not None:
            ranked.update(m.id for m in role.members)
    eoi_timers = store.get("eoi_timers", {})
    open_eois = sum(
        1 for mid in store.get("eoi_apps", {}) if not eoi_timers.get(mid, {}).get("done")
    )
    return {
        "ranked": len(ranked),
        "kos": len(store.get("kos", [])),
        "open_eois": open_eois,
        "strikes_30d": sum(
            1 for s in store.get("strikes", []) if s.get("ts", 0) >= now - 30 * 86400
        ),
        "wars": sum(1 for w in store.get("wars", []) if w.get("active", True)),
        "radio": store.get("radio_freq") or "-",
        "members": guild.member_count or len(getattr(guild, "members", [])),
    }


def _kos_members(guild: "discord.Guild", store: dict) -> list[dict]:
    out = []
    for k in store.get("kos", []):
        uid = k.get("id")
        if not uid:
            continue
        m = guild.get_member(uid)
        out.append(
            {
                "id": str(uid),
                "name": m.display_name if m is not None else (k.get("name") or str(uid)),
                "avatar": str(m.display_avatar.url) if m is not None else "",
                # entries are stored with "note"; older ones used "reason"
                "reason": k.get("note") or k.get("reason") or "",
                "added_by": k.get("added_by") or "",
                "at": k.get("at") or "",
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return out


def _strikes_list(guild: "discord.Guild", store: dict) -> list[dict]:
    """Everyone who currently has strikes, worst first, with each strike's
    reason / who issued it / when. Read-only mirror of `/strike list`."""
    limit = int(store.get("strike_limit", 3) or 3)
    by_user: dict[int, list] = {}
    for s in store.get("strikes", []):
        uid = s.get("user_id")
        if not uid:
            continue
        by_user.setdefault(uid, []).append(s)
    out = []
    for uid, lst in by_user.items():
        m = guild.get_member(uid)
        lst = sorted(lst, key=lambda s: s.get("ts", 0), reverse=True)
        entries = []
        for s in lst:
            giver = guild.get_member(s.get("by")) if s.get("by") else None
            entries.append(
                {
                    "id": s.get("id"),
                    "reason": s.get("reason") or "",
                    "by": giver.display_name if giver is not None else (str(s.get("by")) if s.get("by") else ""),
                    "ts": s.get("ts"),
                }
            )
        out.append(
            {
                "id": str(uid),
                "name": m.display_name if m is not None else str(uid),
                "avatar": str(m.display_avatar.url) if m is not None else "",
                "count": len(lst),
                "limit": limit,
                "over": len(lst) >= limit,
                "strikes": entries,
            }
        )
    out.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return out


def _remove_strike(store: dict, strike_id: int) -> dict:
    """Remove ONE strike by its id (mirrors /strike remove)."""
    hit = next((s for s in store.get("strikes", []) if s.get("id") == strike_id), None)
    if hit is None:
        return {"ok": False, "error": f"No strike #{strike_id}."}
    store["strikes"].remove(hit)
    save_data(DATA)
    return {"ok": True, "user_id": hit.get("user_id")}


def _clear_strikes(store: dict, user_id: int) -> dict:
    """Clear ALL of a member's strikes (mirrors /strike clear)."""
    before = len(store.get("strikes", []))
    store["strikes"] = [s for s in store.get("strikes", []) if s.get("user_id") != user_id]
    save_data(DATA)
    return {"ok": True, "cleared": before - len(store["strikes"])}


async def _set_gang_role(guild, store, user_id: int, role_id: int, action: str, actor=None) -> dict:
    """Add or remove ONE gang role (roster/rank) on a member. Refuses anything
    that isn't a gang role, and anything above the bot."""
    if role_id not in _gang_role_ids(guild, store):
        return {"ok": False, "error": "That isn't a gang role."}
    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "error": "Role not found."}
    me = guild.me
    if me is not None and role.position >= me.top_role.position:
        return {"ok": False, "error": f"'{role.name}' sits above my role - I can't change it."}
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    if member is None:
        return {"ok": False, "error": "Member not found."}
    try:
        if action == "add":
            await member.add_roles(role, reason="Web roster: add role")
        else:
            await member.remove_roles(role, reason="Web roster: remove role")
    except discord.HTTPException:
        return {"ok": False, "error": "Discord refused the change - check my permissions."}
    save_data(DATA)
    await send_log(
        guild, store,
        f"🌐 Role **{role.name}** {action}ed for {member.mention} via the website.",
        category="web", actor=actor,
        fields=[
            ("Member", member.mention, True),
            ("Role", role.name, True),
            ("Action", "Added" if action == "add" else "Removed", True),
        ],
    )
    return {"ok": True, "action": action, "role": role.name}


async def _add_kos(guild, store, user_id: int, note: str | None = None, actor=None) -> dict:
    """Add a member to the KOS list from the website - mirrors `/kos add`:
    resolve the member, dedupe, give them the KOS role, log it, save."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    if member is None:
        return {"ok": False, "error": "That user isn't in this server - double-check the Discord ID."}
    if any(k.get("id") == member.id for k in store.get("kos", [])):
        return {"ok": False, "error": f"{member.display_name} is already on the KOS list."}
    rid = store.get("kos_role_id")
    role = guild.get_role(rid) if rid else None
    role_added = False
    if role is not None:
        try:
            await member.add_roles(role, reason="Web: add to KOS")
            role_added = True
        except discord.HTTPException:
            pass
    store.setdefault("kos", []).append({
        "id": member.id,
        "name": member.display_name,
        "note": note or None,
        "added_by": str(actor) if actor is not None else "website",
        "at": now_stamp(),
    })
    save_data(DATA)
    name = member.mention
    await send_log(
        guild, store, f"🌐 {name} added to KOS via the website.",
        category="web", actor=actor,
        fields=[
            ("Member", name, True),
            ("Reason", note or "-", True),
            ("KOS role given", "Yes" if role_added else ("No role set" if role is None else "No"), True),
            ("KOS size", str(len(store.get("kos", []))), True),
        ],
    )
    return {
        "ok": True, "name": member.display_name, "mention": name,
        "role_added": role_added, "role_missing": role is None,
        "members": _kos_members(guild, store),
    }


async def _remove_kos(guild, store, user_id: int, actor=None) -> dict:
    before = len(store.get("kos", []))
    store["kos"] = [k for k in store.get("kos", []) if k.get("id") != user_id]
    was_listed = len(store["kos"]) != before
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    rid = store.get("kos_role_id")
    role = guild.get_role(rid) if rid else None
    role_removed = False
    if member is not None and role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason="Web: remove from KOS")
            role_removed = True
        except discord.HTTPException:
            pass
    save_data(DATA)
    name = member.mention if member is not None else f"`{user_id}`"
    await send_log(
        guild, store, f"🌐 {name} taken off KOS via the website.",
        category="web", actor=actor,
        fields=[("Member", name, True), ("KOS role removed", "Yes" if role_removed else "No", True)],
    )
    return {
        "ok": True,
        "was_listed": was_listed,
        "role_removed": role_removed,
        "member": None if member is None else member.display_name,
    }


# ---- stash (gang inventory) ------------------------------------------------- #

# A matching emoji per item, picked from keywords in its name. Not the real game
# icon (that needs a trained model), just a quick visual.
_ICON_MAP = [
    (("pistol", "rifle", "smg", "shotgun", "weapon", " gun", "ak", "revolver",
      "knife", "machete", "bat", "sniper"), "🔫"),
    (("ammo", "round", "bullet", " mag", "clip", "shell"), "🧨"),
    (("money", "cash", "dollar", "dirty", "marked", "bank", "chip", "token"), "💵"),
    (("gold", "diamond", "ring", "rolex", "jewel", "necklace", "watch"), "💎"),
    (("weed", "coke", "cocaine", "meth", "crack", "drug", "baggie", "joint",
      "oxy", "pill", "lean", "ket", "acid", "lsd", "shroom"), "💊"),
    (("burger", "taco", "sandwich", "bread", "meat", "food", "donut", "pizza",
      "chips", "apple", "banana"), "🍔"),
    (("water", "soda", "cola", "beer", "juice", "drink", "coffee", "energy", "milk"), "🥤"),
    (("lockpick", "drill", "tool", "screwdriver", "wrench", "hammer", "thermite",
      "advanced", "kit"), "🛠️"),
    (("bandage", "medkit", "medical", "health", "firstaid", "first aid", "heal",
      "painkiller", "morphine"), "🩹"),
    (("phone", "tablet", "laptop", "radio", "gps"), "📱"),
    (("key", "fob"), "🔑"),
    (("cigarette", "cigar", "lighter", "rolling"), "🚬"),
    (("fish", "bait", "rod"), "🐟"),
]


def _item_icon(name: str) -> str:
    n = " " + (name or "").lower()
    for keys, emoji in _ICON_MAP:
        if any(k in n for k in keys):
            return emoji
    return "📦"


def _stash_items(store: dict) -> list[dict]:
    out = [
        {
            "item": s.get("item", ""),
            "qty": int(s.get("qty", 0)),
            "weight": round(float(s.get("weight", 0) or 0), 2),
            "icon": _item_icon(s.get("item", "")),
            "image": s.get("image", "") or "",
        }
        for s in store.get("stash", [])
        if s.get("item")
    ]
    out.sort(key=lambda x: x["item"].lower())
    return out


def _stash_upsert(
    store: dict, item: str, qty, mode: str = "set", weight=None, image=None
) -> dict:
    """Add/set an item in the stash. mode 'add' increments, 'set' replaces.
    Items that drop to 0 or below are removed. `weight` (per-unit kg) and `image`
    (a data: URI icon cropped from a screenshot) are optional; a kept image
    survives when more of the item is added."""
    item = (item or "").strip()
    if not item:
        return {"ok": False, "error": "Item name is required."}
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Quantity must be a whole number."}
    stash = store.setdefault("stash", [])
    hit = None
    for s in stash:
        if s.get("item", "").strip().lower() == item.lower():
            hit = s
            break
    if hit is None:
        hit = {"item": item, "qty": 0, "weight": 0, "image": ""}
        stash.append(hit)
    hit["qty"] = (int(hit.get("qty", 0)) + qty) if mode == "add" else qty
    if weight is not None and str(weight) != "":
        try:
            hit["weight"] = max(float(weight), 0)
        except (TypeError, ValueError):
            pass
    if image and isinstance(image, str) and image.startswith("data:"):
        hit["image"] = image[:200000]  # keep a cropped icon; cap size
    store["stash"] = [s for s in stash if int(s.get("qty", 0)) > 0]
    save_data(DATA)
    return {"ok": True, "item": item, "qty": max(int(hit["qty"]), 0)}


def _stash_remove(store: dict, item: str) -> dict:
    key = (item or "").strip().lower()
    before = len(store.get("stash", []))
    store["stash"] = [
        s for s in store.get("stash", []) if s.get("item", "").strip().lower() != key
    ]
    save_data(DATA)
    return {"ok": len(store["stash"]) != before}


_STASH_SKIP = {
    "inventory", "stash", "trunk", "weight", "items", "item", "qty", "quantity",
    "glovebox", "pocket", "pockets", "name", "amount", "total", "search", "slots",
    "kg", "lb", "capacity", "used", "free",
}
# Weight readouts to ignore: "40/50", "12.5 kg", "40 / 50 KG", etc.
_WEIGHT_RE = re.compile(r"\d+\s*/\s*\d+|\d+(?:[.,]\d+)?\s*(?:kg|kgs|lb|lbs|oz|g)\b", re.I)


def _parse_stash_text(text: str) -> list[dict]:
    """Best-effort: turn OCR'd lines into {item, qty, icon} candidates. Skips the
    weight readout. Imperfect - the page shows these to confirm, never auto-saved."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _WEIGHT_RE.search(line):  # drop weight bars / per-item weight
            continue
        qty = 1
        m = (
            re.search(r"x\s*(\d{1,6})\b", line, re.I)
            or re.search(r"\b(\d{1,6})\s*x\b", line, re.I)
            or re.search(r"\b(\d{1,6})\b", line)
        )
        if m:
            qty = int(m.group(1))
        name = re.sub(r"\b\d{1,6}\s*x\b|\bx\s*\d{1,6}\b|\b\d{1,6}\b", " ", line, flags=re.I)
        name = re.sub(r"[^A-Za-z0-9 \-'&]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if name.lower() in _STASH_SKIP:
            continue
        # drop OCR garbage: need at least 3 letters and a real word
        if len(re.sub(r"[^A-Za-z]", "", name)) < 3:
            continue
        if not any(len(w) >= 3 for w in name.split()):
            continue
        out.append({"item": name.title(), "qty": qty})
    # merge duplicates
    merged: dict[str, int] = {}
    for c in out:
        merged[c["item"]] = merged.get(c["item"], 0) + c["qty"]
    return [{"item": k, "qty": v, "icon": _item_icon(k)} for k, v in merged.items()]


def _ocr_available() -> bool:
    """True only if the Tesseract engine is actually callable."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_image(data: bytes) -> str:
    """OCR an uploaded screenshot. Returns '' if OCR tooling isn't available.
    Game inventories are usually light text on a dark background, which Tesseract
    reads badly, so dark images are inverted first."""
    try:
        import io as _io

        import pytesseract
        from PIL import Image, ImageOps

        img = Image.open(_io.BytesIO(data)).convert("RGB")
        g = ImageOps.grayscale(img)
        big = max(g.size) or 1
        if big < 2000:  # upscale small screenshots so small item text is legible
            scale = 2000 / big
            g = g.resize((max(int(g.width * scale), 1), max(int(g.height * scale), 1)))
        g = ImageOps.autocontrast(g)
        # invert when mostly dark (light-on-dark UI) -> dark-on-light for Tesseract
        hist = g.histogram()
        if sum(hist[:128]) > sum(hist[128:]):
            g = ImageOps.invert(g)
        best = ""
        for psm in (6, 11, 4):  # block, sparse, column - keep whichever reads most
            try:
                out = pytesseract.image_to_string(g, config=f"--psm {psm}")
            except Exception:
                continue
            if len(out.strip()) > len(best.strip()):
                best = out
        return best
    except Exception as exc:  # tesseract missing, bad image, etc.
        log.info("OCR unavailable/failed: %s", exc)
        return ""


def _trim_bg(img, tol: int = 96):
    """Fully knock the slot background out to transparent, then crop tight to the
    icon that's left.

    The background colour is estimated from the *whole border ring* (its median,
    so a stray bright pixel can't throw it off), and every pixel close to that
    colour anywhere in the image is made transparent - not just the ones touching
    an edge - so interior patches of background go too. Near-black slot shading is
    also dropped. What survives is cropped to its bounding box. Best-effort: on a
    busy or gradient background it won't be pixel-perfect."""
    from PIL import Image

    rgba = img.convert("RGBA")
    if max(rgba.size) > 160:
        rgba.thumbnail((160, 160), Image.LANCZOS)
    px = rgba.load()
    w, h = rgba.size
    if w < 2 or h < 2:
        return rgba

    # background colour = median of the 1px border ring
    ring = []
    for x in range(w):
        ring.append(px[x, 0]); ring.append(px[x, h - 1])
    for y in range(h):
        ring.append(px[0, y]); ring.append(px[w - 1, y])
    n = len(ring)
    br = sorted(c[0] for c in ring)[n // 2]
    bg = sorted(c[1] for c in ring)[n // 2]
    bb = sorted(c[2] for c in ring)[n // 2]

    minx, miny, maxx, maxy = w, h, 0, 0
    for y in range(h):
        for x in range(w):
            r, g, b, _ = px[x, y]
            near_bg = abs(r - br) + abs(g - bg) + abs(b - bb) < tol
            dark_slot = max(r, g, b) < 50 and (max(r, g, b) - min(r, g, b)) < 22
            if near_bg or dark_slot:
                px[x, y] = (r, g, b, 0)
            else:
                if x < minx: minx = x
                if y < miny: miny = y
                if x > maxx: maxx = x
                if y > maxy: maxy = y
    if maxx <= minx or maxy <= miny:
        return rgba  # all background - leave as-is
    return rgba.crop((minx, miny, maxx + 1, maxy + 1))


def _scan_stash(data: bytes) -> list[dict]:
    """OCR an uploaded screenshot into {item, qty, icon, image} candidates.

    Words are clustered by position (not Tesseract's line grouping) so a grid
    inventory doesn't merge items across columns, and each item's icon is cropped
    from the slot with its background cut out. Best-effort - still imperfect, so
    the page shows candidates to confirm. Returns [] if OCR is unavailable."""
    try:
        import base64 as _b64
        import io as _io

        import pytesseract
        from PIL import Image, ImageOps

        orig = Image.open(_io.BytesIO(data)).convert("RGB")
        g = ImageOps.grayscale(orig)
        big = max(g.size) or 1
        scale = 2000 / big if big < 2000 else 1.0
        proc = (
            g.resize((max(int(g.width * scale), 1), max(int(g.height * scale), 1)))
            if scale != 1.0
            else g
        )
        proc = ImageOps.autocontrast(proc)
        hist = proc.histogram()
        if sum(hist[:128]) > sum(hist[128:]):
            proc = ImageOps.invert(proc)
        dd = pytesseract.image_to_data(
            proc, config="--psm 11", output_type=pytesseract.Output.DICT
        )
        words = []
        for i in range(len(dd["text"])):
            t = (dd["text"][i] or "").strip()
            if not t:
                continue
            try:
                conf = float(dd["conf"][i])
            except (TypeError, ValueError):
                conf = 0
            if conf < 35:  # drop low-confidence noise
                continue
            words.append(
                {"t": t, "x": dd["left"][i], "y": dd["top"][i],
                 "w": dd["width"][i], "h": dd["height"][i]}
            )
        if not words:
            return []
        heights = sorted(w["h"] for w in words)
        mh = heights[len(heights) // 2] or 12

        # union-find: connect words that sit next to each other (same slot/label)
        parent = list(range(len(words)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i in range(len(words)):
            for j in range(i + 1, len(words)):
                A, B = words[i], words[j]
                gy = max(0, max(A["y"], B["y"]) - min(A["y"] + A["h"], B["y"] + B["h"]))
                gx = max(0, max(A["x"], B["x"]) - min(A["x"] + A["w"], B["x"] + B["w"]))
                if gy < 0.6 * mh and gx < 1.3 * mh:  # close vertically AND horizontally
                    parent[find(i)] = find(j)

        clusters: dict = {}
        for i in range(len(words)):
            clusters.setdefault(find(i), []).append(words[i])

        merged: dict = {}
        for ws in clusters.values():
            ws.sort(key=lambda w: (w["y"], w["x"]))
            line = " ".join(w["t"] for w in ws).strip()
            parsed = _parse_stash_text(line)
            if not parsed:
                continue
            cand = parsed[0]
            x0 = min(w["x"] for w in ws) / scale
            y0 = min(w["y"] for w in ws) / scale
            x1 = max(w["x"] + w["w"] for w in ws) / scale
            y1 = max(w["y"] + w["h"] for w in ws) / scale
            # The item image sits in the slot ABOVE its label, so crop the square
            # directly above the text cluster (never the text itself) and cut the
            # background out of it. Falls back to a slot-sized square if the icon
            # zone would fall off the top edge.
            cx = (x0 + x1) / 2
            side = max(x1 - x0, 24.0)
            gap = side * 0.10
            iy1 = y0 - gap            # bottom of icon = just above the label
            iy0 = iy1 - side          # one slot-height up
            if iy0 < 0:               # label near the top - fall back around it
                cyf = (y0 + y1) / 2
                iy0, iy1 = max(cyf - side / 2, 0), cyf + side / 2
            bx0 = max(cx - side / 2, 0)
            bx1 = min(cx + side / 2, orig.width)
            by0 = max(iy0, 0)
            by1 = min(iy1, orig.height)
            img_uri = ""
            if bx1 - bx0 > 8 and by1 - by0 > 8:
                try:
                    crop = _trim_bg(orig.crop((int(bx0), int(by0), int(bx1), int(by1))))
                    crop = crop.resize((64, 64), Image.LANCZOS)
                    buf = _io.BytesIO()
                    crop.save(buf, "PNG")
                    img_uri = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
                except Exception:
                    pass
            slot = merged.setdefault(cand["item"], {"qty": 0, "image": ""})
            slot["qty"] += cand["qty"]
            if not slot["image"] and img_uri:
                slot["image"] = img_uri
        return [
            {"item": k, "qty": v["qty"], "icon": _item_icon(k), "image": v["image"]}
            for k, v in merged.items()
        ]
    except Exception as exc:
        log.info("stash scan failed: %s", exc)
        return []


# ---- wars (read-only) ------------------------------------------------------- #

def _wars_list(store: dict) -> list[dict]:
    out = []
    for w in store.get("wars", []):
        log = w.get("log", []) or []
        wins, losses = int(w.get("wins", 0)), int(w.get("losses", 0))
        total = wins + losses
        last = log[-1] if log else None
        out.append(
            {
                "enemy": w.get("enemy", "?"),
                "wins": wins,
                "losses": losses,
                "active": bool(w.get("active", True)),
                "first_to": w.get("first_to"),
                "start": w.get("start"),
                "rounds": total,
                "winrate": round(100 * wins / total) if total else 0,
                # last ~14 rounds, oldest→newest, 1=win 0=loss, for a 🟩🟥 strip
                "recent": [1 if e.get("r") == "win" else 0 for e in log[-14:]],
                "last_ts": last.get("ts") if last else None,
                "last_by": (last.get("by_name") or "") if last else "",
            }
        )
    out.sort(key=lambda x: (not x["active"], -(x["wins"] + x["losses"])))
    return out


# ---- EOIs (read-only, pending only) ----------------------------------------- #

def _eois_list(guild: "discord.Guild", store: dict) -> list[dict]:
    """Applications still open for voting (ended ones are skipped), soonest to
    close first."""
    apps = store.get("eoi_apps", {})
    timers = store.get("eoi_timers", {})
    votes = store.get("eoi_votes", {})
    out = []
    for mid, app in apps.items():
        t = timers.get(str(mid), {})
        if t.get("done"):  # voting has ended - hide it
            continue
        v = votes.get(str(mid), {})
        uid = app.get("user_id")
        m = guild.get_member(uid) if uid else None
        out.append(
            {
                "id": str(mid),
                "ign": app.get("ign", ""),
                "role": app.get("role", ""),
                "applicant": m.display_name if m is not None else (app.get("ign") or str(uid)),
                "avatar": str(m.display_avatar.url) if m is not None else "",
                "yes": sum(1 for x in v.values() if x == "yes"),
                "no": sum(1 for x in v.values() if x == "no"),
                "ends": t.get("ends"),
            }
        )
    out.sort(key=lambda x: (x.get("ends") is None, x.get("ends") or 0))
    return out


_ORDER_PRIO = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _orders_list(guild: "discord.Guild", store: dict) -> list[dict]:
    """All ticket orders for the website - open first, then by priority, newest
    first - with the customer, items/guns, quantity, paid state and channel."""
    emoji_by_type = {t["name"]: (t.get("emoji") or "🎫") for t in store.get("ticket_types", [])}
    out = []
    for o in store.get("orders", []):
        uid = o.get("user_id")
        m = guild.get_member(uid) if uid else None
        out.append(
            {
                "id": o.get("id"),
                "type": o.get("type") or "Order",
                "emoji": emoji_by_type.get(o.get("type"), "🎫"),
                "customer": m.display_name if m is not None else (f"User {uid}" if uid else "-"),
                "username": (getattr(m, "name", "") or "") if m is not None else "",
                "avatar": str(m.display_avatar.url) if m is not None else "",
                "guns": o.get("guns") or "",
                "quantity": o.get("quantity") or "",
                "notes": o.get("notes") or "",
                "paid": bool(o.get("paid")),
                "status": o.get("status", "open"),
                "priority": o.get("priority", "normal"),
                "created_ts": o.get("created_ts"),
                "channel_id": str(o.get("channel_id") or ""),
            }
        )
    out.sort(key=lambda x: (x["status"] != "open", _ORDER_PRIO.get(x["priority"], 2), -(x["created_ts"] or 0)))
    return out


def _ticket_types(store: dict) -> list[dict]:
    return [{"name": t["name"], "emoji": t.get("emoji") or "🎫"} for t in store.get("ticket_types", [])]


async def _remove_from_roster(
    guild: "discord.Guild", store: dict, user_id: int, actor: str = "website", actor_member=None
) -> dict:
    """Drop the member from the stored roster AND strip every gang role from them
    in Discord. Mirrors what /roster remove does, but also clears rank roles so
    they actually disappear from the live roster."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None

    before = len(store.get("roster", []))
    store["roster"] = [e for e in store.get("roster", []) if e.get("id") != user_id]
    was_listed = len(store["roster"]) != before

    removed_roles: list[str] = []
    failed_roles: list[str] = []
    if member is not None:
        role_ids = _gang_role_ids(guild, store)
        to_remove = [r for r in member.roles if r.id in role_ids]
        me = guild.me
        for role in to_remove:
            # skip roles we can't manage rather than erroring the whole request
            if me is not None and role.position >= me.top_role.position:
                failed_roles.append(role.name)
                continue
            try:
                await member.remove_roles(role, reason=f"Roster remove via website ({actor})")
                removed_roles.append(role.name)
            except discord.HTTPException:
                failed_roles.append(role.name)

    save_data(DATA)

    name = member.mention if member is not None else f"`{user_id}`"
    await send_log(
        guild,
        store,
        f"🌐 {name} removed from the roster via the website.",
        category="web", actor=actor_member,
        fields=[
            ("Member", name, True),
            ("Roles removed", ", ".join(removed_roles) or "-", False),
            ("Couldn't remove", ", ".join(failed_roles) or "-", True),
        ],
    )
    return {
        "ok": True,
        "was_listed": was_listed,
        "removed_roles": removed_roles,
        "failed_roles": failed_roles,
        "member": None if member is None else member.display_name,
    }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

# ---- sessions (short-lived signed cookie saying who you are) ---------------- #

_COOKIE = "tenshi_sess"
_STATE_COOKIE = "tenshi_state"


def _secret() -> bytes:
    raw = (
        os.getenv("WEB_SECRET")
        or os.getenv("DISCORD_CLIENT_SECRET")
        or os.getenv("DISCORD_TOKEN")
        or "tenshi-dev-secret"
    )
    return hashlib.sha256(raw.encode()).digest()


def _sign_session(uid: int | str, ttl: int = 43200) -> str:
    payload = {"uid": str(uid), "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _read_session(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    good = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload.get("uid")


# ---- per-IP abuse limiting (in-memory sliding window) ---------------------- #
_RL: dict[str, list[float]] = {}
_RL_SWEEP = [0.0]


def _client_ip(request) -> str:
    """Real client IP behind Railway's proxy (first X-Forwarded-For hop)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.remote or "?"


def _rl_ok(key: str, limit: int, window: float = 60.0) -> bool:
    """True while `key` stays under `limit` hits per `window` seconds."""
    now = time.time()
    cutoff = now - window
    arr = [t for t in _RL.get(key, ()) if t >= cutoff]
    if len(arr) >= limit:
        _RL[key] = arr
        return False
    arr.append(now)
    _RL[key] = arr
    if now - _RL_SWEEP[0] > 300:                       # periodic cleanup of idle keys
        _RL_SWEEP[0] = now
        for k in [k for k, v in list(_RL.items()) if not v or v[-1] < now - 3600]:
            _RL.pop(k, None)
    return True


# Content-Security-Policy served on every page. 'unsafe-inline' stays in
# script-src only because the panels use inline on* handlers; every other
# directive is locked down (see the _apply() comment for what this buys).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: https: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "frame-src 'none'"
)


# ---- admin 2FA: one-time code DMed by the GM Support bot ------------------- #
_MFA_COOKIE = "tenshi_mfa"
_MFA: dict[str, dict] = {}   # uid -> {"code","exp","tries","sent"}


def _mfa_on() -> bool:
    """2FA is active only when explicitly turned on AND fully configured, so an
    unset or half-set config can never lock the owner out of the panel."""
    if (os.getenv("ADMIN_MFA") or "").strip().lower() not in ("on", "1", "true", "yes"):
        return False
    return bool((os.getenv("STAFF_BOT_URL") or "").strip() and (os.getenv("MFA_SECRET") or "").strip())


def _sign_mfa(uid, ttl: int = 43200) -> str:
    payload = {"uid": str(uid), "k": "mfa", "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _read_mfa(cookie: str | None) -> str | None:
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if payload.get("k") != "mfa" or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload.get("uid")


def _mfa_ok(request, uid) -> bool:
    return (not _mfa_on()) or _read_mfa(request.cookies.get(_MFA_COOKIE)) == str(uid)


def _mfa_friendly(err: str) -> str:
    return {
        "dms_closed": "Your Discord DMs are closed - turn on DMs from members of a server you share with GM Support, then resend.",
        "user not found": "GM Support couldn't find your Discord account.",
    }.get(err, "Couldn't send the code - GM Support didn't respond. Try again in a moment.")


def _support_invite() -> str:
    """The GM Support server invite shown on the 2FA screen, so an admin whose
    DMs can't be reached can join the server and switch DMs on. Only a genuine
    Discord invite link is accepted - anything else is ignored."""
    url = (os.getenv("SUPPORT_INVITE_URL") or os.getenv("GM_SUPPORT_INVITE") or "").strip()
    if not url:
        return ""
    low = url.lower()
    if low.startswith("discord.gg/") or low.startswith("discord.com/invite/"):
        url, low = "https://" + url, "https://" + low
    if low.startswith(("https://discord.gg/", "https://discord.com/invite/",
                       "https://discord.com/servers/")):
        return url
    return ""


_SUPPORT_INVITE_CACHE: dict = {"url": "", "exp": 0.0}


async def _support_invite_resolved() -> str:
    """The invite for the 2FA 'Join GM Support' button. A configured
    SUPPORT_INVITE_URL always wins; otherwise GM Support mints its own permanent
    invite and we cache it, so nothing has to be set up by hand. Returns '' if
    neither is available (the screen then shows the steps without a button)."""
    env = _support_invite()
    if env:
        return env
    now = time.time()
    c = _SUPPORT_INVITE_CACHE
    if c["url"] and now < c["exp"]:
        return c["url"]
    base = (os.getenv("STAFF_BOT_URL") or "").strip().rstrip("/")
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    inv = ""
    if base and secret and _aiohttp is not None:
        try:
            async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=6)) as s:
                async with s.post(base + "/internal/invite",
                                  headers={"X-Guard-Token": secret}) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        inv = str((data or {}).get("invite") or "")
        except Exception as exc:  # noqa: BLE001
            log.info("support invite fetch skipped: %s", exc)
    # Cache a hit for an hour; cache a miss briefly so we retry soon, not never.
    c.update(url=inv, exp=now + (3600 if inv else 120))
    return inv


async def _mfa_send_code(uid) -> tuple[bool, str]:
    """Generate (or reuse) a code and ask the GM Support bot to DM it. (ok, error)."""
    import secrets
    now = int(time.time())
    cur = _MFA.get(str(uid))
    code = cur["code"] if (cur and now - cur.get("sent", 0) < 20) else f"{secrets.randbelow(10**6):06d}"
    _MFA[str(uid)] = {"code": code, "exp": now + 300, "tries": 0, "sent": now}
    if _aiohttp is None:
        return False, "http unavailable"
    url = (os.getenv("STAFF_BOT_URL") or "").strip().rstrip("/") + "/internal/send-code"
    secret = (os.getenv("MFA_SECRET") or "").strip()
    uid_s = str(uid)
    ts = str(int(time.time()))
    # Bind timestamp + target + code with the shared secret so a captured request
    # can't be replayed (GM Support rejects a stale ts or a bad signature).
    sig = hmac.new(secret.encode(), f"{ts}.{uid_s}.{code}".encode(), hashlib.sha256).hexdigest()
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(url, json={"user_id": uid_s, "code": code,
                                         "service": "the Gang Manager admin panel"},
                              headers={"X-Guard-Token": secret,
                                       "X-Guard-TS": ts, "X-Guard-Sig": sig}) as r:
                try:
                    data = await r.json()
                except Exception:  # noqa: BLE001
                    data = {}
                if r.status == 200 and data.get("ok"):
                    return True, ""
                return False, str(data.get("error") or f"status {r.status}")
    except Exception as exc:  # noqa: BLE001
        log.warning("MFA send to GM Support failed: %s", exc)
        return False, "unreachable"


def _mfa_verify(uid, code: str) -> tuple[bool, str]:
    rec = _MFA.get(str(uid))
    if not rec:
        return False, "No code pending - send a new one."
    if int(time.time()) > rec.get("exp", 0):
        _MFA.pop(str(uid), None)
        return False, "That code expired - send a new one."
    if rec.get("tries", 0) >= 5:
        _MFA.pop(str(uid), None)
        return False, "Too many attempts - send a new code."
    if (code or "").strip() != rec["code"]:
        rec["tries"] = rec.get("tries", 0) + 1
        return False, "That code isn't right."
    _MFA.pop(str(uid), None)
    return True, ""


# ---- audit log + owner security alerts ------------------------------------- #

def _ua_label(ua: str) -> str:
    """Compact 'Browser · OS' from a User-Agent string, for admin audit logs."""
    low = (ua or "").lower()
    os_name = ("Windows" if "windows" in low else
               "macOS" if ("macintosh" in low or "mac os" in low) else
               "iPhone" if "iphone" in low else "iPad" if "ipad" in low else
               "Android" if "android" in low else "Linux" if "linux" in low else "")
    br = ("Edge" if "edg/" in low else
          "Chrome" if ("chrome" in low and "edg/" not in low) else
          "Firefox" if "firefox" in low else
          "Safari" if ("safari" in low and "chrome" not in low) else "")
    parts = [p for p in (br, os_name) if p]
    return " · ".join(parts) if parts else ((ua or "")[:60] or "-")


def _audit(request, actor, action: str, target: str = "", detail: str = "") -> None:
    """Record one privileged admin-site action to the durable audit log.
    Best-effort: a logging failure must never break the action itself."""
    try:
        audit_log_add({
            "actor": str(actor),
            "actor_name": _user_label(actor) or "",
            "action": str(action),
            "target": str(target or ""),
            "detail": str(detail or "")[:300],
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("audit write failed (%s): %s", action, exc)
    # Mirror it into its own Discord log channel via the GM Support staff bot.
    try:
        kind, title = _audit_kind(action)
        # Lead with the full profile of the person, then the action context. Admin
        # sign-ins post now too - that's exactly the "who signed in" the log is for.
        ctx = [{"name": "⚙️ Action", "value": f"`{action}`", "inline": True}]
        if target and str(target) != str(actor):
            ctx.append({"name": "🎯 Target", "value": f"`{target}`", "inline": True})
        if detail:
            ctx.append({"name": "📝 Detail", "value": str(detail)[:1024], "inline": False})
        af = _actor_fields(request, actor)
        fields = af[:1] + ctx + af[1:]  # Account, Action, Target, Detail, IP, Device, Country, …
        _fire(_gmlog(kind, title, fields=fields))
    except Exception as exc:  # noqa: BLE001
        log.info("gmlog audit forward skipped: %s", exc)


async def _alert_owners(text: str) -> None:
    """DM every bot operator a short security alert via the main bot. Best-effort."""
    try:
        emb = discord.Embed(description=text[:3500], colour=0xB33A3A)
        emb.set_author(name="Gang Manager · security")
    except Exception:  # noqa: BLE001
        emb = None
    for oid in owner_ids():
        try:
            u = bot.get_user(int(oid)) or await bot.fetch_user(int(oid))
            await (u.send(embed=emb) if emb is not None else u.send(text[:1900]))
        except Exception as exc:  # noqa: BLE001
            log.info("owner alert to %s skipped: %s", oid, exc)


def _fire(coro) -> None:
    """Schedule a fire-and-forget coroutine from inside a request handler."""
    try:
        import asyncio
        asyncio.create_task(coro)
    except Exception as exc:  # noqa: BLE001
        log.info("background alert skipped: %s", exc)
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass


def _client_ips(request):
    """(client, via): the real client IP (first forwarded hop) and the edge/proxy
    hop it reached us through (socket peer, else the last forwarded hop)."""
    client = ""
    try:
        client = _client_ip(request)
    except Exception:  # noqa: BLE001
        client = ""
    via = ""
    try:
        peer = request.transport.get_extra_info("peername") if getattr(request, "transport", None) else None
        if peer:
            via = str(peer[0])
    except Exception:  # noqa: BLE001
        via = ""
    if not via:
        hops = [h.strip() for h in (request.headers.get("X-Forwarded-For", "") or "").split(",") if h.strip()]
        if len(hops) > 1:
            via = hops[-1]
    return client, via


def _actor_fields(request, actor):
    """Everything about the PERSON behind an admin action - account, both IPs,
    device, browser + OS, language, country (when the edge gives one) and the page
    they acted from. No bot or system noise. Account field first."""
    name = _user_label(actor) or str(actor)
    who = (f"{name}\n<@{actor}> · `{actor}`" if str(actor).isdigit()
           else f"{name} (`{actor}`)")
    fields = [{"name": "👤 Account", "value": who[:1024], "inline": True}]
    if request is None:
        return fields
    client, via = _client_ips(request)
    if client or via:
        ipval = f"Client: `{client or '-'}`"
        if via and via != client:
            ipval += f"\nVia (edge): `{via}`"
        fields.append({"name": "🌐 IP", "value": ipval, "inline": True})
    ua = request.headers.get("User-Agent", "") or ""
    if ua:
        fields.append({"name": "🖥️ Device", "value": _ua_label(ua), "inline": True})
    country = (request.headers.get("CF-IPCountry") or request.headers.get("X-Vercel-IP-Country")
               or request.headers.get("X-Country-Code") or "").strip()
    if country and country.lower() not in ("xx", "t1"):
        fields.append({"name": "🌍 Country", "value": country.upper()[:8], "inline": True})
    lang = (request.headers.get("Accept-Language") or "").split(",")[0].strip()
    if lang:
        fields.append({"name": "🗣️ Language", "value": lang[:32], "inline": True})
    ref = (request.headers.get("Referer") or "").strip()
    if ref:
        try:
            from urllib.parse import urlparse
            ref = urlparse(ref).path or ref
        except Exception:  # noqa: BLE001
            pass
        fields.append({"name": "📄 From page", "value": f"`{ref[:120]}`", "inline": True})
    if ua:
        fields.append({"name": "🔎 Full user-agent", "value": f"`{ua[:280]}`", "inline": False})
    return fields


def _gmlog_on() -> bool:
    """Event forwarding is live once the staff bot URL + shared secret are set
    (independent of whether admin 2FA is switched on)."""
    return bool((os.getenv("STAFF_BOT_URL") or "").strip() and (os.getenv("MFA_SECRET") or "").strip())


async def _gmlog(kind: str, title: str, description: str = "", fields=None, color=None) -> None:
    """Forward one event to the GM Support staff bot, which posts it in the
    channel for this kind. Best-effort - never blocks or breaks the caller."""
    if not _gmlog_on() or _aiohttp is None:
        return
    base = (os.getenv("STAFF_BOT_URL") or "").strip().rstrip("/")
    secret = (os.getenv("MFA_SECRET") or "").strip()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{kind}".encode(), hashlib.sha256).hexdigest()
    payload: dict = {"kind": kind, "title": str(title)[:256], "description": str(description or "")[:4000]}
    if fields:
        payload["fields"] = list(fields)[:23]  # leave room for the staff bot's auto "When"
    if color is not None:
        payload["color"] = color
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.post(base + "/internal/log", json=payload,
                              headers={"X-Guard-Token": secret, "X-Guard-TS": ts, "X-Guard-Sig": sig}) as r:
                if r.status != 200:
                    log.info("gmlog %s -> HTTP %s", kind, r.status)
    except Exception as exc:  # noqa: BLE001
        log.info("gmlog %s failed: %s", kind, exc)


async def _staff_gmlogs_call():
    """Ask the GM Support staff bot to build its admin-log channels (one per kind)
    in its own server. Returns the staff bot's JSON, or None if unreachable."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(base + "/internal/gmlogs_setup", json={},
                              headers={"X-Guard-Token": secret}) as r:
                return await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None


async def _staff_invoice_call(user_id, invoice_id, server, amount, currency, is_renewal):
    """Ask the GM Support bot to DM a buyer their invoice. Returns the staff bot's
    JSON ({"dm": "sent"|"blocked"}) or None if it's unreachable."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    payload = {"user_id": str(user_id), "invoice_id": str(invoice_id), "server": str(server or ""),
               "amount": int(amount or 0), "currency": str(currency or ""), "is_renewal": bool(is_renewal)}
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=12)) as s:
            async with s.post(base + "/internal/invoice", json=payload,
                              headers={"X-Guard-Token": secret}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                return {"ok": False}
    except Exception:  # noqa: BLE001
        return None


async def _staff_member_call(user_id):
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    try:
        async with _aiohttp.ClientSession(timeout=_aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(base + "/internal/member", params={"user": str(user_id)},
                             headers={"X-Guard-Token": secret}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None
    return None


async def _in_support(user_id):
    """True / False if the user is in the GM Support server, or None if the support
    bot can't be reached to check (caller decides how to handle the unknown)."""
    res = await _staff_member_call(user_id)
    if isinstance(res, dict) and res.get("ok"):
        return bool(res.get("member"))
    return None


async def _dm_invoice(purchaser_id, invoice_id: str, guild_name: str,
                      amount: int, currency: str, is_renewal: bool = False) -> bool:
    """DM the buyer their invoice ID the moment a payment lands. Sent by the GM
    Support bot (it shares the support server with users, so DMs reach them), with
    the main bot as a fallback. Returns True if the DM was delivered."""
    pid = str(purchaser_id or "")
    if not pid.isdigit() or not invoice_id:
        return False
    # 1) Support bot first - most buyers share its server, so the DM lands.
    try:
        res = await _staff_invoice_call(pid, invoice_id, guild_name, amount, currency, is_renewal)
        if isinstance(res, dict) and res.get("dm") == "sent":
            return True
    except Exception as exc:  # noqa: BLE001
        log.info("staff invoice DM skipped: %s", exc)
    # 2) Fallback to the main bot (works if it shares a server with the buyer).
    try:
        u = bot.get_user(int(pid)) or await bot.fetch_user(int(pid))
        if u is None:
            return False
        emb = discord.Embed(
            title="✅ Renewal confirmed" if is_renewal else "✅ Purchase confirmed",
            description=("Thanks! Here's your receipt. **Keep your invoice ID** - quote it when "
                         "you open a support ticket and staff can pull up your order instantly."),
            colour=0x2f6bff,
            timestamp=_dt.datetime.now(_dt.timezone.utc),
        )
        emb.add_field(name="🧾 Invoice ID", value=f"```{str(invoice_id)[:90]}```", inline=False)
        if guild_name:
            emb.add_field(name="Server", value=str(guild_name)[:100], inline=True)
        amt_txt = (f"{amount/100:.2f} {currency}".strip()) if amount else ""
        if amt_txt:
            emb.add_field(name="Amount", value=amt_txt, inline=True)
        emb.set_footer(text="Gang Manager · keep this for your records")
        try:
            base = (web_base_url() or "").rstrip("/")
            if base:
                emb.set_thumbnail(url=f"{base}/gm-icon.png")
        except Exception:  # noqa: BLE001
            pass
        await u.send(embed=emb)
        return True
    except discord.Forbidden:
        log.info("invoice DM blocked (DMs closed) for %s", purchaser_id)
        return False
    except Exception as exc:  # noqa: BLE001
        log.info("invoice DM failed for %s: %s", purchaser_id, exc)
        return False


# admin audit action -> (log channel kind, human title for the Discord embed)
def _audit_kind(action: str) -> "tuple[str, str]":
    a = action or ""
    if a == "admin_signin":     return "signin", "🔑 Admin sign-in"
    if a == "mfa_lockout":      return "security", "🚨 Sign-in code lockout"
    if a == "ban":              return "moderation", "🔨 User banned"
    if a == "unban":            return "moderation", "♻️ User unbanned"
    if a == "staff_add":        return "staff", "🛡️ Staff authorised"
    if a == "staff_remove":     return "staff", "🚫 Staff access removed"
    if a.startswith("sub_"):    return "subscription", "💳 Subscription " + a[4:].replace("_", " ")
    if a == "bot_leave_server": return "server", "👋 Bot removed from a server"
    if a.startswith("pool_"):   return "pool", "🤖 Pool · " + a[5:].replace("_", " ")
    return "security", "📋 " + a


# ---- Discord OAuth --------------------------------------------------------- #
# identify + guilds: who you are, and which servers you're in (so the panel can
# offer "add the bot here" / "ask the owner" instead of a dead end).

_OAUTH_AUTHORIZE = "https://discord.com/oauth2/authorize"
_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
_OAUTH_ME = "https://discord.com/api/users/@me"
_OAUTH_MY_GUILDS = "https://discord.com/api/users/@me/guilds"
_OAUTH_SCOPES = "identify guilds"
_STATE_TTL = 900  # seconds a login link stays valid

# uid -> {"at": ts, "user": {...}, "guilds": [...]} - filled at login, in memory
_USER_CACHE: dict[str, dict] = {}


def _configured() -> bool:
    return bool(os.getenv("DISCORD_CLIENT_SECRET"))


def _client_id() -> str:
    # The OAuth client id is the *application* id (equal to the bot's user id
    # for modern apps, but not for some older ones).
    app_id = getattr(bot, "application_id", None)
    if app_id:
        return str(app_id)
    return str(getattr(getattr(bot, "user", None), "id", "") or "")


def _request_base(request) -> str:
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"https://{host}"


def _canonical_base(request) -> str:
    """One stable origin for login so the redirect URI always matches the one
    registered in the Developer Portal and cookies stay on one host. Uses
    PUBLIC_URL / WEB_URL when set, otherwise the host the visitor is on."""
    explicit = (os.getenv("PUBLIC_URL") or os.getenv("WEB_URL") or "").strip().rstrip("/")
    return explicit or _request_base(request)


def _redirect_uri(request) -> str:
    return f"{_canonical_base(request)}/callback"


def _make_state() -> str:
    """Signed, self-verifying login state. Valid for _STATE_TTL seconds even if
    the browser that finishes the login isn't the one that started it (e.g. the
    Discord mobile app hands the redirect to a different browser)."""
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    sig = hmac.new(_secret(), f"st.{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{nonce}.{sig}"


def _state_ok(state: str | None) -> bool:
    """Accept any state this server signed in the last _STATE_TTL seconds. We
    deliberately don't require the state cookie: when Discord's mobile app (or
    another tab) finishes the login, that cookie isn't there and the old check
    failed with 'Login expired'."""
    if not state or state.count(".") != 2:
        return False
    ts, nonce, sig = state.split(".")
    good = hmac.new(_secret(), f"st.{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return False
    try:
        age = int(time.time()) - int(ts)
    except ValueError:
        return False
    return 0 <= age <= _STATE_TTL


def _authorize_url(request, state: str) -> str:
    from urllib.parse import urlencode

    q = urlencode(
        {
            "client_id": _client_id(),
            "response_type": "code",
            "scope": _OAUTH_SCOPES,
            "redirect_uri": _redirect_uri(request),
            "state": state,
            "prompt": "none",
        }
    )
    return f"{_OAUTH_AUTHORIZE}?{q}"


async def _oauth_exchange(code: str, redirect_uri: str) -> dict | None:
    """Swap the login code for a token. Logs Discord's reason on failure so a
    bad secret / redirect mismatch shows up clearly in the Railway logs."""
    secret = os.getenv("DISCORD_CLIENT_SECRET")
    if not secret or _aiohttp is None:
        return None
    data = {
        "client_id": _client_id(),
        "client_secret": secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.post(
                _OAUTH_TOKEN,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as r:
                body = await r.json(content_type=None)
                if r.status != 200:
                    log.warning(
                        "OAuth token exchange failed (%s): %s - check DISCORD_CLIENT_SECRET and that "
                        "%s is listed under OAuth2 → Redirects", r.status, body, redirect_uri,
                    )
                    return None
                return body
    except Exception as exc:  # network hiccup
        log.warning("OAuth token exchange error: %s", exc)
        return None


async def _oauth_get(url: str, token: str):
    if _aiohttp is None:
        return None
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.get(url, headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception as exc:
        log.warning("OAuth GET %s failed: %s", url, exc)
        return None


async def _oauth_user(token: str) -> dict | None:
    return await _oauth_get(_OAUTH_ME, token)


async def _oauth_guilds(token: str) -> list | None:
    data = await _oauth_get(_OAUTH_MY_GUILDS, token)
    return data if isinstance(data, list) else None


def _avatar_url(user: dict) -> str:
    uid, av = user.get("id"), user.get("avatar")
    if uid and av:
        ext = "gif" if str(av).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{uid}/{av}.{ext}?size=64"
    return "https://cdn.discordapp.com/embed/avatars/0.png"


def _remember_user(uid: str, user: dict, guilds: list | None) -> None:
    slim = []
    for g in guilds or []:
        try:
            gid = str(g["id"])
        except (KeyError, TypeError):
            continue
        icon = g.get("icon")
        slim.append(
            {
                "id": gid,
                "name": g.get("name") or "Server",
                "icon": f"https://cdn.discordapp.com/icons/{gid}/{icon}.png?size=64" if icon else "",
                "owner": bool(g.get("owner")),
                "permissions": str(g.get("permissions") or "0"),
            }
        )
    _USER_CACHE[str(uid)] = {
        "at": int(time.time()),
        "user": {
            "id": str(uid),
            "name": user.get("global_name") or user.get("username") or "",
            "avatar": _avatar_url(user),
        },
        "guilds": slim if guilds is not None else None,
    }


def _cached_profile(uid: str) -> dict:
    entry = _USER_CACHE.get(str(uid))
    if entry:
        return entry["user"]
    u = get_user_any(int(uid)) if str(uid).isdigit() else None
    if u is not None:
        return {"id": str(uid), "name": getattr(u, "global_name", None) or u.name,
                "avatar": str(u.display_avatar.url)}
    return {"id": str(uid), "name": "", "avatar": "https://cdn.discordapp.com/embed/avatars/0.png"}


_PERM_ADMIN = 0x8
_PERM_MANAGE_GUILD = 0x20


def _server_picker(uid: str, accessible_ids: set) -> list | None:
    """The user's servers that matter here: ones running the bot, and ones they
    can add it to. None when we don't have their server list (old session)."""
    entry = _USER_CACHE.get(str(uid))
    if not entry or entry.get("guilds") is None:
        return None
    out = []
    for g in entry["guilds"]:
        bot_in = get_guild_any(int(g["id"])) is not None
        try:
            perms = int(g["permissions"])
        except (TypeError, ValueError):
            perms = 0
        can_manage = g["owner"] or bool(perms & (_PERM_ADMIN | _PERM_MANAGE_GUILD))
        if not bot_in and not can_manage:
            continue
        subscribed = peek_active(g["id"])
        out.append(
            {
                "id": g["id"],
                "name": g["name"],
                "icon": g["icon"],
                "owner": g["owner"],
                "bot_in": bot_in,
                "access": g["id"] in accessible_ids,
                "can_add": (not bot_in) and can_manage,
                "subscribed": subscribed,
                # subscribe first, then add - the bot owner can always add
                "can_invite": (not bot_in) and can_manage and (subscribed or is_owner(uid)),
            }
        )
    out.sort(key=lambda s: (not s["access"], not s["bot_in"], not s["can_invite"], not s["can_add"],
                            s["name"].lower()))
    return out


def _managed_server(uid: str, gid: str) -> dict | None:
    """One of the user's own servers (owner / admin / manage server) from their
    login, even if the bot isn't in it yet - so they can subscribe for it."""
    entry = _USER_CACHE.get(str(uid)) or {}
    for g in entry.get("guilds") or []:
        if g["id"] != str(gid):
            continue
        try:
            perms = int(g["permissions"])
        except (TypeError, ValueError):
            perms = 0
        if g["owner"] or perms & (_PERM_ADMIN | _PERM_MANAGE_GUILD):
            return g
    return None


def _movable_sub_for(uid, exclude_gid=None) -> "dict | None":
    """A server the user manages whose ACTIVE, paid/lifetime subscription could be
    moved to another server. Returns {'id','name','mode'} or None. Powers the
    'move my subscription here' option so nobody pays twice to switch servers.

    Wrapped so it can never break /api/me - on any hiccup it just returns None."""
    try:
        entry = _USER_CACHE.get(str(uid)) or {}
        ex = str(exclude_gid) if exclude_gid is not None else None
        for g in entry.get("guilds") or []:
            if ex is not None and g.get("id") == ex:
                continue
            try:
                perms = int(g.get("permissions") or 0)
            except (TypeError, ValueError):
                perms = 0
            if not (g.get("owner") or perms & (_PERM_ADMIN | _PERM_MANAGE_GUILD)):
                continue
            gid = g.get("id")
            if not gid or not peek_active(gid):
                continue
            st = effective_state(int(gid))
            if st["mode"] not in ("paid", "lifetime"):
                continue  # only a real subscription is worth moving
            return {"id": gid, "name": g.get("name") or "your other server", "mode": st["mode"]}
    except Exception as exc:  # noqa: BLE001 - never let this break the panel
        log.warning("_movable_sub_for failed for %s: %s", uid, exc)
    return None


# ---- panel access ----------------------------------------------------------- #

async def _has_web_access(guild, uid, live: bool = False) -> bool:
    """True only if the user may open this guild's web panel.

    Access = the bot operator (support), the server owner, or a member the owner
    added under Settings → Website access. Not every admin - you must own a
    server with the bot, or be added by someone who does.

    Cheap by design: owners/outsiders are decided without any Discord API call.
    Only allowlisted members are checked against the server (so someone who
    left loses access); `live=True` forces a fresh fetch for the logout poll."""
    try:
        uid_i = int(uid)
    except (TypeError, ValueError):
        return False
    if is_owner(uid_i):
        return True
    if getattr(guild, "owner_id", None) == uid_i:
        return True
    allowed = guild_store(DATA, guild.id).get("web_members") or []
    try:
        if uid_i not in {int(x) for x in allowed}:
            return False
    except (TypeError, ValueError):
        return False
    member = None if live else guild.get_member(uid_i)
    if member is None:
        try:
            member = await guild.fetch_member(uid_i)
        except discord.HTTPException:
            return False
    return True


async def _accessible_guilds(uid) -> list:
    out = []
    for g in all_guilds():
        if await _has_web_access(g, uid):
            out.append(g)
    return out


# ---- HTTP handlers ---------------------------------------------------------- #

async def _req_guild(request):
    """The server a request is about: ?guild= on GETs, or "guild" in the JSON
    body on the panel's POSTs (aiohttp caches the body, so handlers can still
    read it afterwards)."""
    gid = request.query.get("guild")
    if not gid and request.method == "POST" and request.can_read_body:
        try:
            body = await request.json()
            gid = str(body.get("guild") or "") if isinstance(body, dict) else ""
        except Exception:
            gid = ""
    return _guild_from(gid or None)


def _guild_from(gid: str | None):
    if not gid:
        _ag = all_guilds()
        return _ag[0] if len(_ag) == 1 else None
    try:
        return get_guild_any(int(gid))
    except (TypeError, ValueError):
        return None


async def _h_index(request):
    base = _canonical_base(request)
    note_base_url(base)  # so links the bot posts point here
    page = _bust(_PAGE_HTML.replace("<!--GM_HEAD-->", _head_meta(base, "Gang Manager · Panel", _SITE_DESC), 1))
    # never let the browser serve a stale panel page (old JS = stuck panels)
    return _aioweb.Response(
        text=page, content_type="text/html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


_ASSET_DIR = os.path.dirname(__file__)
# URL path -> (file in the package, content type). The GM logo everywhere.
_ASSETS = {
    "/gm-logo.png": ("gm-logo.png", "image/png"),
    "/gm-icon.png": ("gm-icon.png", "image/png"),
    "/apple-touch-icon.png": ("gm-icon.png", "image/png"),
    "/gm-og.png": ("gm-og.png", "image/png"),
    "/gm-banner.png": ("gm-banner.png", "image/png"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}
_ASSET_CACHE: dict[str, bytes] = {}
_ASSET_ETAG: dict[str, str] = {}


def _compute_asset_ver() -> str:
    """A short hash of the brand assets' bytes - changes whenever a logo does,
    so versioned URLs bust the browser cache on every asset update."""
    h = hashlib.md5()
    for fn in sorted({v[0] for v in _ASSETS.values()}):
        try:
            with open(os.path.join(_ASSET_DIR, fn), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:10]


_ASSET_VER = _compute_asset_ver()
_VER_PATHS = ("/gm-logo.png", "/gm-icon.png", "/gm-banner.png", "/gm-og.png",
              "/apple-touch-icon.png", "/favicon.ico")


def _bust(doc: str) -> str:
    """Append ?v=<asset version> to every brand-asset URL in a page so browsers
    fetch the current logo instead of a stale cached copy. Apply once per page."""
    for p in _VER_PATHS:
        doc = doc.replace(p, f"{p}?v={_ASSET_VER}")
    return doc


async def _h_asset(request):
    name, ctype = _ASSETS.get(request.path, (None, None))
    if not name:
        return _aioweb.Response(status=404)
    data = _ASSET_CACHE.get(name)
    if data is None:
        try:
            with open(os.path.join(_ASSET_DIR, name), "rb") as f:
                data = f.read()
        except OSError:
            return _aioweb.Response(status=404)
        _ASSET_CACHE[name] = data
        _ASSET_ETAG[name] = '"' + hashlib.md5(data).hexdigest()[:16] + '"'
    etag = _ASSET_ETAG.get(name, "")
    if etag and request.headers.get("If-None-Match") == etag:
        return _aioweb.Response(status=304, headers={
            "ETag": etag, "Cache-Control": "public, max-age=3600"})
    headers = {"Cache-Control": "public, max-age=3600"}
    if etag:
        headers["ETag"] = etag
    return _aioweb.Response(body=data, content_type=ctype, headers=headers)


def _head_meta(base: str, title: str, desc: str) -> str:
    """Favicon + link-preview tags (Discord, etc.) so the GM logo shows up
    wherever the site is opened or shared."""
    base = (base or "").rstrip("/")
    t, d = html.escape(title, quote=True), html.escape(desc, quote=True)
    return (
        '<link rel="icon" type="image/png" href="/gm-icon.png">'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
        '<meta name="theme-color" content="#0b0b10">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="Gang Manager">'
        f'<meta property="og:title" content="{t}">'
        f'<meta property="og:description" content="{d}">'
        f'<meta property="og:image" content="{base}/gm-og.png">'
        '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:image" content="{base}/gm-og.png">'
    )


_SITE_DESC = ("Roster, KOS, strikes, wars, tickets and recruitment - one Discord bot "
              "with a full web control panel for your FiveM gang.")


_LEGAL_TMPL = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>__TITLE__ · Gang Manager</title>__HEAD__<link rel=preconnect href='https://fonts.googleapis.com'><link rel=preconnect href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap' rel=stylesheet><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#060608;--pane:rgba(14,14,18,.66);--line:rgba(255,255,255,.12);--txt:#ececf1;--mute:#9a9aa6;--silver:#c9c9d2;--radius:20px}
html{scroll-behavior:smooth;scroll-padding-top:92px}
body{background:var(--bg);color:var(--txt);font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;overflow-x:hidden;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.aurora{position:fixed;inset:-30% -10% auto -10%;height:140%;z-index:-2;filter:blur(90px);opacity:.55;pointer-events:none}
.aurora i{position:absolute;border-radius:50%;mix-blend-mode:screen;animation:float 18s ease-in-out infinite}
.aurora i:nth-child(1){width:46vw;height:46vw;left:4%;top:2%;background:radial-gradient(circle,#4a4a55,transparent 60%)}
.aurora i:nth-child(2){width:40vw;height:40vw;right:2%;top:12%;background:radial-gradient(circle,#e8e8f0,transparent 62%);opacity:.5;animation-delay:-5s}
.aurora i:nth-child(3){width:52vw;height:52vw;left:28%;top:40%;background:radial-gradient(circle,#2a2a33,transparent 60%);animation-delay:-9s}
@keyframes float{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(6%,4%) scale(1.08)}66%{transform:translate(-5%,-3%) scale(.95)}}
.grain{position:fixed;inset:0;z-index:-1;opacity:.04;pointer-events:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
nav.top{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 26px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);background:rgba(6,6,9,.55);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.3px;color:#fff}
.brand img{width:40px;height:26px;object-fit:contain}
.navlinks{display:flex;gap:22px;align-items:center}
.navlinks a{color:var(--mute);font-size:14px;font-weight:600;transition:color .2s}
.navlinks a:hover,.navlinks a.on{color:#fff}
.btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;padding:11px 18px;border-radius:13px;font-weight:700;font-size:14px;cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.06);color:#fff;transition:transform .1s,box-shadow .2s;white-space:nowrap}
.btn:hover{transform:translateY(-2px)}
.btn.primary{background:linear-gradient(90deg,#c9c9d2,#fff);color:#09090c;border:none;box-shadow:0 10px 30px rgba(220,220,235,.18)}
.btn.sm{padding:9px 15px;font-size:13px}
.hero{position:relative;text-align:center;padding:72px 22px 18px;max-width:820px;margin:0 auto}
.eyebrow{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.04);font-size:12.5px;color:var(--mute);margin-bottom:22px;font-weight:600}
.eyebrow b{color:var(--silver)}
.hero h1{font-size:clamp(34px,6vw,56px);line-height:1.05;letter-spacing:-1.2px;font-weight:850;background:linear-gradient(90deg,#fff,#9a9aa8 62%,#fff);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:sheen 6s ease-in-out infinite}
@keyframes sheen{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
.lead{color:var(--mute);font-size:clamp(15px,2vw,18px);max-width:560px;margin:18px auto 0}
.wrap{position:relative;z-index:1;max-width:1000px;margin:0 auto;padding:30px 22px 70px}
.grid{display:grid;grid-template-columns:220px 1fr;gap:42px;align-items:start}
.toc{position:sticky;top:94px}
.toclab{font-size:11.5px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:var(--silver);margin:0 0 12px;padding-left:14px}
#toc{display:flex;flex-direction:column;gap:1px}
#toc a{display:block;color:var(--mute);font-size:13px;line-height:1.35;padding:7px 14px;border-left:2px solid transparent;transition:.15s}
#toc a:hover{color:#fff}
#toc a.on{color:#fff;border-left-color:var(--silver)}
.doc{min-width:0;border:1px solid var(--line);background:var(--pane);backdrop-filter:blur(8px);border-radius:var(--radius);padding:34px 36px 30px}
.doc .upd{display:inline-block;font-size:12px;font-weight:800;color:#09090c;background:linear-gradient(90deg,#c9c9d2,#fff);padding:5px 13px;border-radius:999px;margin:0 0 22px;letter-spacing:.3px}
.doc h2{font-size:19px;font-weight:800;color:#fff;margin:34px 0 10px;padding-left:16px;position:relative;letter-spacing:-.2px;scroll-margin-top:94px}
.doc h2:first-of-type{margin-top:6px}
.doc h2::before{content:"";position:absolute;left:0;top:3px;bottom:3px;width:3px;border-radius:2px;background:linear-gradient(180deg,#c9c9d2,#fff)}
.doc p,.doc li{color:#c6c6d0;font-size:15px;line-height:1.72}
.doc ul{padding-left:20px;margin:8px 0}
.doc li{margin:7px 0}
.doc li::marker{color:var(--silver)}
.doc a{color:#fff;text-decoration:underline;text-underline-offset:2px;text-decoration-color:rgba(255,255,255,.35)}
.doc a:hover{text-decoration-color:#fff}
.doc b{color:#e7e7ee;font-weight:700}
footer{text-align:center;padding:42px 22px;color:#6f6f78;font-size:13px;border-top:1px solid var(--line);margin-top:24px;line-height:1.6}
footer .fl{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin-bottom:10px}
footer a{color:var(--silver)}
footer p{max-width:620px;margin:0 auto}
.totop{position:fixed;right:20px;bottom:20px;z-index:40;width:42px;height:42px;border-radius:12px;border:1px solid var(--line);background:rgba(14,14,18,.82);backdrop-filter:blur(8px);color:#fff;font-size:18px;cursor:pointer;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}
.totop.show{opacity:1;transform:translateY(0);pointer-events:auto}
.totop:hover{border-color:var(--silver)}
@media(max-width:900px){.grid{grid-template-columns:1fr;gap:16px}.toc{position:static;border:1px solid var(--line);background:var(--pane);border-radius:14px;padding:14px}.toclab{padding-left:4px}#toc{flex-direction:row;flex-wrap:wrap;gap:6px}#toc a{border-left:none;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12.5px}#toc a.on{border-color:var(--silver)}.doc{padding:24px 20px}}
@media(max-width:620px){.hero{padding:52px 16px 14px}nav.top{padding:12px 16px}.navlinks{gap:12px}.navlinks a:not(.btn){display:none}.wrap{padding:24px 16px 64px}}
@media(prefers-reduced-motion:reduce){.aurora i,.hero h1{animation:none}}
</style></head><body>
<div class=aurora><i></i><i></i><i></i></div><div class=grain></div>
<nav class=top>
<a class=brand href="/"><img src="/gm-logo.png" alt="GM">GANG MANAGER</a>
<div class=navlinks>
<a href="/privacy" class="__PON__">Privacy</a>
<a href="/terms" class="__TON__">Terms</a>
<a class="btn sm primary" href="/login">Open panel</a>
</div>
</nav>
<header class=hero>
<div class=eyebrow>&#10022; <b>Legal</b> · __KICK__</div>
<h1>__TITLE__</h1>
<p class=lead>__SUB__</p>
</header>
<main class=wrap>
<div class=grid>
<aside id=tocwrap class=toc><div class=toclab>On this page</div><div id=toc></div></aside>
<article class=doc>__BODY__</article>
</div>
</main>
<footer>
<div class=fl><a href="/privacy">Privacy Policy</a><a href="/terms">Terms of Service</a><a href="/">Home</a></div>
<p>Gang Manager is a community tool for FiveM roleplay Discord servers, operated independently and not affiliated with or endorsed by Discord Inc. or Rockstar Games. Questions or a data request? Contact the bot operator in your Discord server.</p>
</footer>
<button id=toTop class=totop aria-label='Back to top'>&uarr;</button>
<script>
(function(){try{
var doc=document.querySelector('.doc'),toc=document.getElementById('toc');
if(doc&&toc){var hs=doc.querySelectorAll('h2'),items=[];
hs.forEach(function(h,i){var id=h.id||('s'+(i+1));h.id=id;var a=document.createElement('a');a.href='#'+id;a.textContent=h.textContent;toc.appendChild(a);items.push({a:a,h:h});});
if(!items.length){var w=document.getElementById('tocwrap');if(w)w.style.display='none';}
else{var spy=function(){var y=window.scrollY+130,cur=items[0];items.forEach(function(it){if(it.h.offsetTop<=y)cur=it;});items.forEach(function(it){it.a.classList.toggle('on',it===cur);});};window.addEventListener('scroll',spy,{passive:true});spy();}}
var top=document.getElementById('toTop');if(top){window.addEventListener('scroll',function(){top.classList.toggle('show',window.scrollY>600);},{passive:true});top.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});}
}catch(e){}})();
</script>
</body></html>"""


def _legal_page(title: str, body: str) -> str:
    is_privacy = "privacy" in title.lower()
    sub = ("What Gang Manager stores, why it needs it, and how to get your data removed."
           if is_privacy else
           "The agreement for adding and running Gang Manager in your Discord server.")
    head = _head_meta(web_base_url(), f"{title} · Gang Manager", _SITE_DESC)
    return _bust(
        _LEGAL_TMPL
        .replace("__HEAD__", head)
        .replace("__PON__", "on" if is_privacy else "")
        .replace("__TON__", "" if is_privacy else "on")
        .replace("__KICK__", title)
        .replace("__SUB__", sub)
        .replace("__TITLE__", title)
        .replace("__BODY__", body)
    )


_TERMS_BODY = (
    "<p class=upd>Last updated: 13 November 2026</p>"
    "<p>By adding or using Gang Manager (the &ldquo;bot&rdquo;) in your Discord server, you agree to these terms.</p>"
    "<h2>Use of the bot</h2>"
    "<p>The bot provides roster, KOS, strike, application, ticket and stash management for Discord communities. "
    "You and your staff are responsible for how it is used, including roles assigned, data entered and actions taken.</p>"
    "<h2>Subscriptions &amp; billing</h2>"
    "<ul>"
    "<li>Gang Manager is a paid service billed as a <b>monthly subscription per Discord server</b>. One subscription "
    "covers one server.</li>"
    "<li>Payments are processed by Stripe. Subscriptions are charged in advance and <b>renew automatically</b> each "
    "month until cancelled.</li>"
    "<li>You can cancel any time from <b>Settings &rarr; Subscription &rarr; Manage billing</b>. Access continues until "
    "the end of the period you&rsquo;ve paid for.</li>"
    "<li>If a payment fails or a subscription ends, the bot&rsquo;s features are paused in that server until it&rsquo;s "
    "renewed. Your server&rsquo;s data is kept in the meantime.</li>"
    "<li>Refunds are handled case by case &mdash; contact the operator.</li>"
    "<li>Prices may change; any change applies from your next billing period and will be shown before you pay.</li>"
    "</ul>"
    "<h2>Acceptable use</h2>"
    "<p>Do not use the bot to break Discord&rsquo;s Terms of Service or Community Guidelines, to harass others, or for "
    "anything unlawful. Do not attempt to abuse, overload, reverse-engineer or disrupt the bot.</p>"
    "<h2>Availability</h2>"
    "<p>The bot is provided &ldquo;as is&rdquo; and &ldquo;as available&rdquo;, with no guarantee of uptime, accuracy "
    "or that it is error-free. Features may change or stop at any time.</p>"
    "<h2>Limitation of liability</h2>"
    "<p>To the maximum extent permitted by law, the operator is not liable for any loss or damage arising from use of, "
    "or inability to use, the bot &mdash; including lost or altered data.</p>"
    "<h2>Privacy</h2>"
    "<p>Your use of the bot is also covered by our <a href='/privacy'>Privacy Policy</a>.</p>"
    "<h2>Changes</h2>"
    "<p>These terms may be updated. Continued use after a change means you accept the updated terms.</p>"
)

_PRIVACY_BODY = (
    "<p class=upd>Last updated: 13 November 2026</p>"
    "<p>This policy explains what Gang Manager (the &ldquo;bot&rdquo;) stores and why.</p>"
    "<h2>What we collect</h2>"
    "<ul>"
    "<li><b>Discord identifiers &amp; names:</b> user, server, role and channel IDs, usernames and display names &mdash; "
    "needed to show rosters, assign roles, log actions and run the web panel.</li>"
    "<li><b>Data you enter:</b> roster entries, KOS entries and reasons, strikes and reasons, applications (EOIs) and votes, "
    "ticket/order details and stash items, entered by staff and members through commands and the web panel.</li>"
    "<li><b>Website login:</b> when you log in to the web panel we use Discord OAuth2 with the &ldquo;identify&rdquo; and "
    "&ldquo;guilds&rdquo; scopes &mdash; to confirm who you are and to show which of your servers run the bot or can add it. "
    "Your server list is held in memory for your session only and isn&rsquo;t written to the data file. We store a signed "
    "session cookie and never receive your password.</li>"
    "<li><b>Payments:</b> payments are handled by Stripe &mdash; we never see or store card details. To link a subscription "
    "to your server we store the Stripe customer and subscription IDs, the Discord ID of whoever subscribed, and the "
    "paid-until date.</li>"
    "<li><b>Join/leave events (optional):</b> if the Server Members Intent is enabled, joins and leaves are logged "
    "(user, account age, member count).</li>"
    "</ul>"
    "<h2>What we do not collect</h2>"
    "<p>We do not read your message content &mdash; the bot works through slash commands and buttons. "
    "We do not sell or share your data with advertisers or other third parties.</p>"
    "<h2>Where it is stored</h2>"
    "<p>Data is stored in the bot&rsquo;s data file on its host. Access is limited to your server&rsquo;s owner (and anyone "
    "they add to the web panel) and the bot operator, who can open any server&rsquo;s panel to provide support and manage "
    "subscriptions.</p>"
    "<h2>Retention &amp; deletion</h2>"
    "<p>Data is kept while the bot is in your server. To remove your data, ask a server administrator to take you off the "
    "roster and lists, or ask the bot operator to delete the server&rsquo;s data. Removing the bot from a server stops new collection.</p>"
    "<h2>Third parties</h2>"
    "<p>The bot runs on Discord, which is governed by <a href='https://discord.com/privacy'>Discord&rsquo;s Privacy Policy</a>. "
    "Payments are processed by Stripe under <a href='https://stripe.com/privacy'>Stripe&rsquo;s Privacy Policy</a>.</p>"
    "<h2>Changes</h2>"
    "<p>We may update this policy; the &ldquo;last updated&rdquo; date will change.</p>"
)


async def _h_terms(request):
    return _aioweb.Response(text=_legal_page("Terms of Service", _TERMS_BODY), content_type="text/html")


async def _h_privacy(request):
    return _aioweb.Response(text=_legal_page("Privacy Policy", _PRIVACY_BODY), content_type="text/html")


def _brand_info() -> dict:
    """Primary server's name + icon, for branding the page (public info)."""
    g = bot.guilds[0] if getattr(bot, "guilds", None) else None
    icon = ""
    if g is not None:
        ic = getattr(g, "icon", None)
        if ic is not None:
            try:
                icon = str(ic.url)
            except Exception:
                icon = ""
    return {"name": g.name if g is not None else "Gang Manager", "icon": icon}


def _public_cfg() -> dict:
    """Public, unauthenticated bits the page needs (invite link, pricing)."""
    return {
        "invite_url": _invite_url(),
        "stripe": _stripe_ready(),
        "plan": _plan_public(),
        "support_url": _support_url(),
    }


def _sub_public(gid: int) -> dict:
    s = effective_state(gid)
    return {
        "active": s["active"],
        "mode": s["mode"],
        "status": s["status"],
        "days_left": s["days_left"],
        "expires_at": s["expires_at"],
        "lifetime": s["lifetime"],
        "has_billing": bool(s.get("customer_id")),
        "billing_enabled": s["billing_enabled"],
    }


def _json_nostore(payload):
    # dynamic auth state must never be cached by the browser
    return _aioweb.json_response(payload, headers={"Cache-Control": "no-store, max-age=0"})


async def _h_me(request):
    brand = _brand_info()
    cfg = _public_cfg()
    if not _configured():
        log.info("/api/me -> not_configured")
        return _json_nostore({"authed": False, "reason": "not_configured", "brand": brand, "public": cfg})
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        log.info("/api/me -> login (no/invalid session cookie)")
        return _json_nostore({"authed": False, "reason": "login", "brand": brand, "public": cfg})
    try:
        guilds = await _accessible_guilds(uid)
        profile = _cached_profile(uid)
        servers = _server_picker(uid, {str(g.id) for g in guilds})
        if not guilds:
            log.info("/api/me uid=%s -> no_access (servers=%s)", uid,
                     "none" if servers is None else len(servers))
            return _json_nostore({
                "authed": False, "reason": "no_access", "brand": brand, "public": cfg,
                "user": profile, "servers": servers, "move_from": _movable_sub_for(uid),
            })
        log.info("/api/me uid=%s -> authed (guilds=%d, owner=%s)", uid, len(guilds), is_owner(uid))
        return _json_nostore({
            "authed": True,
            "brand": brand,
            "public": cfg,
            "user": {**profile, "owner": is_owner(uid)},
            "servers": servers,
            "move_from": _movable_sub_for(uid),
            "guilds": [
                {
                    "id": str(g.id),
                    "name": g.name,
                    "subscription": _sub_public(g.id),
                    "can_setup": _can_setup(g, uid),
                }
                for g in guilds
            ],
        })
    except Exception as exc:  # noqa: BLE001 - surface, never 500 the panel into a blank
        log.exception("/api/me failed for uid=%s: %s", uid, exc)
        return _json_nostore(
            {"authed": False, "reason": "error", "brand": brand, "public": cfg, "error": str(exc)}
        )


async def _h_access(request):
    """Fast, authoritative 'are you still allowed in' check for the client's
    logout poll. Fetches the member live so a just-removed role logs them out
    right away. Returns {ok: bool} and never leaks detail."""
    if not _configured():
        return _aioweb.json_response({"ok": False})
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.json_response({"ok": False})
    guild = await _req_guild(request)
    if guild is not None:
        return _aioweb.json_response({"ok": await _has_web_access(guild, uid, live=True)})
    # no specific guild - allowed if still in any accessible guild
    for g in all_guilds():
        if await _has_web_access(g, uid, live=True):
            return _aioweb.json_response({"ok": True})
    return _aioweb.json_response({"ok": False})


def _simple_page(icon: str, title: str, text: str, actions: str = "", status: int = 200):
    """A small branded page (errors, notices) that matches the site's look.
    `text` and `actions` are trusted HTML built here, never user input."""
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · Gang Manager</title>"
        + _head_meta(web_base_url(), f"{title} · Gang Manager", _SITE_DESC)
        + "<style>"
        ":root{color-scheme:dark}*{box-sizing:border-box}"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#ececf1;"
        "font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "background:radial-gradient(1100px 560px at 15% -10%,#2a2a33 0%,transparent 60%),"
        "radial-gradient(900px 520px at 110% 115%,#3b3b46 0%,transparent 55%),#060608}"
        ".card{width:100%;max-width:440px;text-align:center;padding:36px 28px;border-radius:24px;"
        "background:rgba(16,16,22,.74);border:1px solid rgba(255,255,255,.12);backdrop-filter:blur(10px);"
        "box-shadow:0 30px 90px rgba(0,0,0,.55);animation:in .45s cubic-bezier(.2,.7,.2,1)}"
        "@keyframes in{from{opacity:0;transform:translateY(14px) scale(.98)}to{opacity:1;transform:none}}"
        ".logo{width:84px;height:52px;object-fit:contain;filter:drop-shadow(0 0 26px rgba(255,255,255,.22))}"
        ".ic{font-size:30px;margin:12px 0 2px}h1{font-size:22px;margin:6px 0 8px;letter-spacing:-.3px}"
        "p{color:#a5a5b0;line-height:1.6;margin:0 0 22px;font-size:15px}code{color:#e8e8ee}"
        ".row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}"
        "a.b{display:inline-flex;align-items:center;gap:8px;padding:11px 18px;border-radius:12px;font-weight:700;"
        "text-decoration:none;font-size:14px;border:1px solid rgba(255,255,255,.16);color:#fff;background:rgba(255,255,255,.06)}"
        "a.b.p{background:linear-gradient(90deg,#c9c9d2,#fff);color:#09090c;border:none}"
        "</style></head><body><div class=card>"
        "<img class=logo src='/gm-logo.png' alt='Gang Manager'>"
        f"<div class=ic>{icon}</div><h1>{html.escape(title)}</h1><p>{text}</p>"
        f"<div class=row>{actions}</div></div></body></html>"
    )
    return _aioweb.Response(text=_bust(doc), status=status, content_type="text/html")


_RETRY = "<a class='b p' href='/login'>Try again</a><a class='b' href='/'>Home</a>"


async def _h_login(request):
    if not _configured():
        return _simple_page(
            "⚙️", "Login isn't set up yet",
            "The site owner still needs to add <b>DISCORD_CLIENT_SECRET</b> to the server settings.",
            "<a class='b' href='/'>Back home</a>", 503,
        )
    canon = _canonical_base(request)
    if canon.rstrip("/").lower() != _request_base(request).rstrip("/").lower():
        # keep the whole login on one host so the redirect URI always matches
        return _aioweb.HTTPFound(f"{canon}/login")
    if not _client_id():
        return _simple_page(
            "⏳", "Almost ready", "The bot is still starting up - give it a few seconds.",
            "<a class='b p' href='/login'>Try again</a>", 503,
        )
    resp = _aioweb.HTTPFound(_authorize_url(request, _make_state()))
    nxt = request.query.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):  # local paths only
        resp.set_cookie("tenshi_next", nxt, max_age=600, httponly=True, secure=True, samesite="Lax")
    return resp


async def _h_callback(request):
    err = request.query.get("error")
    if err:
        if err == "access_denied":
            return _simple_page("👋", "Login cancelled", "No problem - log in whenever you're ready.", _RETRY, 400)
        return _simple_page(
            "⚠️", "Discord stopped the login",
            f"Discord said <code>{html.escape(err)}</code>. Please try again.", _RETRY, 400,
        )
    # Returning from an "add the bot" authorization: our invite link carries
    # response_type=code + the /callback redirect_uri, so Discord appends guild_id
    # here once the bot is added. The user is already signed in, so skip the login
    # exchange and drop them back on the panel, focused on that server.
    added_gid = request.query.get("guild_id")
    if added_gid and added_gid.isdigit():
        return _aioweb.HTTPFound(f"/panel?added={added_gid}")
    if not _state_ok(request.query.get("state")):
        return _simple_page(
            "⏳", "That login link expired",
            "It was started a while ago or from an old tab. Start again and it'll go straight through.",
            _RETRY, 400,
        )
    code = request.query.get("code")
    if not code:
        return _simple_page("⚠️", "Something went wrong", "Discord didn't send a login code back.", _RETRY, 400)
    tok = await _oauth_exchange(code, _redirect_uri(request))
    access = (tok or {}).get("access_token")
    if not access:
        return _simple_page(
            "🔑", "Couldn't finish logging in",
            "Discord didn't accept this login. Try again - if it keeps happening, the site owner "
            "should check the OAuth2 client secret and that this site's <code>/callback</code> URL "
            "is listed under OAuth2 → Redirects.",
            _RETRY, 400,
        )
    user = await _oauth_user(access)
    if not user or not user.get("id"):
        return _simple_page("⚠️", "Couldn't read your account", "Discord didn't share your account details.", _RETRY, 400)
    uid = str(user["id"])
    _remember_user(uid, user, await _oauth_guilds(access))
    # Sign in even with no accessible server yet - the panel shows the next step
    # (add the bot to one of your servers / ask an owner to add you).
    if not is_owner(uid):  # operator support visits don't spam customers' logs
        uname = user.get("global_name") or user.get("username") or uid
        for g in await _accessible_guilds(uid):
            try:
                await send_log(
                    g, guild_store(DATA, g.id), f"🌐 **{uname}** signed in to the website.",
                    category="web", actor=g.get_member(int(uid)),
                )
            except (discord.HTTPException, ValueError, KeyError, TypeError):
                pass
    _nxt = request.cookies.get("tenshi_next", "")
    _dest = _nxt if (_nxt.startswith("/") and not _nxt.startswith("//")) else "/panel"
    resp = _aioweb.HTTPFound(_dest)
    resp.del_cookie("tenshi_next")
    resp.set_cookie(_COOKIE, _sign_session(uid), max_age=43200, httponly=True, secure=True, samesite="Lax")
    resp.del_cookie(_STATE_COOKIE)
    return resp


async def _h_logout(request):
    uid = _read_session(request.cookies.get(_COOKIE))
    if uid:
        _USER_CACHE.pop(str(uid), None)
    resp = _aioweb.HTTPFound("/")
    resp.del_cookie(_COOKIE)
    return resp


def _web_actor(request, guild):
    """The logged-in website user as a guild member (for log attribution), or None."""
    try:
        uid = _read_session(request.cookies.get(_COOKIE))
        return guild.get_member(int(uid)) if uid else None
    except (TypeError, ValueError):
        return None


async def _gate(request, guild, allow_disabled: bool = False) -> tuple[bool, int, str]:
    """Allow only a logged-in user who may open this guild's web panel.

    Also blocks when the panel is switched off (``web_enabled``) or the server's
    subscription has lapsed - except handlers that pass ``allow_disabled=True``
    (Settings, the on/off toggle), so staff can always get back in to fix it."""
    if not _configured():
        return (False, 503, "Login isn't configured on this server.")
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return (False, 401, "Not logged in.")
    if not await _has_web_access(guild, uid):
        return (False, 403, "You don't have the website-access role.")
    if not allow_disabled:
        store = guild_store(DATA, guild.id)
        if not store.get("web_enabled", True):
            return (False, 503, "The website panel is turned off for this server.")
        if not is_active(guild.id) and not is_owner(uid):
            return (False, 402, "This server needs an active subscription.")
    return (True, 200, "")


def _can_setup(guild, uid) -> bool:
    """Only the server owner, whoever bought the subscription, or the bot
    operator may change settings - not every admin or allowlisted member."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    if is_owner(uid):  # bot operator (BOT_OWNER_IDS)
        return True
    if getattr(guild, "owner_id", None) == uid:
        return True
    sub = guild_store(DATA, guild.id).get("subscription") or {}
    pid = sub.get("purchaser_id")
    try:
        return bool(pid) and int(pid) == uid
    except (TypeError, ValueError):
        return False


async def _setup_gate(request, guild) -> tuple[bool, int, str]:
    """Panel access (allow_disabled) AND setup-owner - for config endpoints."""
    ok, code, msg = await _gate(request, guild, True)
    if not ok:
        return (ok, code, msg)
    uid = _read_session(request.cookies.get(_COOKIE))
    if not _can_setup(guild, uid):
        return (False, 403, "Only the server owner can change settings.")
    return (True, 200, "")


async def _h_roster(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(
        {
            "guild": {"id": str(guild.id), "name": guild.name},
            "members": _roster_members(guild, store),
            "ranks": _rank_options(guild, store),
            "rank_order": _RANK_ORDER,
        }
    )


async def _h_overview(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"overview": _overview(guild, store)})


async def _h_kos(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"members": _kos_members(guild, store)})


async def _h_role(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    action = body.get("action")
    if action not in ("add", "remove"):
        return _aioweb.json_response({"error": "action must be add or remove."}, status=400)
    try:
        uid = int(body.get("user_id"))
        rid = int(body.get("role_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id or role_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = await _set_gang_role(guild, store, uid, rid, action, actor=_web_actor(request, guild))
    return _aioweb.json_response(result, status=200 if result.get("ok") else 400)


async def _h_kos_add(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(str(body.get("user_id") or "").strip())
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Enter a valid Discord user ID."}, status=400)
    note = (str(body.get("reason") or body.get("note") or "")).strip()[:300] or None
    store = guild_store(DATA, guild.id)
    result = await _add_kos(guild, store, uid, note, actor=_web_actor(request, guild))
    return _aioweb.json_response(result, status=200 if result.get("ok") else 400)


async def _h_kos_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = await _remove_kos(guild, store, uid, actor=_web_actor(request, guild))
    return _aioweb.json_response(result)


async def _h_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id."}, status=400)
    store = guild_store(DATA, guild.id)
    actor = _read_session(request.cookies.get(_COOKIE))
    result = await _remove_from_roster(guild, store, uid, actor=f"user {actor}", actor_member=_web_actor(request, guild))
    return _aioweb.json_response(result)


async def _h_stash(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    items = _stash_items(store)
    return _aioweb.json_response({"items": items, "total": sum(i["qty"] for i in items)})


async def _h_stash_set(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    result = _stash_upsert(
        store, body.get("item"), body.get("qty"), body.get("mode", "set"),
        body.get("weight"), body.get("image"),
    )
    if result.get("ok"):
        item = (body.get("item") or "").strip()
        mode = body.get("mode", "set")
        verb = {"add": "added to", "remove": "removed from", "set": "set in"}.get(mode, "updated in")
        try:
            await send_log(
                guild, store, f"🌐 Stash: **{item}** {verb} the stash via the website.",
                category="web", actor=_web_actor(request, guild),
                fields=[
                    ("Item", item or "-", True),
                    ("Qty", str(body.get("qty", "") or "-"), True),
                    ("Mode", mode, True),
                ],
            )
        except discord.HTTPException:
            pass
    return _aioweb.json_response(result, status=200 if result.get("ok") else 400)


async def _h_stash_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    item = (body.get("item") or "").strip()
    result = _stash_remove(store, body.get("item"))
    if item:
        try:
            await send_log(
                guild, store, f"🌐 Stash: **{item}** removed from the stash via the website.",
                category="web", actor=_web_actor(request, guild),
            )
        except discord.HTTPException:
            pass
    return _aioweb.json_response(result)


async def _h_stash_scan(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        post = await request.post()
    except Exception:
        return _aioweb.json_response({"error": "Bad upload."}, status=400)
    field = post.get("image")
    data = None
    if field is not None:
        data = field.file.read() if hasattr(field, "file") else (
            bytes(field) if isinstance(field, (bytes, bytearray)) else None
        )
    if not data:
        return _aioweb.json_response({"error": "No image uploaded."}, status=400)
    if len(data) > 12 * 1024 * 1024:
        return _aioweb.json_response({"error": "Image too big (max 12 MB)."}, status=400)
    if not _ocr_available():
        return _aioweb.json_response(
            {"candidates": [], "ocr": False,
             "note": "The OCR engine (Tesseract) isn't installed on the server yet - "
                     "the next deploy adds it. You can still add items by hand."}
        )
    cands = _scan_stash(data)
    if not cands:
        return _aioweb.json_response(
            {"candidates": [], "ocr": True,
             "note": "Couldn't read items from that screenshot - try a bigger/clearer "
                     "crop of just the item list."}
        )
    return _aioweb.json_response({"candidates": cands, "ocr": True})


async def _h_wars(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"wars": _wars_list(store)})


async def _h_war_cancel(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    enemy = str(body.get("enemy") or "").strip()
    if not enemy:
        return _aioweb.json_response({"error": "No war specified."}, status=400)
    store = guild_store(DATA, guild.id)
    war = None
    for w in store.get("wars", []):
        if str(w.get("enemy", "")).lower() == enemy.lower() and w.get("active", True):
            war = w
            break
    if war is None:
        return _aioweb.json_response({"error": "No active war against that enemy."}, status=404)
    war["active"] = False
    war["cancelled"] = True
    save_data(DATA)
    try:  # best-effort: update the war's card in Discord so it stops showing live
        from . import gang
        await gang._refresh_war_panel(guild, war)
    except Exception:  # noqa: BLE001
        log.exception("war panel refresh after cancel failed")
    return _aioweb.json_response({"ok": True, "wars": _wars_list(store)})


async def _h_war_start(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    enemy = str(body.get("enemy") or "").strip()
    raw_ft = body.get("first_to")
    first_to = None
    if raw_ft not in (None, "", 0, "0"):
        try:
            first_to = int(raw_ft)
        except (TypeError, ValueError):
            return _aioweb.json_response({"error": "First-to must be a number."}, status=400)
        if not 1 <= first_to <= 100:
            return _aioweb.json_response({"error": "First-to must be between 1 and 100."}, status=400)
    from . import gang
    try:
        war = await gang.web_start_war(guild, enemy, first_to, actor=_web_actor(request, guild))
    except ValueError as exc:
        return _aioweb.json_response({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        log.exception("web war start failed")
        return _aioweb.json_response({"error": "Couldn't start the war."}, status=500)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(
        {"ok": True, "posted": bool(war.get("_posted")), "wars": _wars_list(store)}
    )


async def _h_war_round(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    enemy = str(body.get("enemy") or "").strip()
    result = str(body.get("result") or "").strip().lower()
    if result not in ("win", "loss", "undo"):
        return _aioweb.json_response({"error": "Unknown action."}, status=400)
    from . import gang
    try:
        await gang.web_war_round(guild, enemy, result, actor=_web_actor(request, guild))
    except ValueError as exc:
        return _aioweb.json_response({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        log.exception("web war round failed")
        return _aioweb.json_response({"error": "Couldn't update the war."}, status=500)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"ok": True, "wars": _wars_list(store)})


async def _h_war_end(request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    enemy = str(body.get("enemy") or "").strip()
    from . import gang
    try:
        await gang.web_end_war(guild, enemy, actor=_web_actor(request, guild))
    except ValueError as exc:
        return _aioweb.json_response({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        log.exception("web war end failed")
        return _aioweb.json_response({"error": "Couldn't end the war."}, status=500)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"ok": True, "wars": _wars_list(store)})


async def _h_orders(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(
        {
            "orders": _orders_list(guild, store),
            "types": _ticket_types(store),
            "open": bool(store.get("tickets_open", True)),
        }
    )


async def _h_order_create(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    type_name = (body.get("type") or "").strip()
    from .tickets import create_order, _find_ticket_type
    tt = _find_ticket_type(store, type_name)
    if tt is None:
        return _aioweb.json_response({"error": "Pick a valid ticket type."}, status=400)
    # customer: an optional member id; otherwise the order has no assigned customer
    owner = None
    cust = body.get("user_id")
    if cust:
        try:
            owner = guild.get_member(int(cust))
        except (TypeError, ValueError):
            owner = None
    res = await create_order(
        guild, store, tt["name"], owner,
        body.get("guns") or "", body.get("quantity") or "", body.get("notes") or "",
        actor=_web_actor(request, guild),
    )
    if not res.get("ok"):
        return _aioweb.json_response({"error": res.get("error", "Couldn't open the ticket.")}, status=400)
    order, channel = res["order"], res["channel"]
    try:
        who = f" for <@{owner.id}>" if owner is not None else ""
        await send_log(
            guild, store,
            f"🌐 New **{tt['name']}** order `#{order['id']}` created via the website{who}.",
            category="web", actor=_web_actor(request, guild),
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True, "id": order["id"], "channel_id": str(channel.id)})


async def _h_order_paid(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        oid = int(body.get("order_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad order_id."}, status=400)
    store = guild_store(DATA, guild.id)
    order = next((o for o in store.get("orders", []) if o.get("id") == oid), None)
    if order is None:
        return _aioweb.json_response({"error": "Order not found."}, status=404)
    paid = bool(body.get("paid", True))
    order["paid"] = paid
    save_data(DATA)
    try:
        from .tickets import _rename_order_channel, _refresh_order_card
        await _refresh_order_card(guild, order)
        await _rename_order_channel(guild, order)
    except discord.HTTPException:
        pass
    try:
        await send_log(
            guild, store,
            f"🌐 Order `#{oid}` marked **{'paid' if paid else 'unpaid'}** via the website.",
            category="web", actor=_web_actor(request, guild),
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True, "paid": paid})


async def _h_orders_clear_closed(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    before = len(store.get("orders", []))
    store["orders"] = [o for o in store.get("orders", []) if o.get("status") != "closed"]
    removed = before - len(store["orders"])
    save_data(DATA)
    if removed:
        try:
            await send_log(
                guild, store, f"🌐 Cleared **{removed}** closed order(s) from the list via the website.",
                category="web", actor=_web_actor(request, guild),
            )
        except discord.HTTPException:
            pass
    return _aioweb.json_response({"ok": True, "removed": removed})


async def _h_order_close(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        oid = int(body.get("order_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad order_id."}, status=400)
    store = guild_store(DATA, guild.id)
    order = next((o for o in store.get("orders", []) if o.get("id") == oid), None)
    if order is None:
        return _aioweb.json_response({"error": "Order not found."}, status=404)
    if order.get("status") == "closed":
        return _aioweb.json_response({"ok": True, "already": True})

    closer = _web_actor(request, guild)
    reason = (body.get("reason") or "Force-closed from the website").strip()
    channel = None
    if order.get("channel_id"):
        try:
            channel = guild.get_channel(int(order["channel_id"]))
        except (TypeError, ValueError):
            channel = None

    # Always mark it closed now - that's the "force" guarantee, even if the
    # channel is gone or deleting it fails.
    order["status"] = "closed"
    order["closed_ts"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    order["closed_by"] = getattr(closer, "id", None)
    save_data(DATA)

    if channel is not None:
        # Transcript + DM + channel delete runs in the background so the request
        # returns straight away (it ends with a 5s wait before deleting).
        from .tickets import _close_ticket_core
        import asyncio
        asyncio.create_task(_close_ticket_core(guild, channel, closer, store, order, reason))
    else:
        try:
            await send_log(
                guild, store,
                f"🔒 **{order.get('type', 'order')}** #{oid} force-closed (no channel on record).",
                category="tickets", actor=closer,
            )
        except discord.HTTPException:
            pass
    try:
        await send_log(
            guild, store,
            f"🌐 Order `#{oid}` force-closed via the website.",
            category="web", actor=closer,
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True})


async def _h_ticket_type_create(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    name = (body.get("name") or "").strip()[:60]
    if not name:
        return _aioweb.json_response({"error": "Give the order type a name."}, status=400)
    emoji = (body.get("emoji") or "").strip()[:8] or None
    store = guild_store(DATA, guild.id)
    types = store.setdefault("ticket_types", [])
    for t in types:
        if t["name"].lower() == name.lower():
            t["emoji"] = emoji
            break
    else:
        if len(types) >= 25:
            return _aioweb.json_response({"error": "You can have at most 25 order types."}, status=400)
        types.append({"name": name, "category_id": None, "emoji": emoji})
    save_data(DATA)
    try:
        from .tickets import _refresh_ticket_panel
        await _refresh_ticket_panel(guild, store)
    except discord.HTTPException:
        pass
    try:
        await send_log(
            guild, store, f"🌐 Order type **{name}** saved via the website.",
            category="web", actor=_web_actor(request, guild),
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True, "types": _ticket_types(store)})


async def _h_ticket_type_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    name = (body.get("name") or "").strip()
    store = guild_store(DATA, guild.id)
    before = len(store.get("ticket_types", []))
    store["ticket_types"] = [t for t in store.get("ticket_types", []) if t["name"].lower() != name.lower()]
    if len(store["ticket_types"]) == before:
        return _aioweb.json_response({"error": "No order type by that name."}, status=404)
    save_data(DATA)
    try:
        from .tickets import _refresh_ticket_panel
        await _refresh_ticket_panel(guild, store)
    except discord.HTTPException:
        pass
    try:
        await send_log(
            guild, store, f"🌐 Order type **{name}** removed via the website.",
            category="web", actor=_web_actor(request, guild),
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True, "types": _ticket_types(store)})


async def _h_strikes(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response(
        {"members": _strikes_list(guild, store), "limit": int(store.get("strike_limit", 3) or 3)}
    )


async def _h_strike_remove(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        sid = int(body.get("strike_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad strike_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = _remove_strike(store, sid)
    if result.get("ok"):
        try:
            await send_log(
                guild, store,
                f"🗑️ Strike `#{sid}` removed from <@{result.get('user_id')}> via the website.",
                category="web", actor=_web_actor(request, guild),
            )
        except discord.HTTPException:
            pass
        await notify_strike_change(guild, store, result["user_id"], kind="remove", strike_id=sid)
    return _aioweb.json_response(result, status=200 if result.get("ok") else 404)


async def _h_strike_clear(request):
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Bad user_id."}, status=400)
    store = guild_store(DATA, guild.id)
    result = _clear_strikes(store, uid)
    if result.get("cleared"):
        try:
            await send_log(
                guild, store,
                f"🧹 Cleared **{result['cleared']}** strike(s) from <@{uid}> via the website.",
                category="web", actor=_web_actor(request, guild),
            )
        except discord.HTTPException:
            pass
        await notify_strike_change(guild, store, uid, kind="clear")
    return _aioweb.json_response(result)


async def _h_eois(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"eois": _eois_list(guild, store)})


async def _web_end_eoi(request, force: str | None):
    """Shared force-end for the web: tally+close (force=None), force-accept
    (force='accept') or force-deny (force='deny'). Always drops the EOI off the
    pending list, falling back to the configured EOI channel for older apps."""
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad JSON body."}, status=400)
    guild = _guild_from(str(body.get("guild") or ""))
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    mid = str(body.get("msg_id") or "")
    store = guild_store(DATA, guild.id)
    if mid not in store.get("eoi_apps", {}):
        return _aioweb.json_response({"error": "That application no longer exists."}, status=404)
    _app = store.get("eoi_apps", {}).get(mid, {})
    rec = dict(store.get("eoi_timers", {}).get(mid) or {})
    if not rec.get("channel_id"):
        rec["channel_id"] = store.get("eoi_channel_id")
    timer = store.setdefault("eoi_timers", {}).setdefault(mid, {})
    timer["done"] = True
    if rec.get("channel_id"):
        timer["channel_id"] = rec["channel_id"]
    save_data(DATA)
    try:
        await _close_eoi(guild, store, int(mid), rec, force=force)
    except (discord.HTTPException, ValueError) as exc:
        log.info("web EOI end (force=%s): couldn't edit the post (%s)", force, exc)
    action = "accepted" if force == "accept" else "denied" if force == "deny" else "closed"
    _uid = _app.get("user_id")
    _who = f"<@{_uid}>" if _uid else (_app.get("ign") or "applicant")
    try:
        await send_log(
            guild, store, f"🌐 EOI for {_who} **{action}** via the website.",
            category="web", actor=_web_actor(request, guild),
            fields=[("Applicant", _who, True), ("Result", action.title(), True)],
        )
    except discord.HTTPException:
        pass
    return _aioweb.json_response({"ok": True})


async def _h_eoi_close(request):
    return await _web_end_eoi(request, None)


async def _h_eoi_accept(request):
    return await _web_end_eoi(request, "accept")


# --------------------------------------------------------------------------- #
# Subscriptions, the "Add to Discord" invite, and the settings editor
# --------------------------------------------------------------------------- #

# Permissions the bot asks for on invite (Manage Roles/Channels, Send/Embed/
# Attach, Read History, Manage/Add-react Messages, View, Mention). Override with
# the INVITE_PERMISSIONS env var.
_DEFAULT_INVITE_PERMS = 268692560


def _invite_perms() -> str:
    raw = os.getenv("INVITE_PERMISSIONS", "").strip()
    return raw if raw.isdigit() else str(_DEFAULT_INVITE_PERMS)


def _invite_url(guild_id: str | None = None, redirect_uri: str | None = None,
                client_id: str | None = None) -> str:
    """Bot invite link. With a guild id, Discord pre-selects that server. With a
    redirect_uri (which must be listed under OAuth2 → Redirects - we reuse the
    login /callback), Discord sends the user back there with guild_id after they
    add the bot, instead of its terminal 'you may close this tab' page.
    client_id picks a pool bot instead of the main bot."""
    cid = str(client_id) if client_id else _client_id()
    if not cid:
        return ""
    from urllib.parse import urlencode

    params = {
        "client_id": cid,
        "scope": "bot applications.commands",
        "permissions": _invite_perms(),
    }
    if guild_id and str(guild_id).isdigit():
        params["guild_id"] = str(guild_id)
        params["disable_guild_select"] = "true"
    if redirect_uri:
        params["response_type"] = "code"
        params["redirect_uri"] = redirect_uri
    return f"{_OAUTH_AUTHORIZE}?{urlencode(params)}"


def _public_base(request) -> str:
    return _canonical_base(request)


def _stripe_ready() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY") and os.getenv("STRIPE_PRICE_ID"))


def _plan_public() -> dict:
    p = PLANS[DEFAULT_PLAN]
    return {"id": DEFAULT_PLAN, "label": p["label"], "price": p["price"], "days": p["days"]}


async def _stripe_call(method: str, path: str, form=None, params=None) -> dict | None:
    """Minimal Stripe REST call. Returns parsed JSON or None (and logs why)."""
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key or _aiohttp is None:
        return None
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.request(
                method,
                f"https://api.stripe.com/v1/{path}",
                data=form,
                params=params,
                headers={"Authorization": f"Bearer {key}"},
            ) as r:
                body = await r.json(content_type=None)
                if r.status >= 400:
                    log.error("Stripe %s %s failed (%s): %s", method, path, r.status,
                              (body or {}).get("error", {}).get("message"))
                    return None
                return body
    except Exception as exc:  # pragma: no cover - network
        log.error("Stripe %s %s error: %s", method, path, exc)
        return None


async def _stripe_post(path: str, form: list[tuple[str, str]]) -> dict | None:
    return await _stripe_call("POST", path, form=form)


async def _stripe_get(path: str, params: dict | None = None) -> dict | None:
    return await _stripe_call("GET", path, params=params)


def _ts_to_dt(ts) -> "datetime.datetime | None":
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _sub_period_end(sub: dict):
    """A Stripe Subscription's current period end (field moved onto the items
    in newer API versions)."""
    end = sub.get("current_period_end")
    if not end:
        items = ((sub.get("items") or {}).get("data")) or []
        ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
        end = max(ends) if ends else None
    return _ts_to_dt(end)


async def sync_subscriptions_from_stripe() -> int:
    """Re-apply every live Stripe subscription to its server. Stripe is the
    source of truth for payments, so this heals subscriptions if the data file
    was reset (e.g. a redeploy without a volume). Safe to run any time - grants
    only ever move the paid-until date forward. Returns servers updated."""
    import datetime as _dt
    if not os.getenv("STRIPE_SECRET_KEY"):
        return 0
    updated, after, pages = 0, None, 0
    while pages < 20:
        params = {"status": "all", "limit": "100"}
        if after:
            params["starting_after"] = after
        page = await _stripe_get("subscriptions", params)
        if not page:
            break
        pages += 1
        for sub in page.get("data") or []:
            meta = sub.get("metadata") or {}
            gid = meta.get("guild_id")
            if not gid or not str(gid).isdigit():
                continue  # not a Gang Manager subscription
            if sub.get("status") not in ("active", "trialing", "past_due"):
                continue
            end = _sub_period_end(sub)
            until = (end or _dt.datetime.now(_dt.timezone.utc)) + _dt.timedelta(days=1)
            grant(
                int(gid), until=until, plan=DEFAULT_PLAN, status="active",
                customer_id=sub.get("customer"), sub_id=sub.get("id"),
                purchaser_id=meta.get("purchaser_id"),
            )
            updated += 1
        data = page.get("data") or []
        if not page.get("has_more") or not data:
            break
        after = data[-1].get("id")
    if updated:
        log.info("Stripe sync: refreshed %d server subscription(s).", updated)
    return updated


# ---- pricing / invite landing ---------------------------------------------- #

async def _h_invite(request):
    """Bot invite - subscribe first: only a signed-in user, only for a server
    that already has an active subscription (the bot owner can always add it).
    The bot also leaves unsubscribed servers on join, so copied links fail."""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.HTTPFound("/login")
    gid = (request.query.get("guild") or "").strip()
    if not is_owner(uid):
        if not gid.isdigit():
            return _aioweb.HTTPFound("/panel#servers")  # pick (and subscribe) a server first
        if not peek_active(gid):
            name = (_managed_server(uid, gid) or {}).get("name") or "This server"
            return _simple_page(
                "💳", "Subscribe first",
                f"<b>{html.escape(name)}</b> needs an active subscription before Gang Manager can be "
                "added. Just paid? This unlocks by itself in a few seconds.",
                _await_active_actions(gid, subscribe=True),
            )
    # Bot pool: a server that already has a pool bot gets that same bot again; a
    # server with none of our bots yet gets a free pool bot (when hand-outs are
    # on). Pool bots' own apps don't list our /callback, so their invite has no
    # redirect - the panel/success page waits for the bot to join instead.
    # The main bot is the distributor/hub only. Customers ALWAYS get a pool bot and
    # never the main bot; only the owner can still add the main bot (e.g. to the hub).
    if is_owner(uid):
        pool_bid = _pool_assignments().get(int(gid)) if gid.isdigit() else None
        if pool_bid:
            url = _invite_url(gid, client_id=str(pool_bid))
        else:
            url = _invite_url(gid if gid.isdigit() else None, redirect_uri=_redirect_uri(request))
    else:
        pool_bid = assign_bot_for_guild(gid) if gid.isdigit() else None
        if not pool_bid:
            return _simple_page(
                "⏳", "All bots are busy",
                "Every Gang Manager bot is at capacity right now. This usually clears within a "
                "minute - try again shortly, or open a ticket if it keeps happening.",
                "<a class='b p' href='/panel'>Back to the panel</a>", 503,
            )
        url = _invite_url(gid, client_id=str(pool_bid))
    if not url:
        return _simple_page(
            "⏳", "Almost ready", "The bot is still starting up - try again in a few seconds.",
            "<a class='b p' href='/panel'>Back to the panel</a>", 503,
        )
    return _aioweb.HTTPFound(url)


_LANDING_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Gang Manager - the all-in-one FiveM gang bot</title>
<meta name=description content="Roster, KOS, strikes, wars, tickets, recruitment and a full web control panel - one Discord bot for your FiveM gang.">
<!--GM_HEAD-->
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=JetBrains+Mono:wght@500;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
 --bg:#09090a;--bg2:#101013;--panel:#0d0d10;--panel2:#141418;
 --ink:#ece9e4;--mute:#908d87;--dim:#64615c;
 --line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.15);
 --acc:#2f6bff;--acc2:#5b93ff;--aglow:rgba(47,107,255,.55);--red:#e5362b;--red2:#ff5847;
 --grid:rgba(255,255,255,.035);
 --ok:#35c06b;--amber:#eaa12a;--blue:#5b8cff;--purple:#a78bfa;
 --disp:'Oswald',Inter,sans-serif;--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace;
 --r:3px}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.62;overflow-x:hidden;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
::selection{background:var(--acc);color:#fff}
/* ---- tactical backdrop: hairline grid + top ember glow + grain ---- */
.gridbg{position:fixed;inset:0;z-index:-3;pointer-events:none;
 background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
 background-size:48px 48px;background-position:center top;
 -webkit-mask:radial-gradient(130% 90% at 50% 0,#000 35%,transparent 78%);mask:radial-gradient(130% 90% at 50% 0,#000 35%,transparent 78%)}
.vign{position:fixed;inset:0;z-index:-2;pointer-events:none;
 background:radial-gradient(90% 60% at 50% -8%,rgba(47,107,255,.14),transparent 60%),radial-gradient(60% 50% at 50% 108%,rgba(47,107,255,.05),transparent 70%)}
.grain{position:fixed;inset:0;z-index:-1;opacity:.045;pointer-events:none;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.glow{position:fixed;left:0;top:0;width:520px;height:520px;margin:-260px 0 0 -260px;border-radius:50%;pointer-events:none;z-index:-1;
 background:radial-gradient(circle,rgba(47,107,255,.10),transparent 60%);opacity:0;transition:opacity .5s;will-change:transform}
/* ---- top status strip ---- */
.statusbar{display:flex;align-items:center;justify-content:space-between;gap:14px;
 padding:7px 22px;font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;
 color:var(--mute);border-bottom:1px solid var(--line);background:rgba(7,7,9,.7)}
.statusbar .sb-l{display:flex;align-items:center;gap:9px;color:var(--ink)}
.statusbar .sb-r{color:var(--dim)}
.livedot{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 0 var(--aglow);animation:ping 1.9s infinite}
@media(max-width:620px){.statusbar .sb-r{display:none}}
/* ---- nav ---- */
nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:14px;
 padding:13px 22px;backdrop-filter:blur(14px);background:rgba(7,7,9,.72);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:11px;font-family:var(--disp);font-weight:700;font-size:19px;letter-spacing:2px;text-transform:uppercase}
.brand img{width:38px;height:25px;object-fit:contain}
.navlinks{display:flex;gap:26px;align-items:center}
.navlinks a{color:var(--mute);font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:1px;text-transform:uppercase;transition:color .18s;position:relative}
.navlinks a:not(.btn):hover{color:var(--ink)}
.navlinks a:not(.btn)::after{content:"";position:absolute;left:0;right:100%;bottom:-6px;height:1px;background:var(--acc);transition:right .22s}
.navlinks a:not(.btn):hover::after{right:0}
.btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;padding:12px 22px;border-radius:var(--r);
 font-family:var(--mono);font-weight:700;font-size:12.5px;letter-spacing:1px;text-transform:uppercase;cursor:pointer;
 border:1px solid var(--line2);background:transparent;color:var(--ink);transition:transform .1s,border-color .2s,background .2s,box-shadow .2s;white-space:nowrap}
.btn:hover{transform:translateY(-2px);border-color:var(--ink)}
.btn.primary{background:linear-gradient(180deg,var(--acc2),var(--acc));color:#fff;border:1px solid var(--acc2);box-shadow:inset 0 1px 0 rgba(255,255,255,.22),0 12px 32px rgba(47,107,255,.34)}
.btn.primary:hover{filter:brightness(1.08);border-color:var(--acc2);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 18px 48px rgba(47,107,255,.5)}
.btn.sm{padding:9px 15px;font-size:11.5px}
.btn.disabled{opacity:.5;cursor:not-allowed;transform:none!important}
@media(max-width:720px){.navlinks .hidem{display:none}.navlinks{gap:16px}}
/* ---- hero ---- */
.hero{position:relative;text-align:center;padding:74px 22px 58px;max-width:960px;margin:0 auto}
.hero::before,.hero::after{content:"";position:absolute;width:26px;height:26px;opacity:.5}
.hero::before{left:10px;top:40px;border-left:2px solid var(--acc);border-top:2px solid var(--acc)}
.hero::after{right:10px;top:40px;border-right:2px solid var(--acc);border-top:2px solid var(--acc)}
.eyebrow{display:inline-flex;align-items:center;gap:9px;padding:6px 14px;border-radius:var(--r);border:1px solid var(--line2);
 background:rgba(47,107,255,.06);font-family:var(--mono);font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mute);margin-bottom:26px}
.eyebrow b{color:var(--acc2);font-weight:700}
.herologo{width:112px;height:70px;object-fit:contain;margin:0 auto 20px;display:block;filter:drop-shadow(0 0 30px rgba(47,107,255,.35))}
h1{font-family:var(--disp);font-size:clamp(44px,8.4vw,92px);line-height:.94;letter-spacing:0;font-weight:700;text-transform:uppercase}
h1 .g{color:var(--acc);position:relative;text-shadow:0 0 44px rgba(47,107,255,.45)}
.lead{color:var(--mute);font-size:clamp(16px,2.3vw,19px);max-width:600px;margin:22px auto 32px}
.cta{display:flex;gap:13px;justify-content:center;flex-wrap:wrap}
.trust{margin-top:22px;color:var(--dim);font-family:var(--mono);font-size:12px;letter-spacing:.5px}
/* ---- marquee ---- */
.marq{overflow:hidden;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:rgba(47,107,255,.03);margin:34px 0}
.marq .row{display:flex;gap:38px;white-space:nowrap;padding:12px 0;animation:scroll 26s linear infinite;width:max-content}
.marq span{color:var(--dim);font-family:var(--disp);font-weight:600;font-size:15px;letter-spacing:2px;text-transform:uppercase}
.marq span b{color:var(--ink);font-weight:600}
.marq span:not(b){color:var(--acc)}
@keyframes scroll{to{transform:translateX(-50%)}}
/* ---- sections ---- */
section{max-width:1080px;margin:0 auto;padding:62px 22px}
.kicker{text-align:center;color:var(--acc2);font-family:var(--mono);font-weight:600;letter-spacing:2.5px;text-transform:uppercase;font-size:11.5px;margin-bottom:12px}
.kicker::before{content:"// "}
h2{text-align:center;font-family:var(--disp);font-size:clamp(30px,5vw,52px);letter-spacing:.5px;font-weight:700;text-transform:uppercase;line-height:1.02;margin-bottom:14px}
.sub{text-align:center;color:var(--mute);max-width:570px;margin:0 auto 42px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(244px,1fr));gap:14px}
/* ---- feature cards ---- */
.feat{position:relative;padding:24px 22px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);
 transition:transform .2s,border-color .2s,box-shadow .2s;overflow:hidden}
.feat::before{content:"";position:absolute;left:0;top:0;height:2px;width:0;background:var(--acc);transition:width .28s}
.feat:hover{transform:translateY(-4px);border-color:var(--line2);box-shadow:0 18px 44px rgba(0,0,0,.5)}
.feat:hover::before{width:100%}
.feat .ic{font-size:26px;margin-bottom:12px;display:block;filter:grayscale(.2)}
.feat h3{font-family:var(--disp);font-weight:600;font-size:18px;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px}
.feat p{color:var(--mute);font-size:14px}
.feat::after{content:"";position:absolute;inset:0;background:radial-gradient(360px circle at var(--mx,50%) var(--my,0),rgba(47,107,255,.09),transparent 42%);opacity:0;transition:opacity .3s;pointer-events:none}
.feat:hover::after{opacity:1}
/* ---- interactive walkthrough ---- */
.walk{display:grid;grid-template-columns:234px 1fr;gap:18px;margin-top:34px;align-items:start}
.wrail{display:flex;flex-direction:column;gap:8px}
.wtab{display:flex;align-items:center;gap:12px;width:100%;text-align:left;padding:13px 14px;border-radius:var(--r);
 border:1px solid var(--line);background:var(--panel);color:var(--mute);cursor:pointer;font:inherit;transition:border-color .2s,background .2s,color .2s}
.wtab:hover{border-color:var(--line2);color:var(--ink)}
.wtab .wn{flex:0 0 30px;height:30px;border-radius:var(--r);display:grid;place-items:center;font-family:var(--mono);font-weight:700;font-size:13px;
 border:1px solid var(--line2);background:transparent;color:var(--mute);transition:.2s}
.wtab .wl{font-family:var(--disp);font-weight:600;font-size:15px;letter-spacing:.6px;text-transform:uppercase;white-space:nowrap}
.wtab.on{border-color:var(--acc);background:rgba(47,107,255,.08);color:var(--ink)}
.wtab.on .wn,.wtab.done .wn{background:var(--acc);color:#fff;border:none}
.wtab.done{color:var(--ink)}
.wstage{position:relative;border:1px solid var(--line);background:var(--panel);border-radius:var(--r);padding:30px 30px 20px;min-height:258px}
.wstage::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--acc);opacity:.7}
.wpanel{display:none;animation:wfade .4s ease}
.walk.js .wpanel.on{display:block}
.walk:not(.js) .wpanel{display:block;border-top:1px solid var(--line);margin-top:18px;padding-top:18px}
.walk:not(.js) .wpanel:first-of-type{border:none;margin:0;padding:0}
@keyframes wfade{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.wpanel .wic{font-size:32px;margin-bottom:12px}
.wpanel h3{font-family:var(--disp);font-weight:600;font-size:23px;letter-spacing:.6px;text-transform:uppercase;margin-bottom:10px}
.wpanel>p{color:var(--mute);font-size:15px;max-width:60ch}
.wtip{margin-top:16px;padding:12px 14px;border-radius:var(--r);border:1px solid var(--line);border-left:2px solid var(--acc);
 background:rgba(47,107,255,.04);color:var(--ink);font-size:13.5px}
.wtip b{color:var(--acc2)}
.wctl{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:22px;padding-top:18px;border-top:1px solid var(--line)}
.walk:not(.js) .wctl{display:none}
.wcount{color:var(--mute);font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:1px}
.wback[disabled]{opacity:.4;cursor:not-allowed;transform:none!important}
@media(max-width:760px){.walk{grid-template-columns:1fr}.wrail{flex-direction:row;overflow-x:auto;gap:8px;padding-bottom:4px;-webkit-overflow-scrolling:touch}.wtab{flex:0 0 auto}.wstage{padding:24px 20px 16px;min-height:0}}
/* ---- stat readouts ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:8px}
.statc{position:relative;text-align:left;padding:22px 18px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel);overflow:hidden}
.statc::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--acc);opacity:.8}
.statc b{display:block;font-family:var(--disp);font-size:38px;font-weight:700;color:var(--ink);line-height:1;font-variant-numeric:tabular-nums}
.statc span{color:var(--mute);font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:500;margin-top:7px;display:block}
/* ---- pricing ---- */
.price{max-width:432px;margin:0 auto;padding:34px 32px;border-radius:var(--r);border:1px solid var(--line2);
 background:linear-gradient(180deg,var(--panel2),var(--panel));box-shadow:0 30px 80px rgba(0,0,0,.55);position:relative;overflow:hidden}
.price::before{content:"";position:absolute;left:0;top:0;height:3px;width:100%;background:linear-gradient(90deg,var(--acc),transparent)}
.price::after{content:"";position:absolute;right:14px;top:14px;width:22px;height:22px;border-right:2px solid var(--acc);border-top:2px solid var(--acc);opacity:.6}
.pill{display:inline-block;padding:5px 12px;border-radius:var(--r);background:rgba(47,107,255,.12);color:var(--acc2);
 font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:16px}
.amount{font-family:var(--disp);font-size:60px;font-weight:700;line-height:1}.amount small{font-size:16px;color:var(--mute);font-weight:400;font-family:var(--mono)}
.price ul{list-style:none;margin:22px 0}
.price li{padding:10px 0;border-bottom:1px solid var(--line);font-size:14.5px;display:flex;gap:11px;align-items:center}
.price li:last-child{border:none}.price li .c{color:var(--acc2);font-weight:800}
/* ---- faq ---- */
.faq{max-width:760px;margin:0 auto}
details{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);margin-bottom:10px;overflow:hidden;transition:border-color .2s}
details[open]{border-color:var(--line2)}
summary{cursor:pointer;padding:17px 20px;font-family:var(--disp);font-weight:500;font-size:16px;letter-spacing:.4px;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:14px}
summary::-webkit-details-marker{display:none}
summary::after{content:"+";font-family:var(--mono);font-size:20px;color:var(--acc);transition:transform .2s}
details[open] summary::after{transform:rotate(45deg)}
details p{padding:0 20px 18px;color:var(--mute);font-size:14.5px}
/* ---- reveal ---- */
.rev{opacity:0;transform:translateY(24px);transition:opacity .7s cubic-bezier(.2,.7,.2,1),transform .7s cubic-bezier(.2,.7,.2,1)}
.rev.in{opacity:1;transform:none}
/* ---- footer + big cta ---- */
footer{text-align:center;padding:38px 22px;color:var(--dim);font-family:var(--mono);font-size:12px;letter-spacing:.5px;border-top:1px solid var(--line);margin-top:40px}
footer a{color:var(--mute)}footer a:hover{color:var(--acc2)}
.bignote{position:relative;text-align:center;max-width:700px;margin:0 auto;padding:44px 30px;border-radius:var(--r);border:1px solid var(--line2);
 background:linear-gradient(130deg,rgba(47,107,255,.07),var(--panel) 55%);overflow:hidden}
.bignote::before,.bignote::after{content:"";position:absolute;width:26px;height:26px;opacity:.6}
.bignote::before{left:14px;bottom:14px;border-left:2px solid var(--acc);border-bottom:2px solid var(--acc)}
.bignote::after{right:14px;bottom:14px;border-right:2px solid var(--acc);border-bottom:2px solid var(--acc)}
/* ---- product mockup: tactical reskin of the real panel ---- */
.mockwrap{max-width:1020px;margin:6px auto 0;padding:0 22px;perspective:1600px}
.mock{border-radius:var(--r);border:1px solid var(--line2);background:linear-gradient(180deg,var(--panel2),var(--panel));
 box-shadow:0 50px 110px rgba(0,0,0,.62),0 0 70px rgba(47,107,255,.06);overflow:hidden;transform:rotateX(13deg) scale(.97);
 transform-origin:50% 0;transition:transform .9s cubic-bezier(.2,.7,.2,1);will-change:transform}
.mock.up{transform:none}
.mbar{display:flex;align-items:center;gap:7px;padding:11px 14px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
.mbar i{width:10px;height:10px;border-radius:50%;background:#2c2c32}
.mbar i:nth-child(1){background:var(--acc)}.mbar i:nth-child(2){background:#3a3a42}.mbar i:nth-child(3){background:#3a3a42}
.mbar span{margin-left:12px;font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);padding:4px 12px;border-radius:var(--r);background:rgba(255,255,255,.03)}
.mphead{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(47,107,255,.05),transparent)}
.mphead img{width:38px;height:25px;object-fit:contain;filter:drop-shadow(0 0 12px rgba(47,107,255,.35))}
.mpbrand{font-family:var(--disp);font-weight:700;font-size:18px;letter-spacing:2px;text-transform:uppercase;line-height:1;color:var(--ink)}
.mpbrand small{display:block;font-family:var(--mono);font-weight:500;font-size:10px;letter-spacing:1px;color:var(--dim);margin-top:4px;text-transform:uppercase}
.mpchip{margin-left:auto;display:flex;align-items:center;gap:8px;padding:5px 12px 5px 6px;border-radius:var(--r);border:1px solid var(--line);background:rgba(255,255,255,.03);font-size:12.5px;font-weight:600;color:var(--ink)}
.mpchip .pa{width:22px;height:22px;border-radius:50%;background:var(--acc);display:grid;place-items:center;color:#fff;font-weight:800;font-size:11px}
.mpchip em{font-style:normal;font-family:var(--mono);font-size:9.5px;font-weight:700;color:var(--acc2);padding:2px 7px;border-radius:var(--r);background:rgba(47,107,255,.12);text-transform:uppercase;letter-spacing:.6px}
.mpbody{padding:16px 18px 20px;text-align:left}
.mpsrv{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.mpsrv .l{font-family:var(--mono);font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--dim);font-weight:500}
.mpsel{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:var(--r);border:1px solid var(--line);background:rgba(0,0,0,.3);font-size:13px;font-weight:600;color:var(--ink)}
.mpsel i{font-style:normal;color:var(--dim)}
.mpstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:10px;margin-bottom:14px}
.mpstat .n.rf{font-size:17px}
.mptoolbar{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.mpsearch{flex:1;padding:9px 13px;border-radius:var(--r);border:1px solid var(--line);background:rgba(0,0,0,.3);color:var(--dim);font-size:13px}
.mprefresh{padding:9px 13px;border-radius:var(--r);border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--ink);font-size:13px;font-weight:600;white-space:nowrap}
.mpcount{color:var(--dim);font-family:var(--mono);font-size:11.5px;font-weight:500;white-space:nowrap}
.mpfoot{margin-top:16px;padding-top:13px;border-top:1px solid var(--line);font-family:var(--mono);font-size:11px;letter-spacing:.5px;color:var(--dim);text-align:center}
.mpstat{position:relative;overflow:hidden;padding:13px 14px 12px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel2);box-shadow:0 8px 24px rgba(0,0,0,.4)}
.mpstat::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--acc);opacity:.9}
.mpstat.bad::after{background:var(--red)}.mpstat.blue::after{background:var(--blue)}.mpstat.warn::after{background:var(--amber)}.mpstat.purple::after{background:var(--purple)}.mpstat.ok::after{background:var(--ok)}
.mpstat .ic{position:absolute;top:10px;right:11px;font-size:13px;opacity:.5}
.mpstat .n{font-family:var(--disp);font-size:25px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums;color:var(--ink)}
.mpstat.bad .n{color:var(--red2)}.mpstat.blue .n{color:#93c5fd}.mpstat.warn .n{color:#ffcf6b}.mpstat.purple .n{color:#c4b5fd}.mpstat.ok .n{color:#86efac}
.mpstat .l{font-family:var(--mono);font-size:9.5px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;margin-top:6px;font-weight:500}
.mptabs{display:flex;gap:4px;padding:4px;border-radius:var(--r);background:rgba(0,0,0,.3);border:1px solid var(--line);margin-bottom:14px;overflow-x:auto}
.mptab{flex:0 0 auto;padding:7px 14px;border-radius:var(--r);cursor:pointer;color:var(--mute);font-weight:600;font-size:13px;white-space:nowrap;transition:.18s;border:none;background:none;font-family:inherit}
.mptab:hover{color:var(--ink)}
.mptab.on{background:var(--acc);color:#fff;box-shadow:0 4px 14px rgba(47,107,255,.4)}
.mpview{display:none;animation:wfade .35s ease}
.mpview.on{display:block}
.mprank{display:flex;align-items:center;gap:9px;margin:4px 2px 9px;font-family:var(--mono);font-weight:700;font-size:10.5px;color:var(--mute);text-transform:uppercase;letter-spacing:1.2px}
.mprank::before{content:"";width:3px;height:12px;border-radius:2px;background:var(--acc)}
.mprank .pill{margin-left:auto;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:var(--r);padding:1px 9px;font-size:10px;color:var(--mute);letter-spacing:.5px}
.mpmembers{list-style:none;display:flex;flex-direction:column;gap:8px;margin:0 0 14px;padding:0}
.mpmem{display:flex;align-items:center;gap:12px;padding:9px 13px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel2);box-shadow:0 6px 20px rgba(0,0,0,.3)}
.mpav{width:28px;height:28px;border-radius:var(--r);display:grid;place-items:center;font-size:12px;font-weight:800;color:#09090c;flex:none}
.mpmem b{flex:1;font-weight:600;font-size:14px}
.mpmem em{font-style:normal;font-family:var(--mono);font-size:10px;font-weight:700;padding:3px 9px;border-radius:var(--r);background:rgba(255,255,255,.06);color:var(--mute);text-transform:uppercase;letter-spacing:.5px}
.mpmem em.r1{background:var(--acc);color:#fff}
.mpwar{display:flex;align-items:center;gap:16px;padding:16px 18px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel2);box-shadow:0 6px 20px rgba(0,0,0,.3)}
.mpwar .wl{flex:1}
.mpwar .wt{font-family:var(--mono);font-size:10.5px;color:var(--mute);text-transform:uppercase;letter-spacing:1px;margin-bottom:9px;display:flex;align-items:center;gap:7px}
.mpscore{display:flex;align-items:baseline;gap:9px;font-family:var(--disp);font-size:42px;font-weight:700;line-height:1;margin-bottom:11px}
.mpscore .dim{color:var(--dim)}.mpscore span{font-size:24px;color:var(--dim)}
.mpbar{height:7px;border-radius:4px;background:rgba(229,54,43,.3);overflow:hidden;max-width:230px}
.mpbar i{display:block;height:100%;width:62%;background:var(--ok);border-radius:4px}
.mprounds{margin-top:11px;font-size:14px;letter-spacing:3px}
.mplive{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(53,192,107,.6);animation:ping 1.8s infinite}
.mpbadge{font-family:var(--mono);font-size:9.5px;font-weight:700;padding:3px 10px;border-radius:var(--r);text-transform:uppercase;letter-spacing:.8px;background:rgba(53,192,107,.14);color:#86efac;border:1px solid rgba(53,192,107,.4)}
.mpitem{display:flex;align-items:center;gap:12px;padding:11px 14px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel2);box-shadow:0 6px 20px rgba(0,0,0,.3);margin-bottom:9px;font-size:14px}
.mpitem b{flex:1;font-weight:600}
.mpitem .tag{font-family:var(--mono);font-size:10px;font-weight:700;padding:3px 9px;border-radius:var(--r);white-space:nowrap;letter-spacing:.5px}
.tag.red{background:rgba(229,54,43,.16);color:#ff8a80;border:1px solid rgba(229,54,43,.4)}
.tag.grn{background:rgba(53,192,107,.14);color:#86efac;border:1px solid rgba(53,192,107,.35)}
.tag.gry{background:rgba(255,255,255,.05);color:var(--mute);border:1px solid var(--line)}
.tag.amb{background:rgba(234,161,42,.14);color:#fcd34d;border:1px solid rgba(234,161,42,.35)}
.mpset{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:var(--r);border:1px solid var(--line);background:var(--panel2);margin-bottom:9px}
.mpset .sk{flex:1;font-size:13.5px;color:var(--ink);font-weight:600}
.mpdrop{display:flex;align-items:center;gap:8px;padding:7px 12px;border-radius:var(--r);border:1px solid var(--line);background:rgba(0,0,0,.3);font-size:13px;color:var(--ink);min-width:150px}
.mpdrop::after{content:"▾";margin-left:auto;color:var(--dim)}
.mpcap{font-size:12px;color:var(--mute);margin:0 2px 11px;line-height:1.5}
.mpitem .num{font-family:var(--mono);font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink);font-size:14px}
@keyframes ping{0%{box-shadow:0 0 0 0 var(--aglow)}80%,100%{box-shadow:0 0 0 9px rgba(47,107,255,0)}}
@media(max-width:760px){.mpstats{grid-template-columns:repeat(2,1fr)}.mock,.mock.up{transform:none}.mphead{padding:12px 14px}.mpbody{padding:14px}.mpchip em{display:none}.mpcount{display:none}}
/* ---- Discord showcase (kept Discord-accurate on purpose) ---- */
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px;align-items:start}
.dmsg{display:flex;gap:12px;padding:16px;border-radius:8px;background:#2b2d31;border:1px solid rgba(255,255,255,.06);
 box-shadow:0 24px 60px rgba(0,0,0,.45);font-family:"gg sans","Noto Sans",Inter,-apple-system,sans-serif;transition:transform .25s,box-shadow .25s}
.dmsg:hover{transform:translateY(-5px);box-shadow:0 34px 80px rgba(0,0,0,.55)}
.dav{width:40px;height:40px;border-radius:50%;background:#111214;display:grid;place-items:center;flex:none;overflow:hidden}
.dav img{width:34px;height:22px;object-fit:contain}
.dcol{flex:1;min-width:0}
.dname{font-weight:700;font-size:15px;color:#f2f3f5;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.dbot{font-size:10px;font-weight:700;background:#5865f2;color:#fff;padding:1px 5px;border-radius:4px;letter-spacing:.2px}
.dname time{font-weight:500;font-size:12px;color:#949ba4}
.vcard{position:relative;margin-top:8px;background:#232428;border:1px solid rgba(255,255,255,.05);border-radius:8px;padding:14px 16px 12px 18px;color:#dbdee1;font-size:14px;line-height:1.5;overflow:hidden}
.vcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--c,#e5362b)}
.vhead{display:flex;gap:12px;align-items:flex-start}
.vtitle{flex:1;min-width:0}
.vtitle b{display:block;color:#f2f3f5;font-size:16px;font-weight:800;letter-spacing:.2px}
.vtitle small{display:block;color:#b5bac1;font-size:13px;margin-top:3px;line-height:1.45}
.vthumb{width:46px;height:46px;border-radius:12px;flex:none;object-fit:cover;background:#111214;border:1px solid rgba(255,255,255,.08)}
.vquote{margin:11px 0 2px;border-left:3px solid rgba(255,255,255,.14);padding:2px 0 2px 12px;display:grid;gap:5px}
.vquote div{font-size:13.5px;color:#dbdee1}
.vquote b{color:#f2f3f5;font-weight:700}
.vquote .sep{color:#80848e;margin:0 5px}
.vfull{margin:10px 0 2px}
.vfull i{display:block;font-style:normal;font-weight:800;color:#f2f3f5;font-size:12.5px;margin-bottom:3px}
.vdiv{height:1px;background:rgba(255,255,255,.08);margin:12px 0}
.vfoot{color:#8a8f98;font-size:12px;margin-top:11px}
.vfoot b{color:#aeb3bd;font-weight:700}
.dscore{font-size:36px;font-weight:900;color:#fff;letter-spacing:1px;margin:4px 0}.dscore span{color:#80848e;font-size:24px}.dscore .dim{color:#80848e}
.dbar{height:8px;border-radius:5px;background:rgba(237,66,69,.5);overflow:hidden;margin:8px 0 6px}.dbar i{display:block;height:100%;background:#23a55a}
.dsmall{font-size:12.5px;color:#b5bac1}
.drounds{margin-top:8px;letter-spacing:2px;font-size:15px}
.dlist{display:grid;gap:7px;margin:4px 0 2px}
.dlist div{display:flex;align-items:center;gap:9px;font-size:13.5px;color:#dbdee1}
.dlist .rk{margin-left:auto;color:#aeb3bd;font-size:12.5px;font-weight:600}
.dlist .pip{width:9px;height:9px;border-radius:50%;background:var(--c,#e5362b);flex:none}
.dbtns{display:flex;flex-wrap:wrap;gap:8px}
.dbtn{font-size:13px;font-weight:600;padding:7px 13px;border-radius:5px;background:#4e5058;color:#fff;line-height:1}
.dbtn.g{background:#248046}.dbtn.r{background:#da373c}.dbtn.b{background:#5865f2}
.dvote{height:8px;border-radius:5px;background:rgba(237,66,69,.5);overflow:hidden;margin:9px 0 4px}.dvote i{display:block;height:100%;width:87%;background:#23a55a}
@media(prefers-reduced-motion:reduce){.aurora i,.marq .row,.livedot,.mplive{animation:none}.mock{transition:none}.rev{transition:none;opacity:1;transform:none}}
</style></head><body>
<div class=gridbg></div><div class=vign></div><div class=grain></div><div class=glow id=glow></div>
<div class=statusbar><span class=sb-l><i class=livedot></i> Systems online</span><span class=sb-r>24/7 hosted · 8 modules · 2-min setup</span></div>

<nav>
  <a class=brand href="/"><img src="/gm-logo.png" alt="GM">GANG MANAGER</a>
  <div class=navlinks>
    <a href="#features" class=hidem>Features</a>
    <a href="#discord" class=hidem>In Discord</a>
    <a href="#how" class=hidem>How it works</a>
    <a href="#pricing" class=hidem>Pricing</a>
    <a id="navCta" class="btn sm primary" href="/login">Open panel</a>
  </div>
</nav>

<header class=hero>
  <div class=eyebrow>✦ <b>Live</b> · all-in-one FiveM gang toolkit</div>
  <img class=herologo src="/gm-logo.png" alt="Gang Manager">
  <h1>Run your gang<br><span class=g>like a machine.</span></h1>
  <p class=lead>Roster, KOS, strikes, wars, tickets and recruitment - one Discord bot with a full web control panel. Add it, subscribe, and command everything from the browser.</p>
  <div class=cta>
    <a id="heroCta" class="btn primary" href="/login">Get started</a>
    <a class="btn" href="#pricing">See pricing</a>
  </div>
  <div class=trust id="heroStatus">Log in with Discord · $7.99 / month · 1 server per subscription</div>
</header>

<div class=mockwrap>
  <div class=mock id=mock>
    <div class=mbar><i></i><i></i><i></i><span>gang manager · admin panel</span></div>
    <div class=mphead>
      <img src="/gm-logo.png" alt="">
      <div class=mpbrand>GANG MANAGER<small>Control Panel</small></div>
      <div class=mpchip><span class=pa>S</span>Shadow<em>Owner</em></div>
    </div>
    <div class=mpbody>
      <div class=mpsrv><span class=l>Server</span><span class=mpsel>Los Santos Mafia<i>▾</i></span></div>
      <div class=mpstats>
        <div class=mpstat><span class=ic>🎯</span><div class=n data-n="48">0</div><div class=l>Ranked</div></div>
        <div class="mpstat bad"><span class=ic>💀</span><div class=n data-n="7">0</div><div class=l>On KOS</div></div>
        <div class="mpstat warn"><span class=ic>📨</span><div class=n data-n="3">0</div><div class=l>Pending EOIs</div></div>
        <div class="mpstat warn"><span class=ic>⚠️</span><div class=n data-n="2">0</div><div class=l>Strikes 30d</div></div>
        <div class="mpstat blue"><span class=ic>⚔️</span><div class=n data-n="1">0</div><div class=l>Wars</div></div>
        <div class="mpstat ok"><span class=ic>👥</span><div class=n data-n="52">0</div><div class=l>In server</div></div>
        <div class="mpstat purple"><span class=ic>📻</span><div class="n rf">112.5</div><div class=l>Radio</div></div>
      </div>
      <div class=mptabs role=tablist>
        <button type=button class="mptab on" data-v=roster>👥 Roster</button>
        <button type=button class=mptab data-v=kos>💀 KOS</button>
        <button type=button class=mptab data-v=strikes>⚠️ Strikes</button>
        <button type=button class=mptab data-v=stash>📦 Stash</button>
        <button type=button class=mptab data-v=wars>⚔️ Wars</button>
        <button type=button class=mptab data-v=eois>📨 EOIs</button>
        <button type=button class=mptab data-v=orders>🎫 Orders</button>
        <button type=button class=mptab data-v=settings>⚙️ Settings</button>
      </div>
      <div class="mpview on" data-v=roster>
        <div class=mpcap>Built from your rank roles - grouped, ranked and always current.</div>
        <div class=mptoolbar><span class=mpsearch>Search members…</span><span class=mprefresh>↻ Refresh</span><span class=mpcount>48 members</span></div>
        <div class=mprank>Boss<span class=pill>1</span></div>
        <ul class=mpmembers><li class=mpmem><span class=mpav style="background:#d9d9e3">S</span><b>Shadow</b><em class=r1>Boss</em></li></ul>
        <div class=mprank>Underboss<span class=pill>1</span></div>
        <ul class=mpmembers><li class=mpmem><span class=mpav style="background:#c4cfe8">V</span><b>Vex</b><em>Underboss</em></li></ul>
        <div class=mprank>Enforcer<span class=pill>2</span></div>
        <ul class=mpmembers>
          <li class=mpmem><span class=mpav style="background:#e3d2c4">R</span><b>Riot</b><em>Enforcer</em></li>
          <li class=mpmem><span class=mpav style="background:#cde3cf">N</span><b>Nova</b><em>Enforcer</em></li>
        </ul>
      </div>
      <div class=mpview data-v=kos>
        <div class=mpcap>Your <b>kill-on-sight hit list</b> - who to drop on sight. Sign-up → random draw → staff approve → announce.</div>
        <div class=mpitem><span class=mpav style="background:#e9b7b7">D</span><b>Dmitri "Ghost" Kane</b><span class="tag red">● ACTIVE</span></div>
        <div class=mpitem><span class=mpav style="background:#e9c7b7">M</span><b>Marco Vitelli</b><span class="tag red">● ACTIVE</span></div>
        <div class=mpitem><span class=mpav style="background:#c7c7d6">T</span><b>Tenpenny crew</b><span class="tag amb">DRAW OPEN</span></div>
        <div class=mpitem><span class=mpav style="background:#bcd6c0">L</span><b>Luca Moreno</b><span class="tag gry">CLEARED</span></div>
      </div>
      <div class=mpview data-v=strikes>
        <div class=mpcap>Issue and track warnings - auto-action at the limit, a DM on every change.</div>
        <div class=mpitem><span class=mpav style="background:#e3d2c4">R</span><b>Riot · griefing civilians</b><span class="tag amb">2 / 3</span></div>
        <div class=mpitem><span class=mpav style="background:#cdd7e3">P</span><b>Pyro · no-show at war</b><span class="tag gry">1 / 3</span></div>
        <div class=mpitem><span class=mpav style="background:#e9b7b7">G</span><b>Ghost · friendly fire</b><span class="tag red">AT LIMIT · 3/3</span></div>
      </div>
      <div class=mpview data-v=stash>
        <div class=mpcap>Shared gang stash - cash, weapons and materials, every change logged.</div>
        <div class=mpitem><b>💵 Cash</b><span class=num>$482,500</span></div>
        <div class=mpitem><b>🔫 Weapons</b><span class=num>37</span></div>
        <div class=mpitem><b>💊 Product</b><span class=num>12 kg</span></div>
        <div class=mpitem><b>🎒 Materials</b><span class=num>1,240</span></div>
      </div>
      <div class=mpview data-v=wars>
        <div class=mpcap>A <b>scored gang-vs-gang fight</b> - not a list, a match. Live score, round-by-round rolls, first-to targets.</div>
        <div class=mpwar><div class=wl>
          <div class=wt><span class=mplive></span>War · vs Night Riders <span class=mpbadge>Live</span></div>
          <div class=mpscore><b>5</b><span>:</span><b class=dim>3</b></div>
          <div class=mpbar><i></i></div>
          <div class=mprounds>🟩🟩🟥🟩🟥🟩🟩🟥</div>
        </div></div>
      </div>
      <div class=mpview data-v=eois>
        <div class=mpcap>Recruitment applications with ✅ / ❌ voting, vouches and automatic closing.</div>
        <div class=mpitem><span class=mpav style="background:#d9d9e3">K</span><b>Kilo - wants in</b><span class="tag grn">✓ 6</span><span class="tag red">✗ 1</span></div>
        <div class=mpitem><span class=mpav style="background:#cfcfe0">Z</span><b>Zane - referred by Vex</b><span class="tag amb">PENDING</span></div>
      </div>
      <div class=mpview data-v=orders>
        <div class=mpcap>Discord tickets for jobs &amp; buys - paid/unpaid, transcripts, auto-close.</div>
        <div class=mpitem><b>#1042 · Weapon crate</b><span class="tag grn">OPEN</span></div>
        <div class=mpitem><b>#1041 · Protection - Vinewood</b><span class="tag grn">OPEN</span></div>
        <div class=mpitem><b>#1038 · Vehicle respray</b><span class="tag gry">CLOSED</span></div>
      </div>
      <div class=mpview data-v=settings>
        <div class=mpcap>Point the bot at your roles and channels - all from dropdowns.</div>
        <div class=mpset><span class=sk>Rank roles</span><span class=mpdrop>@Boss, @Underboss…</span></div>
        <div class=mpset><span class=sk>Log channel</span><span class=mpdrop>#gang-logs</span></div>
        <div class=mpset><span class=sk>KOS channel</span><span class=mpdrop>#kos-board</span></div>
      </div>
      <div class=mpfoot>Only the owner &amp; members they add can open this · changes apply to Discord live</div>
    </div>
  </div>
</div>

<div class=marq><div class=row id="marqRow"></div></div>

<section id=features>
  <div class="kicker rev">Everything in one bot</div>
  <h2 class=rev>Eight toolkits. Zero clutter.</h2>
  <p class="sub rev">Each system is built for FiveM gangs and wired into Discord - and all of it is manageable from the web panel.</p>
  <div class=grid id="featGrid"></div>
</section>

<section id=discord>
  <div class="kicker rev">Right inside Discord</div>
  <h2 class=rev>Looks this good in your server.</h2>
  <p class="sub rev">The real cards your members use - command center, roster, war scoreboards, orders, KOS draws and applications, each one updating itself.</p>
  <div class=dgrid>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:41 PM</time></div>
        <div class=vcard style="--c:#c9c9d2">
          <div class=vhead>
            <div class=vtitle><b>👑 Black Lotus - Command Center</b><small>Your whole gang at a glance.</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=vquote>
            <div><b>Members</b><span class=sep>·</span>48</div>
            <div><b>On KOS</b><span class=sep>·</span>7</div>
            <div><b>Open orders</b><span class=sep>·</span>12</div>
            <div><b>Active wars</b><span class=sep>·</span>1</div>
          </div>
          <div class=vdiv></div>
          <div class=dbtns><span class=dbtn>📊 Refresh</span><span class=dbtn>👤 My profile</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · just now</div>
        </div>
      </div>
    </div>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:44 PM</time></div>
        <div class=vcard style="--c:#23a55a">
          <div class=vhead>
            <div class=vtitle><b>⚔️ War · Black Lotus vs Night Riders</b><small>First to 7 · live scoreboard</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=dscore>5 <span>-</span> <span class=dim>3</span></div>
          <div class=dbar><i style="width:62%"></i></div>
          <div class=dsmall>Win rate 62% · 8 rounds played</div>
          <div class=drounds>🟩🟩🟥🟩🟥🟩🟩🟥</div>
          <div class=vdiv></div>
          <div class=dbtns><span class="dbtn g">🏆 Round won</span><span class="dbtn r">💀 Round lost</span><span class=dbtn>↩️ Undo</span><span class=dbtn>🏁 End war</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · 1 minute ago</div>
        </div>
      </div>
    </div>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:46 PM</time></div>
        <div class=vcard style="--c:#f0b232">
          <div class=vhead>
            <div class=vtitle><b>🎫 P90 Order · Order #42</b><small>Thanks for your order, @Vex - staff are on it.</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=vquote>
            <div><b>Customer</b><span class=sep>·</span>@Vex</div>
            <div><b>Status</b><span class=sep>·</span>🔴 Unpaid</div>
            <div><b>Priority</b><span class=sep>·</span>🟠 High</div>
            <div><b>Handled by</b><span class=sep>·</span>@Shadow</div>
          </div>
          <div class=vfull><i>📦 Items</i>P90 × 4</div>
          <div class=vdiv></div>
          <div class=dbtns><span class=dbtn>🙋 Claim</span><span class="dbtn g">💰 Mark paid</span><span class=dbtn>🚩 Priority</span><span class="dbtn r">🔒 Close</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · 2 minutes ago</div>
        </div>
      </div>
    </div>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:48 PM</time></div>
        <div class=vcard style="--c:#c9c9d2">
          <div class=vhead>
            <div class=vtitle><b>👥 Roster · Black Lotus</b><small>Built live from your rank roles · 48 members</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=dlist>
            <div><span class=pip></span><b>Shadow</b><span class=rk>Boss</span></div>
            <div><span class=pip></span><b>Vex</b><span class=rk>Underboss</span></div>
            <div><span class=pip></span><b>Riot</b><span class=rk>Enforcer</span></div>
            <div><span class=pip></span><b>Nova</b><span class=rk>Enforcer · +44 more</span></div>
          </div>
          <div class=vdiv></div>
          <div class=dbtns><span class=dbtn>🔄 Refresh roster</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · just now</div>
        </div>
      </div>
    </div>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:50 PM</time></div>
        <div class=vcard style="--c:#da373c">
          <div class=vhead>
            <div class=vtitle><b>💀 KOS · Tonight's draw</b><small>Signed up → randomly drawn → staff approved.</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=vquote>
            <div><b>Signed up</b><span class=sep>·</span>9 members</div>
            <div><b>Drawn</b><span class=sep>·</span>@Vex · @Riot · @Nova</div>
            <div><b>Status</b><span class=sep>·</span>🟢 Approved by @Shadow</div>
          </div>
          <div class=vdiv></div>
          <div class=dbtns><span class="dbtn r">🎯 Sign up</span><span class=dbtn>🎲 Re-draw</span><span class="dbtn g">📣 Announce</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · 3 minutes ago</div>
        </div>
      </div>
    </div>

    <div class="dmsg rev">
      <div class=dav><img src="/gm-logo.png" alt=""></div>
      <div class=dcol>
        <div class=dname>Gang Manager <span class=dbot>APP</span><time>Today at 9:52 PM</time></div>
        <div class=vcard style="--c:#5865f2">
          <div class=vhead>
            <div class=vtitle><b>📨 New application · Riot</b><small>Community vote · closes in 24 hours</small></div>
            <img class=vthumb src="/gm-icon.png" alt="">
          </div>
          <div class=vquote>
            <div><b>Role</b><span class=sep>·</span>Enforcer</div>
            <div><b>Active</b><span class=sep>·</span>Daily</div>
            <div><b>Vouched by</b><span class=sep>·</span>@Shadow</div>
          </div>
          <div class=dvote><i></i></div>
          <div class=dsmall>✅ 7 · ❌ 1</div>
          <div class=vdiv></div>
          <div class=dbtns><span class="dbtn g">✅ Yes · 7</span><span class="dbtn r">❌ No · 1</span></div>
          <div class=vdiv></div>
          <div class=vfoot>✦ <b>Gang Manager</b> · made by pmxl · 1 minute ago</div>
        </div>
      </div>
    </div>

  </div>
</section>

<section id=how>
  <div class="kicker rev">Live in two minutes</div>
  <h2 class=rev>How it works</h2>
  <p class="sub rev">Four steps from zero to running - here's exactly what you'll do.</p>

  <div class="walk rev" id="walk" tabindex="0">
    <div class="wrail" id="wrail">
      <button type="button" class="wtab" data-i="0"><span class="wn">1</span><span class="wl">Log in</span></button>
      <button type="button" class="wtab" data-i="1"><span class="wn">2</span><span class="wl">Subscribe</span></button>
      <button type="button" class="wtab" data-i="2"><span class="wn">3</span><span class="wl">Add the bot</span></button>
      <button type="button" class="wtab" data-i="3"><span class="wn">4</span><span class="wl">Set it up</span></button>
    </div>
    <div class="wstage">
      <div class="wpanel" data-i="0">
        <div class="wic">🔑</div>
        <h3>Log in with Discord</h3>
        <p>Hit <b>Get started</b> and authorise with Discord - the same account you already use. No password, no form to fill in. You land straight on your panel with every server you can manage listed out.</p>
        <div class="wtip">What you'll see → each of your servers, each with a button to <b>subscribe</b> or <b>add the bot</b>.</div>
      </div>
      <div class="wpanel" data-i="1">
        <div class="wic">💳</div>
        <h3>Subscribe your server</h3>
        <p>Pick the server you want Gang Manager in and start the subscription - <b>$7.99 a month</b>, handled by Stripe. It covers that one server and unlocks every module. Cancel anytime, no lock-in.</p>
        <div class="wtip">Running two servers? → start a second subscription, or move this one over for free whenever you like.</div>
      </div>
      <div class="wpanel" data-i="2">
        <div class="wic">➕</div>
        <h3>Add the bot</h3>
        <p>One click invites Gang Manager, already fully unlocked. It's hosted 24/7 on our side - nothing to run, host or keep online yourself. Give it a few seconds and it's in your server.</p>
        <div class="wtip">Nothing to install → it's live the moment it joins.</div>
      </div>
      <div class="wpanel" data-i="3">
        <div class="wic">⚙️</div>
        <h3>Set your channels &amp; roles</h3>
        <p>Back in the panel, point Gang Manager at your server from simple dropdowns - your rank roles, your log channel, your KOS and recruitment channels. The roster builds itself from the rank roles you pick. Save, and you're live.</p>
        <div class="wtip">That's it → members show up on the live roster, KOS sign-ups open and EOI voting runs.</div>
      </div>
      <div class="wctl">
        <button type="button" class="btn sm wback" id="wback">◀ Back</button>
        <span class="wcount" id="wcount">Step 1 of 4</span>
        <button type="button" class="btn sm primary" id="wnext">Next ▶</button>
      </div>
    </div>
  </div>
</section>

<section>
  <div class=stats>
    <div class="statc rev"><b data-n="8">0</b><span>Modules</span></div>
    <div class="statc rev"><b data-n="50" data-suf="+">0</b><span>Commands</span></div>
    <div class="statc rev"><b>2 min</b><span>Setup</span></div>
    <div class="statc rev"><b>24/7</b><span>Hosted</span></div>
  </div>
</section>

<section id=pricing>
  <div class="kicker rev">Simple pricing</div>
  <h2 class=rev>One plan. Everything included.</h2>
  <p class="sub rev">One subscription covers one Discord server. Need the bot in another server? Add another subscription.</p>
  <div class="price rev">
    <span class=pill>MONTHLY</span>
    <div class=amount>$7.99<small> / month</small></div>
    <ul>
      <li><span class=c>✓</span> Full roster, KOS, strikes &amp; wars</li>
      <li><span class=c>✓</span> Ticket &amp; order system with transcripts</li>
      <li><span class=c>✓</span> EOI recruitment with voting</li>
      <li><span class=c>✓</span> Web control panel &amp; live logs</li>
      <li><span class=c>✓</span> 1 Discord server · cancel anytime</li>
    </ul>
    <a id="buyBtn" class="btn primary" style="width:100%" href="/login">Log in to start</a>
  </div>
</section>

<section id=faq>
  <div class="kicker rev">Questions</div>
  <h2 class=rev>Good to know</h2>
  <div class="faq rev" style="margin-top:28px">
    <details><summary>Can I move my subscription to another server?</summary><p>Yes - for free. In the panel, pick the new server and choose <b>Move my subscription here</b>; it switches over and the old server turns off. Each subscription covers one server at a time, so to run two servers at once you'd start a second subscription.</p></details>
    <details><summary>How much does it cost?</summary><p>$7.99 a month per Discord server. Subscribe for your server, add the bot, and everything unlocks. Cancel anytime.</p></details>
    <details><summary>Do I need to host anything?</summary><p>No. Gang Manager is fully hosted and online 24/7. Add it from this site and it just works.</p></details>
    <details><summary>Can I cancel?</summary><p>Anytime. Your access stays active until the end of the period you've paid for.</p></details>
  </div>
</section>

<section>
  <div class="bignote rev">
    <h2 style="margin-bottom:10px">Ready to take over the city?</h2>
    <p class=sub style="margin-bottom:24px">Add Gang Manager and subscribe to unlock everything.</p>
    <div class=cta>
      <a id="panelBtn" class="btn primary" href="/login">Open the panel</a>
    </div>
  </div>
</section>

<footer>
  Gang Manager · <a href="/terms">Terms</a> · <a href="/privacy">Privacy</a> · <a href="/panel">Panel</a> · made by pmxl
</footer>

<script>
var FEATURES=[
 ["🎯","Live Roster","Built straight from your rank roles - grouped, ranked and always current."],
 ["💀","KOS List","Sign-up → random draw → staff approve → announce, all in one flow."],
 ["⚠️","Strikes","Issue, track and auto-warn at the limit. Members get a DM on every change."],
 ["⚔️","War Scoreboards","Live score, win-rate bar, round-by-round rolls and first-to targets."],
 ["🎫","Tickets & Orders","Real Discord tickets - paid/unpaid, transcripts and auto-close."],
 ["📨","EOI Recruitment","Applications with ✅/❌ voting, vouches and automatic closing."],
 ["🌐","Web Control Panel","Run the whole gang from your browser. Every change applies live."],
 ["📝","Detailed Logs","Every action routed to its own channel - clean and categorised."]
];
var g=document.getElementById("featGrid");
g.innerHTML=FEATURES.map(function(f){return '<div class="feat rev"><span class="ic">'+f[0]+'</span><h3>'+f[1]+'</h3><p>'+f[2]+'</p></div>';}).join("");
// marquee
var words=["Roster","KOS","Strikes","Wars","Tickets","Orders","Recruitment","Radio","Logs","Web Panel"];
var line=words.map(function(w){return '<span><b>'+w+'</b></span>';}).join('<span>✦</span>');
document.getElementById("marqRow").innerHTML=line+'<span>✦</span>'+line;
// spotlight hover
document.addEventListener("mousemove",function(e){
 var t=e.target.closest(".feat");if(!t)return;var r=t.getBoundingClientRect();
 t.style.setProperty("--mx",(e.clientX-r.left)+"px");t.style.setProperty("--my",(e.clientY-r.top)+"px");
});
// reveal on scroll
var io=new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting){x.target.classList.add("in");io.unobserve(x.target);}});},{threshold:.12});
document.querySelectorAll(".rev").forEach(function(e,i){e.style.transitionDelay=(i%4*60)+"ms";io.observe(e);});
var REDUCE=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
// count-up numbers when they scroll into view
function countUp(el){
 var to=parseInt(el.getAttribute("data-n"),10)||0,suf=el.getAttribute("data-suf")||"";
 if(REDUCE){el.textContent=to+suf;return;}
 var t0=performance.now(),dur=1100;
 (function step(t){var p=Math.min((t-t0)/dur,1);el.textContent=Math.round(to*(1-Math.pow(1-p,3)))+suf;if(p<1)requestAnimationFrame(step);})(t0);
}
var cio=new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting){countUp(x.target);cio.unobserve(x.target);}});},{threshold:.4});
document.querySelectorAll("[data-n]").forEach(function(e){cio.observe(e);});
// product mockup: straightens as it scrolls in; its tabs are live
var mock=document.getElementById("mock");
if(mock){
 var mio=new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting){mock.classList.add("up");mio.unobserve(mock);}});},{threshold:.2});
 mio.observe(mock);
 var mtabs=[].slice.call(mock.querySelectorAll(".mptab")),mviews=[].slice.call(mock.querySelectorAll(".mpview"));
 mtabs.forEach(function(t){t.addEventListener("click",function(){
  var v=t.getAttribute("data-v");
  mtabs.forEach(function(x){x.classList.toggle("on",x===t);});
  mviews.forEach(function(x){x.classList.toggle("on",x.getAttribute("data-v")===v);});
 });});
}
// soft light that follows the cursor
var glow=document.getElementById("glow");
if(glow&&!REDUCE&&window.matchMedia("(pointer:fine)").matches){
 var gx=0,gy=0,pending=false;
 document.addEventListener("mousemove",function(e){gx=e.clientX;gy=e.clientY;glow.style.opacity="1";
  if(!pending){pending=true;requestAnimationFrame(function(){glow.style.transform="translate("+gx+"px,"+gy+"px)";pending=false;});}});
 document.addEventListener("mouseleave",function(){glow.style.opacity="0";});
}
// adapt to auth + subscription (invite buttons always route through /invite,
// which forces Discord login before the bot can be added)
// interactive setup walkthrough
(function(){
 var walk=document.getElementById("walk");
 if(!walk)return;
 var tabs=[].slice.call(walk.querySelectorAll(".wtab"));
 var panels=[].slice.call(walk.querySelectorAll(".wpanel"));
 var back=document.getElementById("wback"),next=document.getElementById("wnext"),count=document.getElementById("wcount");
 if(!tabs.length||!panels.length||!back||!next||!count)return;
 var n=panels.length,cur=0;
 walk.classList.add("js");
 function go(i){
  cur=Math.max(0,Math.min(n-1,i));
  tabs.forEach(function(t,k){t.classList.toggle("on",k===cur);t.classList.toggle("done",k<cur);});
  panels.forEach(function(p,k){p.classList.toggle("on",k===cur);});
  back.disabled=cur===0;
  count.textContent="Step "+(cur+1)+" of "+n;
  var last=cur===n-1;
  next.textContent=last?"Get started ▶":"Next ▶";
  next.dataset.cta=last?"1":"";
 }
 tabs.forEach(function(t){t.addEventListener("click",function(){go(+t.dataset.i);});});
 back.addEventListener("click",function(){go(cur-1);});
 next.addEventListener("click",function(){if(next.dataset.cta){location.href="/login";return;}go(cur+1);});
 walk.addEventListener("keydown",function(e){
  if(e.key==="ArrowRight"){go(cur+1);}else if(e.key==="ArrowLeft"){go(cur-1);}
 });
 go(0);
})();
fetch("/api/me").then(function(r){return r.json();}).then(function(me){
 var pub=me.public||{};
 var buy=document.getElementById("buyBtn"),nav=document.getElementById("navCta"),panel=document.getElementById("panelBtn");
 var checkout="/billing/checkout";
 if(me.authed){
   nav.href="/panel";nav.textContent="Open panel";panel.href="/panel";
   var gs=me.guilds||[],act=gs.find(function(x){return x.subscription&&x.subscription.active;});
   if(act){
     buy.textContent="Open your panel";buy.href="/panel";
     document.getElementById("heroStatus").textContent="✓ You're subscribed on "+act.name+" - open your panel";
   }else if(gs.length){
     buy.textContent="Subscribe - $7.99/mo";buy.href=checkout;
     document.getElementById("heroStatus").textContent="You're logged in - subscribe to unlock your server";
   }else{
     buy.textContent="➕ Pick your server";buy.href="/panel";
     nav.href="/panel";nav.textContent="Pick a server";
     document.getElementById("heroStatus").textContent="You're logged in - pick a server to add the bot to";
   }
 }else{
   nav.textContent="Log in";nav.href="/login";
   buy.textContent="Log in to start";buy.href="/login";
 }
}).catch(function(){});
</script>
</body></html>"""


async def _h_landing(request):
    base = _canonical_base(request)
    note_base_url(base)
    page = _bust(_LANDING_HTML.replace(
        "<!--GM_HEAD-->", _head_meta(base, "Gang Manager - the all-in-one FiveM gang bot", _SITE_DESC), 1
    ))
    return _aioweb.Response(text=page, content_type="text/html")


def _pick_billing_guild(request):
    """Resolve the guild a billing action is for: ?guild=, else sole guild."""
    return _guild_from(request.query.get("guild"))


_BACK = "<a class='b p' href='/panel'>Back to the panel</a>"
_NO_ACCESS_TEXT = "Only the server owner (or someone they added in Settings → Website access) can manage it."


def _support_url() -> str:
    return (os.getenv("SUPPORT_URL") or "").strip()


def _support_actions() -> str:
    u = _support_url()
    contact = (
        f"<a class='b p' href='{html.escape(u, quote=True)}' target='_blank' rel='noopener'>Contact us</a>"
        if u else ""
    )
    return contact + "<a class='b' href='/panel'>Back to the panel</a>"


async def _h_billing_checkout(request):
    """Start a Stripe Checkout subscription for one server the user manages.
    A server that's already paid for goes to the billing portal instead, so
    nobody is ever charged twice for the same server."""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.HTTPFound("/login")
    if not _stripe_ready():
        who = "Contact us" if _support_url() else "Ask the bot owner"
        return _simple_page(
            "💳", "Payments are being set up",
            f"Online checkout isn't switched on yet. {who} and they'll activate your server.",
            _support_actions(), 503,
        )
    gid_q = (request.query.get("guild") or "").strip()
    guild = get_guild_any(int(gid_q)) if gid_q.isdigit() else None
    if guild is not None:
        # the bot is already there - only people with panel access can pay for it
        if not await _has_web_access(guild, uid):
            return _simple_page("🔒", "No access to that server", _NO_ACCESS_TEXT, _BACK, 403)
        target_id, target_name = guild.id, guild.name
    elif gid_q.isdigit():
        # subscribe-first: one of the user's own servers the bot hasn't joined yet
        srv = _managed_server(uid, gid_q)
        if srv is None:
            if _USER_CACHE.get(str(uid)) is None:
                return _aioweb.HTTPFound("/login")  # server list expired - log in again
            return _simple_page("🔒", "Not your server",
                                "You can only subscribe for a server you own or manage.", _BACK, 403)
        target_id, target_name = int(gid_q), srv["name"]
    else:
        return _aioweb.HTTPFound("/panel")  # pick a server in the panel first

    if peek_active(target_id):
        st = effective_state(target_id)
        if st.get("customer_id") and guild is not None:
            return _aioweb.HTTPFound(f"/billing/portal?guild={target_id}")
        nxt = (f"<a class='b p' href='/invite?guild={target_id}'>➕ Add the bot</a>" if guild is None else "") + _BACK
        return _simple_page(
            "✅", "Already unlocked",
            f"<b>{html.escape(target_name)}</b> already has an active subscription.", nxt,
        )

    # Must be in the GM Support server before buying - that's where invoices,
    # tickets and updates go. Only block on a definite "no"; if the support bot
    # can't be reached to check, let the purchase through rather than lose a sale.
    if await _in_support(uid) is False:
        inv = await _support_invite_resolved()
        join = (f"<a class='b p' href='{html.escape(inv, quote=True)}' target=_blank rel=noopener>🎫 Join GM Support</a>"
                if inv else "")
        return _simple_page(
            "🎫", "Join the support server first",
            "Before subscribing, please join the <b>GM Support</b> server - that's where your "
            "invoice, support tickets and updates are sent. Join, then come back and subscribe.",
            join + _BACK, 403,
        )

    base = _public_base(request)
    form = [
        ("mode", "subscription"),
        ("line_items[0][price]", os.getenv("STRIPE_PRICE_ID", "")),
        ("line_items[0][quantity]", "1"),
        ("success_url", f"{base}/billing/success?guild={target_id}&session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{base}/panel"),
        ("client_reference_id", str(target_id)),
        ("metadata[guild_id]", str(target_id)),
        ("metadata[purchaser_id]", str(uid)),
        ("metadata[client_ip]", _client_ip(request)),
        ("subscription_data[metadata][guild_id]", str(target_id)),
        ("subscription_data[metadata][purchaser_id]", str(uid)),
        ("allow_promotion_codes", "true"),
    ]
    session = await _stripe_post("checkout/sessions", form)
    if not session or not session.get("url"):
        return _simple_page("⚠️", "Couldn't start checkout", "Stripe didn't respond - please try again in a moment.",
                            _BACK, 502)
    return _aioweb.HTTPFound(session["url"])


async def _h_billing_portal(request):
    """Stripe's hosted billing portal: update the card, see invoices, cancel."""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.HTTPFound("/login")
    guild = _pick_billing_guild(request)
    if guild is None or not await _has_web_access(guild, uid):
        return _simple_page("🔒", "No access to that server", _NO_ACCESS_TEXT, _BACK, 403)
    if not _can_setup(guild, uid):
        return _simple_page("🔒", "Owner only",
                            "Only the server owner or whoever bought the subscription can manage billing.", _BACK, 403)
    cust = (guild_store(DATA, guild.id).get("subscription") or {}).get("customer_id")
    if not cust:
        return _aioweb.HTTPFound(f"/billing/checkout?guild={guild.id}")
    sess = await _stripe_post(
        "billing_portal/sessions",
        [("customer", cust), ("return_url", f"{_public_base(request)}/panel")],
    )
    if not sess or not sess.get("url"):
        return _simple_page(
            "⚠️", "Billing portal unavailable",
            "Stripe couldn't open the billing portal. The site owner may need to switch on the "
            "customer portal in the Stripe dashboard.", _BACK, 502,
        )
    return _aioweb.HTTPFound(sess["url"])


def _await_active_actions(gid: str, subscribe: bool = False) -> str:
    """Buttons for 'waiting on Stripe': polls until the server's subscription
    is active, then unlocks '➕ Add the bot'. gid is validated digits."""
    sub_btn = f"<a class='b' href='/billing/checkout?guild={gid}'>💳 Subscribe</a>" if subscribe else ""
    return (
        "<a class='b p' id='go' href='#' style='opacity:.55;pointer-events:none'>⏳ Waiting for payment…</a>"
        + sub_btn
        + "<a class='b' href='/panel'>Back to the panel</a>"
        "<script>(function(){var g='" + gid + "';function t(){fetch('/api/sub_check?guild='+g)"
        ".then(function(r){return r.json();}).then(function(d){if(d.active){var a=document.getElementById('go');"
        "a.href='/invite?guild='+g;a.textContent='➕ Add the bot';a.style.opacity='1';a.style.pointerEvents='auto';}"
        "else setTimeout(t,2500);}).catch(function(){setTimeout(t,4000);});}t();})();</script>"
    )


async def _h_sub_check(request):
    """Is this server subscribed yet? (Polled while waiting on Stripe.)"""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.json_response({"active": False}, status=401)
    gid = (request.query.get("guild") or "").strip()
    if not gid.isdigit():
        return _aioweb.json_response({"active": False})
    return _aioweb.json_response({"active": peek_active(gid), "bot_in": get_guild_any(int(gid)) is not None})


def _bot_info_for_guild(gid):
    """Who serves this server and their REAL status, for support tickets. Reports
    the actual assigned pool bot (Online / Offline / Disabled / Not in the server),
    or the main bot when none is assigned - never a misleading fallback."""
    info = guild_bot_status(gid)
    if info is not None:
        return info
    # No pool bot assigned - the main bot serves it.
    u = getattr(bot, "user", None)
    try:
        online = bool(bot.is_ready())
    except Exception:  # noqa: BLE001
        online = False
    ping = None
    try:
        lat = bot.latency
        if lat is not None and lat == lat and lat not in (float("inf"), float("-inf")):
            ping = max(0, round(lat * 1000))
    except Exception:  # noqa: BLE001
        ping = None
    return {
        "bot_name": str(getattr(u, "name", "") or "") if u is not None else "",
        "bot_id": str(getattr(u, "id", "") or "") if u is not None else "",
        "bot_label": "Main bot",
        "bot_status": "Online" if online else "Offline",
        "bot_online": online, "bot_ping": ping,
    }


async def _h_support_verify(request):
    """Staff-bot lookup for Purchase Help tickets: verify an invoice ID the user
    typed against real purchases, and report which server + bot it's for, plus the
    user's own servers and their bots. Authed with the shared secret."""
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not secret or request.headers.get("X-Guard-Token", "") != secret:
        return _aioweb.json_response({"error": "unauthorized"}, status=401)
    uid = (request.query.get("user") or "").strip()
    invoice = (request.query.get("invoice") or "").strip()[:120]
    if not uid.isdigit():
        return _aioweb.json_response({"error": "bad user"}, status=400)

    match_out = None
    if invoice:
        for e in shop_log_list(600):
            if str(e.get("session_id") or "") == invoice:
                mgid = str(e.get("guild_id") or "")
                g = get_guild_any(mgid) if mgid else None
                match_out = {
                    "server_id": mgid,
                    "server_name": (g.name if g is not None else ""),
                    "amount": e.get("amount"), "currency": e.get("currency"),
                    "ts": e.get("ts"), "kind": e.get("kind"),
                    "owned": str(e.get("purchaser_id") or "") == uid,
                    **(_bot_info_for_guild(mgid) if mgid else {}),
                }
                break

    # the user's own servers (bought by them or subscription-linked), each with bot
    seen = {}
    for e in shop_log_list(600):
        if str(e.get("purchaser_id") or "") == uid:
            g = str(e.get("guild_id") or "")
            if g:
                seen[g] = True
    for gid2, store in (DATA.get("guilds") or {}).items():
        sub = store.get("subscription") if isinstance(store, dict) else None
        if isinstance(sub, dict) and str(sub.get("purchaser_id") or "") == uid:
            seen[str(gid2)] = True
    servers = []
    for g in list(seen)[:10]:
        gg = get_guild_any(g)
        try:
            st = subscription_state(int(g))
        except Exception:  # noqa: BLE001
            st = {}
        servers.append({
            "server_id": g, "server_name": (gg.name if gg is not None else ""),
            "active": bool(st.get("active")), "plan": st.get("plan"),
            **_bot_info_for_guild(g),
        })

    return _aioweb.json_response({
        "ok": True, "queried": invoice,
        "valid": match_out is not None, "match": match_out, "servers": servers,
    })


async def _h_support_lookup(request):
    """Staff-bot lookup: one Discord user's Gang Manager billing, matched by the
    purchaser recorded at checkout (or by server ownership). Authed with the
    shared secret - it returns a single customer's subscription status so the
    support bot can show it in a ticket. Never public (names, Stripe ids)."""
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not secret or request.headers.get("X-Guard-Token", "") != secret:
        return _aioweb.json_response({"error": "unauthorized"}, status=401)
    uid = (request.query.get("user") or "").strip()
    if not uid.isdigit():
        return _aioweb.json_response({"error": "bad user"}, status=400)
    uid_i = int(uid)

    subs = []
    for gid, store in (DATA.get("guilds") or {}).items():
        if not isinstance(store, dict):
            continue
        sub = store.get("subscription")
        if not isinstance(sub, dict):
            continue
        pid = str(sub.get("purchaser_id") or "")
        g = get_guild_any(gid)
        owner_id = getattr(g, "owner_id", None) if g is not None else None
        paid_by_user = pid == uid
        if not paid_by_user and owner_id != uid_i:
            continue
        try:
            st = subscription_state(int(gid))
        except Exception:  # noqa: BLE001
            continue
        # owner-only matches with no real subscription aren't this user's billing
        if not paid_by_user and st.get("status") in ("none", None) and not st.get("active"):
            continue
        subs.append({
            "guild_id": str(gid),
            "guild_name": (g.name if g is not None else store.get("name") or ""),
            "active": bool(st.get("active")),
            "status": st.get("status"),
            "plan": st.get("plan"),
            "expires_at": st.get("expires_at"),
            "days_left": st.get("days_left"),
            "lifetime": bool(st.get("lifetime")),
            "sub_id": st.get("sub_id"),
            "customer_id": st.get("customer_id"),
            "purchased": paid_by_user,
        })

    # Best = an active one first (lifetime, then most days left), else anything.
    subs.sort(key=lambda s: (not s["active"], not s["lifetime"], -(s["days_left"] or 0)))
    best = subs[0] if subs else None

    purchases = []
    try:
        for e in shop_log_list(400):
            if str(e.get("purchaser_id") or "") != uid:
                continue
            purchases.append({
                "ts": e.get("ts"),
                "kind": e.get("kind"),
                "guild_id": e.get("guild_id"),
                "amount": e.get("amount"),
                "currency": e.get("currency"),
                "session_id": e.get("session_id"),
                "customer_id": e.get("customer_id"),
            })
        purchases.sort(key=lambda p: p.get("ts") or 0, reverse=True)
        purchases = purchases[:8]
    except Exception:  # noqa: BLE001
        pass

    payload = {
        "user": uid,
        "active": bool(best and best["active"]),
        "sub_count": len(subs),
        "subscriptions": subs,
        "purchases": purchases,
    }
    if best:
        payload.update({
            "plan": best["plan"], "status": best["status"],
            "expires_at": best["expires_at"], "days_left": best["days_left"],
            "lifetime": best["lifetime"], "sub_id": best["sub_id"],
            "customer_id": best["customer_id"],
            "guild_id": best["guild_id"], "guild_name": best["guild_name"],
        })
    resp = _aioweb.json_response(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _h_internal_subscription(request):
    """Staff-bot subscription control: grant / extend / cancel / status a server's
    Gang Manager subscription from the GM Support (staff) bot's owner-only slash
    commands. The subscription data lives here on the main bot, so the staff bot
    POSTs its admin intent over the shared secret and this performs it. Authed
    with X-Guard-Token (same MFA_SECRET/WEBHOOK_SECRET as the other /internal
    staff endpoints). Mirrors the web admin's /api/admin/sub behaviour."""
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not secret or request.headers.get("X-Guard-Token", "") != secret:
        return _aioweb.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _aioweb.json_response({"ok": False, "error": "bad json"}, status=400)

    try:
        gid = int(body.get("guild_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"ok": False, "error": "bad guild"}, status=400)

    action = str(body.get("action") or "").strip().lower()
    plan = str(body.get("plan") or DEFAULT_PLAN).strip() or DEFAULT_PLAN
    lifetime = bool(body.get("lifetime"))
    actor = str(body.get("actor_name") or "the bot owner").strip()[:80] or "the bot owner"

    def _state_payload(st: dict) -> dict:
        return {k: st.get(k) for k in
                ("active", "mode", "status", "plan", "days_left", "expires_at", "lifetime")}

    if action == "status":
        st = subscription_state(gid)
        target = get_guild_any(gid)
        return _aioweb.json_response({
            "ok": True, "action": "status", "guild_id": str(gid),
            "server_name": (target.name if target is not None else ""),
            "in_server": target is not None, "state": _state_payload(st),
        })

    if action in ("grant", "add_days", "extend"):
        try:
            days = int(body.get("days", 30))
        except (TypeError, ValueError):
            return _aioweb.json_response({"ok": False, "error": "bad days"}, status=400)
        if lifetime:
            st = grant(gid, days=None, plan=plan, status="active", lifetime=True)
            summary = f"lifetime ({plan})"
        else:
            days = max(-3650, min(3650, days))
            if days == 0:
                return _aioweb.json_response({"ok": False, "error": "days can't be 0"}, status=400)
            st = grant(gid, days=days, plan=plan, status="active")
            summary = f"{days:+d} day(s) ({plan})"
    elif action in ("cancel", "revoke", "expire"):
        st = expire(gid)
        summary = "cancelled"
    else:
        return _aioweb.json_response({"ok": False, "error": "unknown action"}, status=400)

    target = get_guild_any(gid)
    name = target.name if target is not None else str(gid)
    log.info("internal sub action=%s guild=%s by=staff(%s)", action, gid, actor)

    # In-server activity log (matches the old /subscription slash command).
    if target is not None:
        verb = "granted" if action in ("grant", "add_days", "extend") else "expired"
        try:
            await send_log(target, guild_store(DATA, gid),
                           f"💳 Subscription {verb} by {actor} (staff bot - {summary}).",
                           category="web")
        except discord.HTTPException:
            pass

    # Staff-bot #subscriptions channel.
    verb = "Granted" if action in ("grant", "add_days", "extend") else "Cancelled"
    _sub_status = ("♾️ Lifetime" if st.get("lifetime") else
                   (f"✅ Active · {st.get('days_left')}d left"
                    if st.get("active") and st.get("days_left") is not None else
                    "✅ Active" if st.get("active") else "⛔ Expired"))
    _fire(_gmlog(
        "subscription",
        f"{verb} subscription - {name}",
        f"{summary} by {actor} via the staff bot.",
        fields=[
            {"name": "🏠 Server", "value": f"{name}\n`{gid}`", "inline": True},
            {"name": "⚙️ Action", "value": f"`{action}`", "inline": True},
            {"name": "📦 Change", "value": str(summary), "inline": True},
            {"name": "👤 By", "value": str(actor), "inline": True},
            {"name": "📊 Status now", "value": _sub_status, "inline": True},
            {"name": "🏷️ Plan", "value": str(st.get("plan") or "-"), "inline": True},
        ],
    ))

    return _aioweb.json_response({
        "ok": True, "action": action, "guild_id": str(gid),
        "server_name": (target.name if target is not None else ""),
        "in_server": target is not None, "summary": summary,
        "state": _state_payload(st),
    })


async def _h_sub_move(request):
    """Move the user's active subscription onto another server they manage - no
    new charge. Releases the old server and repoints the live Stripe sub so
    renewals follow it. The panel offers this instead of paying twice."""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _aioweb.json_response({"ok": False, "error": "login"}, status=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    to_gid = str(body.get("guild") or "").strip()
    if not to_gid.isdigit():
        return _aioweb.json_response({"ok": False, "error": "Pick a server to move it to."}, status=400)

    # The user must manage the destination (owner / panel access, or their own
    # server the bot hasn't joined yet).
    dest = get_guild_any(int(to_gid))
    if dest is not None:
        if not await _has_web_access(dest, uid):
            return _aioweb.json_response({"ok": False, "error": "You don't manage that server."}, status=403)
    elif _managed_server(uid, to_gid) is None:
        if _USER_CACHE.get(str(uid)) is None:
            return _aioweb.json_response({"ok": False, "error": "login"}, status=401)
        return _aioweb.json_response({"ok": False, "error": "You can only move it to a server you manage."}, status=403)

    if peek_active(to_gid):
        return _aioweb.json_response({"ok": False, "error": "That server already has a subscription."}, status=400)

    src = _movable_sub_for(uid, exclude_gid=to_gid)
    if not src:
        return _aioweb.json_response({"ok": False, "error": "You don't have a subscription to move."}, status=400)

    from_gid = src["id"]
    sub_id = effective_state(int(from_gid)).get("sub_id")  # capture before the move unlinks it
    state = move_subscription(from_gid, to_gid)
    if state is None:
        return _aioweb.json_response(
            {"ok": False, "error": "Couldn't move it - the subscription may have just lapsed."}, status=400
        )

    # Repoint the live Stripe subscription at the new server so its next invoice
    # renews the right one (best-effort - the move already succeeded in our data).
    if sub_id and _stripe_ready():
        try:
            await _stripe_post(f"subscriptions/{sub_id}", [("metadata[guild_id]", str(to_gid))])
        except Exception as exc:  # noqa: BLE001
            log.warning("sub move: couldn't repoint Stripe metadata for %s: %s", sub_id, exc)

    log.info("subscription moved via panel: %s -> %s by user %s", from_gid, to_gid, uid)
    return _aioweb.json_response(
        {
            "ok": True,
            "moved_from": src["name"],
            "bot_in": dest is not None,
            "subscription": _sub_public(int(to_gid)),
        }
    )


# A celebratory post-checkout page. It drives itself from /api/sub_check:
# waits for the webhook to activate the sub, reveals "Add the bot" (opens the
# Discord invite in a NEW tab so this page stays alive), then auto-advances to
# the panel the moment the bot actually joins the server. __GID__ (validated
# digits) and __BRAND__ (html-escaped) are substituted at serve time.
_SUCCESS_HTML = r"""<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;color:#ececf1;overflow:hidden;
 font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 background:radial-gradient(1100px 560px at 15% -10%,#2a2a33 0%,transparent 60%),
 radial-gradient(900px 520px at 110% 115%,#3b3b46 0%,transparent 55%),#060608}
canvas#cf{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0}
.card{position:relative;z-index:1;width:100%;max-width:480px;text-align:center;padding:34px 30px 28px;
 border-radius:26px;background:rgba(16,16,22,.76);border:1px solid rgba(255,255,255,.12);
 backdrop-filter:blur(12px);box-shadow:0 36px 100px rgba(0,0,0,.6);animation:in .5s cubic-bezier(.2,.7,.2,1)}
@keyframes in{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:none}}
.logo{width:92px;height:56px;object-fit:contain;filter:drop-shadow(0 0 26px rgba(255,255,255,.22))}
.check{width:74px;height:74px;margin:14px auto 2px;border-radius:50%;display:grid;place-items:center;
 background:radial-gradient(circle at 50% 35%,rgba(74,222,128,.28),rgba(74,222,128,.06));
 border:1px solid rgba(74,222,128,.45);box-shadow:0 0 40px rgba(74,222,128,.25)}
.check svg{width:38px;height:38px}
.check path{stroke:#4ade80;stroke-width:5;fill:none;stroke-linecap:round;stroke-linejoin:round;
 stroke-dasharray:48;stroke-dashoffset:48;animation:draw .55s .25s forwards cubic-bezier(.6,.2,.2,1)}
@keyframes draw{to{stroke-dashoffset:0}}
h1{font-size:24px;margin:10px 0 6px;letter-spacing:-.4px}
.sub{color:#a7a7b2;line-height:1.6;margin:0 0 20px;font-size:15px}
.steps{display:flex;gap:6px;justify-content:center;margin:0 0 6px}
.step{flex:1;max-width:116px;display:flex;flex-direction:column;align-items:center;gap:7px;opacity:.4;transition:.35s}
.step.on,.step.done{opacity:1}
.dot{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:800;
 background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.18);color:#cfcfd6;transition:.35s}
.step.on .dot{border-color:#fff;background:rgba(255,255,255,.14);color:#fff;box-shadow:0 0 0 4px rgba(255,255,255,.06)}
.step.done .dot{background:linear-gradient(135deg,#4ade80,#22c55e);border-color:transparent;color:#06240f}
.step .lbl{font-size:11px;font-weight:600;color:#c3c3cc;letter-spacing:.2px}
.rail{position:relative;height:2px;background:rgba(255,255,255,.1);margin:4px 16% 22px;border-radius:2px}
.rail i{position:absolute;top:0;bottom:0;left:0;right:100%;background:linear-gradient(90deg,#4ade80,#22c55e);
 border-radius:2px;transition:right .55s cubic-bezier(.2,.7,.2,1)}
.row{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
a.b{display:inline-flex;align-items:center;gap:8px;padding:12px 20px;border-radius:13px;font-weight:700;
 text-decoration:none;font-size:14.5px;border:1px solid rgba(255,255,255,.16);color:#fff;
 background:rgba(255,255,255,.06);transition:.2s}
a.b.p{background:linear-gradient(90deg,#c9c9d2,#fff);color:#09090c;border:none}
a.b.g{background:linear-gradient(90deg,#4ade80,#22c55e);color:#06240f;border:none;box-shadow:0 10px 30px rgba(34,197,94,.3)}
a.b.wait{opacity:.5;pointer-events:none}
a.b.pulse{animation:pulse 1.8s infinite}
@keyframes pulse{0%,100%{box-shadow:0 10px 30px rgba(34,197,94,.3)}50%{box-shadow:0 10px 46px rgba(34,197,94,.58)}}
.stat{margin:15px 0 0;font-size:12.5px;color:#8f8f99;min-height:16px}
.stat b{color:#4ade80;font-weight:700}
.hint{font-size:11.5px;color:#73737d;margin-top:4px;min-height:14px}
.foot{margin-top:18px;font-size:11px;color:#6a6a74;letter-spacing:.3px}
.spin{display:inline-block;width:11px;height:11px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;
 border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head>
<body>
<canvas id=cf></canvas>
<div class=card>
 <img class=logo src='/gm-logo.png' alt='Gang Manager'>
 <div class=check><svg viewBox='0 0 52 52'><path d='M14 27 l8 8 l16 -18'/></svg></div>
 <h1>You're subscribed 🎉</h1>
 <p class=sub id=lead>__BRAND__ is activating. One quick step left - add the bot to your server.</p>
 <div class=steps>
  <div class=step id=st0><div class=dot>1</div><div class=lbl>Payment</div></div>
  <div class=step id=st1><div class=dot>2</div><div class=lbl>Add bot</div></div>
  <div class=step id=st2><div class=dot>3</div><div class=lbl>Ready</div></div>
 </div>
 <div class=rail><i id=rail></i></div>
 <div class=row><a class='b p wait' id=go href='#'><span class=spin></span>Confirming payment…</a></div>
 <div class=stat id=stat aria-live=polite>Checking your payment with Stripe…</div>
 <div class=hint id=hint></div>
 <div class=row style='margin-top:14px'><a class='b' id=panel href='/panel'>Skip to the panel</a></div>
 <div class=foot>Gang Manager · made by pmxl</div>
</div>
<script>
(function(){
 var g='__GID__';
 var go=document.getElementById('go'),stat=document.getElementById('stat'),hint=document.getElementById('hint'),
     lead=document.getElementById('lead'),rail=document.getElementById('rail'),
     st0=document.getElementById('st0'),st1=document.getElementById('st1'),st2=document.getElementById('st2');
 var RM=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
 var phase=-1,redirecting=false;
 function setPhase(p){
   if(p<=phase)return;phase=p;
   if(p>=0)st0.className='step done';
   if(p>=1){st1.className='step on';rail.style.right='50%';}
   if(p>=2){st1.className='step done';st2.className='step done';rail.style.right='0%';}
 }
 function waiting(){
   setPhase(0);
   go.className='b p wait';go.innerHTML='<span class=spin></span>Confirming payment…';go.href='#';
   stat.innerHTML='Checking your payment with Stripe…';hint.textContent='';
 }
 function readyToAdd(){
   setPhase(1);
   go.className='b g pulse';go.innerHTML='➕ Add __BRAND__';go.href='/invite?guild='+g;
   go.target='_blank';go.rel='noopener';
   stat.innerHTML='Subscription <b>active</b> ✓';
   hint.textContent='Opens Discord in a new tab - this page moves on the moment the bot joins.';
 }
 function done(){
   if(redirecting)return;redirecting=true;
   setPhase(2);
   go.className='b g';go.innerHTML='✓ Bot added';go.removeAttribute('target');go.href='/panel';
   lead.textContent="You're all set. Opening your panel…";
   stat.innerHTML='<b>All set!</b> Taking you to your dashboard…';hint.textContent='';
   burst(180);
   setTimeout(function(){location.href='/panel';},1400);
 }
 function tick(){
   if(redirecting)return;
   fetch('/api/sub_check?guild='+g).then(function(r){return r.json();}).then(function(d){
     if(d.active&&d.bot_in){done();return;}
     if(d.active)readyToAdd();else waiting();
     setTimeout(tick,d.active?3000:2500);
   }).catch(function(){setTimeout(tick,4000);});
 }
 if(!/^[0-9]+$/.test(g)){
   setPhase(0);go.className='b p';go.innerHTML='Open the panel';go.href='/panel';
   stat.textContent='';hint.textContent='';
   var sw=document.querySelector('.steps'),rw=document.querySelector('.rail');
   if(sw)sw.style.display='none';if(rw)rw.style.display='none';
 }else{tick();}

 // lightweight one-shot confetti
 var cv=document.getElementById('cf'),cx=cv.getContext('2d'),W,H,parts=[],raf=0,until=0;
 function size(){W=cv.width=innerWidth;H=cv.height=innerHeight;}
 size();addEventListener('resize',size);
 var COL=['#4ade80','#22c55e','#ffffff','#c9c9d2','#8b5cf6','#f59e0b'];
 function burst(n){
   if(RM)return;n=n||160;
   for(var i=0;i<n;i++)parts.push({x:W/2+(Math.random()-.5)*140,y:H*0.32,
     vx:(Math.random()-.5)*9,vy:-Math.random()*11-3,gr:.26+Math.random()*.14,
     s:5+Math.random()*6,r:Math.random()*6.28,vr:(Math.random()-.5)*.3,
     c:COL[(Math.random()*COL.length)|0]});
   until=Date.now()+2600;if(!raf)raf=requestAnimationFrame(loop);
 }
 function loop(){
   cx.clearRect(0,0,W,H);
   for(var i=parts.length-1;i>=0;i--){var p=parts[i];
     p.vy+=p.gr;p.x+=p.vx;p.y+=p.vy;p.r+=p.vr;p.vx*=.99;
     if(p.y>H+30||p.x<-30||p.x>W+30){parts.splice(i,1);continue;}
     cx.save();cx.translate(p.x,p.y);cx.rotate(p.r);cx.fillStyle=p.c;
     cx.fillRect(-p.s/2,-p.s/2,p.s,p.s*.6);cx.restore();}
   if(parts.length||Date.now()<until)raf=requestAnimationFrame(loop);else{raf=0;cx.clearRect(0,0,W,H);}
 }
 setTimeout(function(){burst(170);},350);
})();
</script>
</body></html>"""


def _billing_success_page(gid: str, brand: dict, status: int = 200):
    """Celebratory post-checkout page (see _SUCCESS_HTML). gid must already be
    validated digits or empty; brand name is escaped for both HTML and the JS
    string literals it lands in."""
    name = (html.escape(brand.get("name") or "Gang Manager") or "Gang Manager").replace("\\", "")
    head = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>You're subscribed · Gang Manager</title>"
        + _head_meta(web_base_url(), "You're subscribed · Gang Manager", _SITE_DESC)
    )
    body = _SUCCESS_HTML.replace("__GID__", gid).replace("__BRAND__", name)
    return _aioweb.Response(text=_bust(head + body), status=status, content_type="text/html")


async def _h_billing_success(request):
    gid = (request.query.get("guild") or "").strip()
    if not gid.isdigit():
        gid = ""  # no server context - page shows a plain "open the panel"
    return _billing_success_page(gid, _brand_info())


def _stripe_sig_ok(payload: bytes, header: str, secret: str) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        t, v1 = parts.get("t"), parts.get("v1")
        if not t or not v1:
            return False
        mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, v1)
    except Exception:
        return False


def _dig(obj, *path):
    cur = obj
    for k in path:
        if isinstance(k, int):
            cur = cur[k] if isinstance(cur, list) and len(cur) > k else None
        else:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _client_ip(request) -> str:
    """Best-effort client IP. Railway is behind a proxy, so trust the first
    X-Forwarded-For hop, falling back to the socket peer."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.remote or "")[:64]


def _event_meta(obj: dict, key: str):
    """A metadata value from wherever Stripe put it: the object itself, an
    invoice's subscription details (old and new API shapes), or its first line."""
    for path in (
        ("metadata", key),
        ("subscription_details", "metadata", key),
        ("parent", "subscription_details", "metadata", key),
        ("lines", "data", 0, "metadata", key),
    ):
        v = _dig(obj, *path)
        if v:
            return v
    return None


def _event_guild_id(obj: dict) -> int | None:
    for v in (_event_meta(obj, "guild_id"), obj.get("client_reference_id")):
        if v and str(v).isdigit():
            return int(v)
    return None


def _event_sub_id(obj: dict) -> str | None:
    """The Stripe *subscription* id an event is about - never an invoice id."""
    if obj.get("object") == "subscription":
        return obj.get("id")
    for path in (
        ("subscription",),
        ("parent", "subscription_details", "subscription"),
        ("lines", "data", 0, "subscription"),
        ("lines", "data", 0, "parent", "subscription_item_details", "subscription"),
    ):
        v = _dig(obj, *path)
        if isinstance(v, dict):
            v = v.get("id")
        if isinstance(v, str) and v.startswith("sub_"):
            return v
    return None


def _event_period_end(obj: dict):
    """When the period this event pays for ends."""
    if obj.get("object") == "subscription":
        return _sub_period_end(obj)
    ends = [
        _dig(line, "period", "end")
        for line in (_dig(obj, "lines", "data") or [])
        if isinstance(line, dict)
    ]
    ends = [e for e in ends if e]
    return _ts_to_dt(max(ends)) if ends else None


def _is_current_sub(gid: int, sub_id: str | None) -> bool:
    """True if this Stripe subscription is the one the server is running on (or
    the server has none recorded) - so a stale cancel can't kill a newer sub."""
    stored = (guild_store(DATA, gid).get("subscription") or {}).get("sub_id")
    return not stored or not sub_id or stored == sub_id


async def _notify(gid: int, text: str) -> None:
    g = get_guild_any(gid)
    if g is not None:
        try:
            await send_log(g, guild_store(DATA, gid), text, category="web")
        except discord.HTTPException:
            pass


async def _h_billing_webhook(request):
    import datetime as _dt

    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    payload = await request.read()
    if secret:
        if not _stripe_sig_ok(payload, request.headers.get("Stripe-Signature", ""), secret):
            return _aioweb.Response(status=400, text="bad signature")
    else:
        log.warning("STRIPE_WEBHOOK_SECRET isn't set - Stripe webhooks aren't being verified.")
    try:
        event = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        return _aioweb.Response(status=400, text="bad payload")

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    gid = _event_guild_id(obj)
    if gid is None:
        return _aioweb.json_response({"received": True, "ignored": True})  # not ours
    sub_id = _event_sub_id(obj)
    now = _dt.datetime.now(_dt.timezone.utc)

    if etype in ("checkout.session.completed", "invoice.paid", "invoice.payment_succeeded"):
        if etype == "checkout.session.completed" and obj.get("payment_status") == "unpaid":
            return _aioweb.json_response({"received": True})  # invoice.paid activates it later
        # One server per subscription: a sub that already powers another server
        # can't unlock a second one.
        other = guild_for_sub(sub_id, exclude_guild=gid)
        if other is not None:
            log.warning("Sub %s already active on guild %s - refusing to bind guild %s", sub_id, other, gid)
            await _notify(gid, f"⚠️ That subscription already covers another server (`{other}`). "
                               "Each subscription covers one server - start a separate one here.")
            return _aioweb.json_response({"received": True, "conflict": True})
        end = _event_period_end(obj)
        # Paid-until = end of the billed period (+1 day grace). Grants only move
        # forward, so checkout + invoice for the same payment never stack.
        until = (end or now + _dt.timedelta(days=PLANS[DEFAULT_PLAN]["days"])) + _dt.timedelta(days=1)
        was_active = subscription_state(gid)["active"]
        grant(
            gid, until=until, plan=DEFAULT_PLAN, status="active",
            customer_id=obj.get("customer"), sub_id=sub_id,
            purchaser_id=_event_meta(obj, "purchaser_id"),
        )
        try:
            details = obj.get("customer_details") or {}
            shop_log_add({
                "ts": int(now.timestamp()),
                "kind": "purchase" if etype == "checkout.session.completed" else "renewal",
                "guild_id": str(gid),
                "email": (details.get("email") or obj.get("customer_email") or "")[:200],
                "ip": (_event_meta(obj, "client_ip") or "")[:64],
                "amount": int(obj.get("amount_total") or obj.get("amount_paid") or 0),
                "currency": (obj.get("currency") or "").upper()[:8],
                "customer_id": (obj.get("customer") or "")[:64],
                "purchaser_id": (_event_meta(obj, "purchaser_id") or "")[:32],
                "session_id": (obj.get("id") or "")[:80],
            })
        except Exception:  # noqa: BLE001
            log.exception("shop log record failed")
        try:
            g = get_guild_any(gid)
            det = obj.get("customer_details") or {}
            amt = int(obj.get("amount_total") or obj.get("amount_paid") or 0)
            cur = (obj.get("currency") or "").upper()
            pid = _event_meta(obj, "purchaser_id") or ""
            addr = det.get("address") if isinstance(det, dict) else None
            country = (addr.get("country") or "") if isinstance(addr, dict) else ""
            plan_label = (PLANS.get(DEFAULT_PLAN) or {}).get("label") or DEFAULT_PLAN
            sid = obj.get("id") or ""
            new_sale = etype == "checkout.session.completed"
            _fire(_gmlog(
                "purchase",
                "🛒 New purchase" if new_sale else "💳 Subscription renewal",
                description=f"**{plan_label}** on **{g.name if g else gid}** - {amt/100:.2f} {cur}".strip(),
                fields=[
                    {"name": "🏠 Server", "value": f"{g.name if g else gid}\n`{gid}`", "inline": True},
                    {"name": "👤 Buyer", "value": (f"<@{pid}>\n`{pid}`" if str(pid).isdigit() else "-"), "inline": True},
                    {"name": "💵 Amount", "value": (f"**{amt/100:.2f} {cur}**".strip() or "-"), "inline": True},
                    {"name": "📦 Plan", "value": str(plan_label), "inline": True},
                    {"name": "📧 Email", "value": (det.get("email") or obj.get("customer_email") or "-")[:200], "inline": True},
                    {"name": "🌍 Country", "value": country or "-", "inline": True},
                    {"name": "🌐 IP", "value": (_event_meta(obj, "client_ip") or "-")[:64], "inline": True},
                    {"name": "🔁 Renews", "value": (f"<t:{int(until.timestamp())}:D>" if until else "-"), "inline": True},
                    {"name": "✨ First activation", "value": ("Yes - newly unlocked 🎉" if not was_active else "No - already active"), "inline": True},
                    {"name": "🧾 Invoice ID", "value": (f"`{str(sid)[:90]}`" if sid else "-"), "inline": False},
                    {"name": "💳 Stripe customer", "value": f"`{obj.get('customer') or '-'}`", "inline": True},
                ],
            ))
            _fire(_dm_invoice(pid, sid, (g.name if g else str(gid)), amt, cur, is_renewal=not new_sale))
        except Exception:  # noqa: BLE001
            log.info("gmlog purchase skipped")
        if not was_active:
            await _notify(gid, "💳 Subscription active - Gang Manager is fully unlocked. Thanks!")
    elif etype == "invoice.payment_failed":
        # Stripe retries failed cards; keep what's already paid for and warn.
        await _notify(gid, "⚠️ A subscription payment failed. Stripe will retry - update the card from "
                           "**Settings → Subscription → Manage billing** to avoid an interruption.")
    elif etype == "customer.subscription.updated":
        status = obj.get("status")
        if status in ("canceled", "unpaid", "incomplete_expired") and _is_current_sub(gid, sub_id):
            expire(gid)
            await _notify(gid, "🔒 Subscription ended - Gang Manager is paused here until it's renewed.")
        elif status in ("active", "trialing"):
            end = _sub_period_end(obj)
            if end is not None:
                grant(gid, until=end + _dt.timedelta(days=1), plan=DEFAULT_PLAN, status="active",
                      customer_id=obj.get("customer"), sub_id=sub_id)
    elif etype == "customer.subscription.deleted":
        if _is_current_sub(gid, sub_id):
            expire(gid)
            await _notify(gid, "🔒 Subscription cancelled - Gang Manager is paused here until it's renewed.")
    return _aioweb.json_response({"received": True})


# ---- website on/off toggle -------------------------------------------------- #

async def _h_web_toggle(request):
    """Turn this server's website panel on or off. Reachable even while off so
    staff can switch it back on."""
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled", not store.get("web_enabled", True)))
    store["web_enabled"] = enabled
    save_data(DATA)
    await send_log(
        guild, store,
        f"🌐 Website panel turned **{'on' if enabled else 'off'}**.",
        category="web", actor=_web_actor(request, guild),
    )
    return _aioweb.json_response({"enabled": enabled})


# ---- settings editor -------------------------------------------------------- #

def _settings_list(guild: "discord.Guild", store: dict) -> dict:
    """Every configurable channel/role, its current value, and the options."""
    me = getattr(guild, "me", None)
    bot_top = me.top_role.position if me is not None and me.top_role is not None else 10 ** 9
    channels = [
        {"id": str(c.id), "name": c.name}
        for c in sorted(guild.text_channels, key=lambda c: c.position)
    ]
    roles = [
        {"id": str(r.id), "name": r.name, "locked": r.position >= bot_top}
        for r in sorted((r for r in guild.roles if r.name != "@everyone"),
                        key=lambda r: r.position, reverse=True)
    ]
    items, done = [], 0
    for key, (label, skey, kind) in CONFIG_SETTINGS.items():
        val = store.get(skey)
        obj = (guild.get_channel(val) if kind == "channel" else guild.get_role(val)) if val else None
        if obj is not None:
            done += 1
        items.append({
            "key": key,
            "label": label,
            "emoji": _SETTING_EMOJI.get(key, "•"),
            "kind": kind,
            "value": str(val) if val else "",
            "value_name": getattr(obj, "name", "") if obj else "",
            "is_log": key in _LOG_KEYS,
        })
    return {
        "items": items,
        "channels": channels,
        "roles": roles,
        "progress": {"done": done, "total": len(CONFIG_SETTINGS)},
        "web_enabled": bool(store.get("web_enabled", True)),
        "invite_url": _invite_url(),
        "web_access": _web_members_list(guild, store),
        "subscription": _sub_public(guild.id),
        "stripe": _stripe_ready(),
    }


async def _h_sub_admin(request):
    """Bot-owner subscription controls from the panel: comp 30 days, lifetime,
    expire, or re-sync everything from Stripe."""
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid or not is_owner(uid):
        return _aioweb.json_response({"error": "Only the bot owner can do that."}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = body.get("action")
    if action == "grant30":
        grant(guild.id, days=30, plan=DEFAULT_PLAN, status="active")
        msg = "Added 30 days"
    elif action == "lifetime":
        grant(guild.id, lifetime=True, plan="lifetime", status="active")
        msg = "Granted lifetime access"
    elif action == "expire":
        expire(guild.id)
        msg = "Subscription expired"
    elif action == "sync":
        n = await sync_subscriptions_from_stripe()
        msg = f"Synced {n} subscription(s) from Stripe"
    else:
        return _aioweb.json_response({"error": "Unknown action."}, status=400)
    if action != "sync":
        await send_log(guild, guild_store(DATA, guild.id), f"💳 {msg} - by the bot owner from the panel.",
                       category="web", actor=_web_actor(request, guild))
    return _aioweb.json_response({"ok": True, "message": msg, "subscription": _sub_public(guild.id)})


def _web_members_list(guild: "discord.Guild", store: dict) -> dict:
    """The panel's access allowlist plus pickable server members."""
    allowed_ids = []
    for x in store.get("web_members") or []:
        try:
            allowed_ids.append(int(x))
        except (TypeError, ValueError):
            pass
    allowed = []
    for uid in allowed_ids:
        m = guild.get_member(uid)
        allowed.append({"id": str(uid), "name": (m.display_name if m else f"User {uid}")})
    # Candidates for the picker: real members (needs the members intent to be
    # complete), bots excluded, already-allowed excluded, name-sorted, capped.
    aset = set(allowed_ids)
    cands = []
    for m in getattr(guild, "members", []) or []:
        if getattr(m, "bot", False) or m.id in aset:
            continue
        cands.append({"id": str(m.id), "name": m.display_name})
    cands.sort(key=lambda c: c["name"].lower())
    owner_id = getattr(guild, "owner_id", None)
    return {
        "allowed": allowed,
        "candidates": cands[:500],
        "owner_id": str(owner_id) if owner_id else "",
        "member_count": len(cands) + len(allowed),
    }


async def _h_logs_setup(request):
    """Create the Logs category + a channel per log type, from the website."""
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    me = getattr(guild, "me", None)
    if me is None or not me.guild_permissions.manage_channels:
        return _aioweb.json_response(
            {"error": "The bot needs the Manage Channels permission to create log channels."},
            status=400,
        )
    store = guild_store(DATA, guild.id)
    try:
        created, kept = await ensure_log_channels(guild, store)
    except discord.HTTPException:
        return _aioweb.json_response({"error": "Discord wouldn't let me create the channels."}, status=502)
    save_data(DATA)
    await send_log(
        guild, store, f"⚙️ Log channels auto-created from the website ({len(created)} new).",
        category="web", actor=_web_actor(request, guild),
    )
    return _aioweb.json_response(
        {"ok": True, "created": len(created), "kept": len(kept), "settings": _settings_list(guild, store)}
    )


async def _h_web_member_add(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad request."}, status=400)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _aioweb.json_response({"error": "Pick a member."}, status=400)
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.HTTPException:
            return _aioweb.json_response({"error": "That member isn't in this server."}, status=400)
    store = guild_store(DATA, guild.id)
    members = store.setdefault("web_members", [])
    if uid not in {int(x) for x in members}:
        members.append(uid)
        save_data(DATA)
        await send_log(
            guild, store, f"🌐 Gave **{member.display_name}** website access.",
            category="web", actor=_web_actor(request, guild),
        )
    return _aioweb.json_response({"ok": True, "settings": _settings_list(guild, store)})


async def _h_web_member_remove(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        body = await request.json()
        uid = int(body.get("user_id"))
    except Exception:
        return _aioweb.json_response({"error": "Bad request."}, status=400)
    store = guild_store(DATA, guild.id)
    members = store.setdefault("web_members", [])
    new = [x for x in members if int(x) != uid]
    if len(new) != len(members):
        store["web_members"] = new
        save_data(DATA)
        m = guild.get_member(uid)
        await send_log(
            guild, store, f"🌐 Removed website access from **{m.display_name if m else uid}**.",
            category="web", actor=_web_actor(request, guild),
        )
    return _aioweb.json_response({"ok": True, "settings": _settings_list(guild, store)})


async def _h_settings(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    return _aioweb.json_response({"settings": _settings_list(guild, store)})


async def _h_setting_set(request):
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    try:
        body = await request.json()
    except Exception:
        return _aioweb.json_response({"error": "Bad request."}, status=400)
    key = body.get("key")
    value = body.get("value")
    if key not in CONFIG_SETTINGS:
        return _aioweb.json_response({"error": "Unknown setting."}, status=400)
    label, skey, kind = CONFIG_SETTINGS[key]
    store = guild_store(DATA, guild.id)

    if value in (None, "", "none"):
        store[skey] = None
        new_obj = None
    else:
        try:
            vid = int(value)
        except (TypeError, ValueError):
            return _aioweb.json_response({"error": "Bad value."}, status=400)
        obj = guild.get_channel(vid) if kind == "channel" else guild.get_role(vid)
        if obj is None:
            return _aioweb.json_response({"error": f"That {kind} no longer exists."}, status=400)
        store[skey] = vid
        new_obj = obj
    save_data(DATA)
    await send_log(
        guild, store,
        f"⚙️ **{label}** set to {new_obj.mention if new_obj else '*cleared*'}.",
        category="web", actor=_web_actor(request, guild),
    )
    return _aioweb.json_response({"ok": True, "settings": _settings_list(guild, store)})


async def _h_welcome_test(request):
    """Post the welcome card to the configured welcome channel right now, so the
    user can verify it works without waiting for a real join (and without the
    members intent). Surfaces the real error if the send fails."""
    guild = await _req_guild(request)
    if guild is None:
        return _aioweb.json_response({"error": "Guild not found."}, status=404)
    ok, code, msg = await _setup_gate(request, guild)
    if not ok:
        return _aioweb.json_response({"error": msg}, status=code)
    store = guild_store(DATA, guild.id)
    wid = store.get("welcome_channel_id")
    if not wid:
        return _aioweb.json_response({"error": "No welcome channel is set yet - pick one above first."}, status=400)
    ch = guild.get_channel(wid)
    if ch is None:
        return _aioweb.json_response({"error": "That welcome channel was deleted - pick a new one."}, status=400)
    member = _web_actor(request, guild) or guild.me
    if member is None:
        return _aioweb.json_response({"error": "Couldn't resolve a member to preview with."}, status=400)
    try:
        await _send_card(
            ch, [welcome_embed(guild, member)],
            header=member.mention,
            allowed_mentions=discord.AllowedMentions(users=False),
        )
    except discord.Forbidden:
        return _aioweb.json_response(
            {"error": f"I can't post in #{ch.name} - give me Send Messages + Embed Links there."}, status=400)
    except discord.HTTPException:
        return _aioweb.json_response({"error": "Discord rejected the message - try again."}, status=502)
    await send_log(guild, store, f"🌐 Test welcome posted to {ch.mention} from the website.",
                   category="web", actor=member if member is not guild.me else None)
    return _aioweb.json_response({"ok": True, "channel": ch.name})


_web_started = False


async def _h_stats(request):
    """Public aggregate stats (counts only) for the Guard status bot to poll.
    No auth on purpose: it exposes totals, never names, IDs or member data."""
    active = [g for g in all_guilds() if guild_active(g.id)]
    members = sum(g.member_count or 0 for g in active)
    tickets = 0
    wars = 0
    for g in active:
        store = guild_store(DATA, g.id)
        tickets += sum(1 for o in store.get("orders", []) if o.get("status", "open") == "open")
        wars += sum(1 for w in store.get("wars", []) if w.get("active", True))
    try:
        _pc = pool_overview().get("counts", {})
    except Exception:  # never let the public status endpoint 500 over pool stats
        _pc = {}
    payload = {
        "online": True,
        "name": "Gang Manager",
        "servers": len(active),
        "members": members,
        "wars": wars,
        "tickets": tickets,
        "bots": 1 + int(_pc.get("online", 0)),     # main bot + online pool bots
        "bots_total": 1 + int(_pc.get("total", 0)),
        "fleet_servers": int(_pc.get("serving", 0)),  # servers the pool bots serve
        "ts": int(time.time()),
    }
    # Per-bot detail (names + off-reasons) only for the staff bot, which signs its
    # poll with the shared secret - the public endpoint still exposes counts only.
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if secret and request.headers.get("X-Guard-Token", "") == secret:
        try:
            fleet = [{
                "name": (str(bot.user.name) if getattr(bot, "user", None) else "Gang Manager"),
                "role": "distributor",
                "online": bool(bot.is_ready()) and not MAIN_OFF["v"],
                "off": MAIN_OFF["v"],
                "off_reason": "turned off (maintenance)" if MAIN_OFF["v"] else "",
                "ping": smoothed_latency_ms(bot),
            }]
            for b in pool_overview().get("bots", []):
                fleet.append({
                    "name": b.get("name") or "bot",
                    "role": "pool",
                    "online": bool(b.get("online")),
                    "off": bool(b.get("off")),
                    "off_reason": b.get("off_reason") or "",
                    "ping": b.get("latency_ms"),
                })
            payload["fleet"] = fleet
        except Exception:  # noqa: BLE001 - never 500 the status endpoint
            pass
    resp = _aioweb.json_response(payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# Admin site - owner + authorised staff: subscriptions, bans, shop log, staff
# --------------------------------------------------------------------------- #

# RAW string on purpose (served byte-for-byte): a plain string would let Python
# rewrite JS escapes like "\n", which breaks the script. See _PAGE_HTML.
_ADMIN_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Gang Manager - Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@600;700&family=Oswald:wght@500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#07070a;--card:rgba(20,20,26,.72);--line:rgba(255,255,255,.07);--text:#f2f2f6;--mute:#9a9aa8;--brand:#2f6bff;--brand2:#5b93ff;--acc:#2f6bff;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--blue:#60a5fa;--purple:#a78bfa;--r:4px}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--text);min-height:100vh;font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;background:radial-gradient(900px 520px at 12% -8%,rgba(255,255,255,.22),transparent 60%),radial-gradient(760px 520px at 102% 2%,rgba(130,20,70,.20),transparent 55%),radial-gradient(640px 640px at 50% 118%,rgba(60,10,36,.28),transparent 62%);animation:drift 20s ease-in-out infinite alternate}
body::after{content:"";position:fixed;inset:-40%;z-index:-1;pointer-events:none;opacity:.55;background:conic-gradient(from 0deg at 50% 50%,rgba(255,255,255,.20),rgba(120,20,90,.05),rgba(60,10,44,.18),rgba(255,255,255,.12),rgba(255,255,255,.20));filter:blur(64px);animation:aurora 26s linear infinite}
.aura{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}
.aura i{position:absolute;border-radius:50%;filter:blur(80px);opacity:.38;animation:aurafloat 22s ease-in-out infinite}
.aura i:nth-child(1){width:52vw;height:52vw;left:-12vw;top:-18vw;background:radial-gradient(circle,#5a5a66,transparent 65%)}
.aura i:nth-child(2){width:44vw;height:44vw;right:-10vw;top:22vh;background:radial-gradient(circle,#8a8a98,transparent 65%);animation-delay:-7s}
.aura i:nth-child(3){width:60vw;height:60vw;left:20vw;bottom:-30vw;background:radial-gradient(circle,#2e2e38,transparent 65%);animation-delay:-13s}
@media(prefers-reduced-motion:reduce){.aura i,body::after{animation:none}}
@keyframes drift{to{transform:translate3d(0,-14px,0) scale(1.04)}}
@keyframes aurora{to{transform:rotate(360deg)}}
@keyframes aurafloat{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(4vw,3vh) scale(1.08)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes shimmer{100%{background-position:-200% 0}}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
@keyframes pop{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
a{text-decoration:none;color:inherit}
::selection{background:rgba(255,255,255,.35)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:8px}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(7,7,10,.92),rgba(7,7,10,.72));backdrop-filter:blur(12px)}
.logo{width:56px;height:56px;border-radius:10px;display:grid;place-items:center;overflow:visible;background:transparent;border:none;box-shadow:none;padding:0}
.logo img{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;display:block;filter:drop-shadow(0 0 11px rgba(47,107,255,.55))}
.brand{font-family:"Space Grotesk",sans-serif;font-size:19px;font-weight:700;letter-spacing:.5px;background:linear-gradient(90deg,#fff,#e8e8ee 40%,#b9b9c4 70%,#fff);background-size:200% auto;-webkit-background-clip:text;background-clip:text;color:transparent;flex:1;animation:sheen 6s linear infinite}
@keyframes sheen{to{background-position:200% center}}
.brand small{font-family:"Inter";font-weight:500;font-size:12px;color:var(--mute);letter-spacing:0;display:block;margin-top:-2px;-webkit-text-fill-color:var(--mute)}
.kbtn{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--mute);border:1px solid var(--line);background:rgba(255,255,255,.04);padding:8px 13px;border-radius:10px;cursor:pointer;transition:.15s}
.kbtn:hover{border-color:rgba(255,255,255,.35);color:var(--text);background:rgba(255,255,255,.08)}
.kbtn .sicon{font-size:15px}
.kbtn kbd{font-family:inherit;font-size:11px;background:rgba(255,255,255,.1);border-radius:5px;padding:1px 6px;margin-left:2px}
.userchip{display:flex;align-items:center;gap:10px;padding:5px 7px 5px 5px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.05);transition:.15s}
.userchip:hover{border-color:rgba(255,255,255,.25)}
.uavwrap{position:relative;width:32px;height:32px;flex:none}.uavwrap>*{position:absolute;inset:0;width:32px;height:32px;border-radius:50%}
.uavwrap .uav{object-fit:cover;box-shadow:0 0 0 2px rgba(255,255,255,.14)}
.uinitial{display:grid;place-items:center;font-weight:800;font-size:13px;color:#15151b;background:linear-gradient(135deg,#c9c9d2,#fff);box-shadow:0 0 0 2px rgba(255,255,255,.14)}
.uinfo{display:flex;flex-direction:column;line-height:1.2;padding-right:6px}
.uname{font-size:13px;font-weight:700;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.urole{font-size:10px;color:var(--mute);text-transform:uppercase;letter-spacing:.6px;font-weight:700}
.urole.owner{background:linear-gradient(90deg,#ffd479,#fff1c2);-webkit-background-clip:text;background-clip:text;color:transparent}
header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.22),rgba(130,20,70,.4),transparent)}
.wrap{max-width:900px;margin:0 auto;padding:20px 18px 70px}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--text);padding:9px 14px;border-radius:11px;transition:.16s ease}
button:hover{border-color:rgba(255,255,255,.55);background:rgba(255,255,255,.1);transform:translateY(-1px)}
button:active{transform:translateY(0)}
button:disabled{opacity:.5;cursor:default;transform:none}
button.brand-btn{background:linear-gradient(135deg,var(--brand),#3a3a42);border-color:transparent;color:#fff;font-weight:700;box-shadow:0 8px 24px rgba(255,255,255,.1)}
button.ghost{background:transparent}
button.danger{border-color:rgba(239,68,68,.3);color:#ffb0b0;background:transparent}
button.danger:hover{background:var(--bad);border-color:var(--bad);color:#fff}
button.sm{padding:7px 11px;font-size:12.5px;border-radius:9px}
button.pur{border-color:rgba(167,139,250,.35);color:#d4c6ff;background:rgba(167,139,250,.08)}
button.on{border-color:var(--ok);color:#86efac;background:rgba(34,197,94,.1)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:12px;margin:4px 0 16px}
.stat{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 16px 14px;backdrop-filter:blur(10px);animation:fadeUp .4s ease both;transition:.18s}
.stat:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.2)}
.stat::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--brand2);opacity:.85}
.stat[data-k=active]::before{background:var(--ok)}.stat[data-k=life]::before{background:var(--purple)}.stat[data-k=bans]::before{background:var(--bad)}.stat[data-k=members]::before{background:var(--blue)}.stat[data-k=staff]::before{background:var(--warn)}
.stat .ic{position:absolute;top:12px;right:13px;font-size:15px;opacity:.55}
.stat .n{font-size:27px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.stat .l{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.6px;margin-top:5px}
.tabs{display:inline-flex;gap:4px;padding:4px;border-radius:14px;background:rgba(255,255,255,.04);border:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
.tab{padding:8px 18px;border-radius:10px;cursor:pointer;color:var(--mute);font-weight:600;transition:.18s;user-select:none}
.tab:hover{color:var(--text)}
.tab.active{background:linear-gradient(135deg,var(--brand),#3a3a42);color:#fff;box-shadow:0 6px 18px rgba(255,255,255,.2)}
.bd{padding:15px 18px}
.bdbar{display:flex;height:11px;border-radius:999px;overflow:hidden;background:rgba(255,255,255,.05);gap:2px}
.bdbar i{display:block;transition:width .6s cubic-bezier(.22,1,.36,1)}
.bdbar .sa{background:linear-gradient(90deg,#22c55e,#16a34a)}.bdbar .sl{background:linear-gradient(90deg,#a78bfa,#7c5cff)}.bdbar .se{background:linear-gradient(90deg,#ef4444,#b91c1c)}.bdbar .sn{background:rgba(255,255,255,.12)}
.bdleg{display:flex;gap:16px;flex-wrap:wrap;margin-top:11px;font-size:12px;color:var(--mute)}
.bdleg span{display:inline-flex;align-items:center;gap:6px}
.bdleg b{color:var(--text)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.toolbar input{flex:1;min-width:170px}
.toolbar select{font:inherit;padding:10px 13px;border-radius:11px;border:1px solid var(--line);background:rgba(0,0,0,.3);color:var(--text)}
.toolbar select:focus{outline:none;border-color:rgba(255,255,255,.55)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.fchip{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--mute);font-size:12.5px;font-weight:600;cursor:pointer;transition:.15s;user-select:none}
.fchip:hover{border-color:rgba(255,255,255,.4);color:var(--text)}
.fchip.on{background:linear-gradient(135deg,var(--brand),#3a3a42);color:#fff;border-color:transparent}
.fchip .cnt{font-size:11px;background:rgba(0,0,0,.25);padding:1px 7px;border-radius:999px}
.fchip.on .cnt{background:rgba(255,255,255,.22)}
.card{position:relative;background:rgba(14,14,20,.62);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:14px;backdrop-filter:blur(10px);transition:.2s}
#subsList .card,#bansList .rowline,#staffList .rowline{animation:fadeUp .32s ease both}
#subsList .card:hover{border-color:rgba(255,255,255,.18);box-shadow:0 18px 50px -20px rgba(130,20,70,.5),0 0 0 1px rgba(255,255,255,.04)}
.card h3{margin:0 0 4px;font-size:15px;font-weight:700}
.card .hint{color:var(--mute);font-size:13px;margin:0 0 14px}
.srvhead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.srvname{font-weight:700;font-size:15.5px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.srvname img{width:26px;height:26px;border-radius:8px}
.acts{display:flex;gap:6px;flex-wrap:wrap}
.badge{font-size:11px;font-weight:700;padding:2px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.5px}
.badge.live{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.4)}
.badge.pur{background:rgba(167,139,250,.15);color:#cdbcff;border:1px solid rgba(167,139,250,.4)}
.badge.red{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.4)}
.badge.off{background:rgba(255,255,255,.05);color:var(--mute);border:1px solid var(--line)}
.botin{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;display:inline-flex;align-items:center;gap:4px}
.botin.yes{background:rgba(34,197,94,.12);color:#86efac}
.botin.no{background:rgba(239,68,68,.14);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}
.botin.pool{background:rgba(167,139,250,.15);color:#cdbcff;border:1px solid rgba(167,139,250,.35)}
.botin.wait{background:rgba(245,158,11,.14);color:#fcd34d;border:1px solid rgba(245,158,11,.35)}
.botin.off{background:rgba(255,255,255,.05);color:var(--mute)}
.poolhead{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
.poolsw{display:inline-flex;align-items:center;gap:10px;padding:7px 14px 7px 7px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--text);font-weight:700;font-size:13px;cursor:pointer;font-family:inherit}
.poolsw .knob{width:36px;height:20px;border-radius:999px;background:rgba(255,255,255,.14);position:relative;transition:.2s;pointer-events:none}
.poolsw .knob:after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:#cfcfd6;transition:.2s}
.poolsw span{pointer-events:none}
.poolsw.on{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.10)}
.poolsw.on .knob{background:linear-gradient(90deg,#22c55e,#4ade80)}
.poolsw.on .knob:after{left:19px;background:#fff}
.poolstats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:14px}
.pst{background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px;padding:10px 6px;text-align:center}
.pst b{display:block;font-size:20px;margin-top:2px}.pst small{color:var(--mute);font-size:11px}
@media(max-width:640px){.poolstats{grid-template-columns:repeat(3,1fr)}}
.poolwarn{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);color:#fca5a5;font-size:13px}
#poolTokens{width:100%;box-sizing:border-box;background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:12px;color:var(--text);padding:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;resize:vertical;min-height:92px}
.presults{margin-top:10px;display:grid;gap:6px}
.pres{font-size:13px;padding:8px 10px;border-radius:9px;background:rgba(255,255,255,.03);border:1px solid var(--line)}
.pres.ok{border-color:rgba(34,197,94,.35)}.pres.bad{border-color:rgba(239,68,68,.35);color:#fca5a5}
.howto{margin-top:14px;font-size:13px;color:var(--mute)}.howto summary{cursor:pointer;font-weight:700;color:var(--text)}.howto ol{margin:8px 0 0 18px;line-height:1.75}.howto b{color:var(--text)}
.bcard{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px;background:rgba(255,255,255,.025)}
.bcard.boff{opacity:.55}
.bhead{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.bav{position:relative;width:42px;height:42px;border-radius:12px;background:rgba(255,255,255,.06);display:grid;place-items:center;flex:none;font-size:20px}
.bav img{width:42px;height:42px;border-radius:12px;object-fit:cover}
.bdot{position:absolute;right:-3px;bottom:-3px;width:13px;height:13px;border-radius:50%;border:2px solid #0d0d12;background:#6b7280}
.bdot.on{background:#22c55e;box-shadow:0 0 8px rgba(34,197,94,.7)}.bdot.wait{background:#f59e0b}.bdot.off{background:#ef4444}
.bmain{flex:1;min-width:170px}.bname{font-weight:700}.bnum{color:var(--mute);font-weight:600;font-size:12px}
.bserve{margin-left:auto}
.bwarn{margin-top:10px;font-size:12.5px;color:#fcd34d;display:grid;gap:3px}
.bacts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.botcap{font-size:11px;font-weight:800;padding:1px 8px;border-radius:999px;background:rgba(255,255,255,.07);color:#cfcfd8;border:1px solid var(--line);vertical-align:middle;margin-left:4px}
.botcap.full{background:rgba(239,68,68,.16);color:#fca5a5;border-color:rgba(239,68,68,.4)}
.bservers{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.botx{border:none;background:transparent;color:inherit;opacity:.6;cursor:pointer;font-size:13px;line-height:1;padding:0 2px;font-weight:800}
.botx:hover{opacity:1;color:#fff;transform:none;background:transparent}
.botlink{color:#fcd34d;text-decoration:underline;font-weight:700}
.bstrain{display:flex;align-items:center;gap:10px;margin-top:10px}
.sbar{flex:1;height:7px;border-radius:5px;background:rgba(255,255,255,.08);overflow:hidden;min-width:80px}
.sbar i{display:block;height:100%;border-radius:5px;background:#22c55e;transition:width .4s}
.slab{font-size:11.5px;color:var(--mute);white-space:nowrap}
.bstrain.idle .sbar i{background:#6b7280}.bstrain.light .sbar i{background:#22c55e}.bstrain.mod .sbar i{background:#f59e0b}.bstrain.heavy .sbar i{background:#f97316}.bstrain.full .sbar i{background:#ef4444}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px 22px;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.kv{display:flex;flex-direction:column;gap:2px;min-width:0}
.kv .k{font-size:10.5px;color:var(--mute);text-transform:uppercase;letter-spacing:.6px}
.kv .v{font-size:13.5px;font-weight:500;word-break:break-word}
.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mute)}
.copy{cursor:pointer;transition:.12s}.copy:hover{color:var(--text) !important;text-decoration:underline}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--mute);word-break:break-all}
.mute{color:var(--mute)}
.rev{font-size:13px;color:var(--mute);margin:0 0 14px}.rev b{color:#86efac;font-weight:700}
.pad{padding:22px;text-align:center;color:var(--mute)}
input{font:inherit;padding:10px 13px;border-radius:11px;border:1px solid var(--line);background:rgba(0,0,0,.3);color:var(--text);width:100%}
input:focus{outline:none;border-color:rgba(255,255,255,.55);background:rgba(255,255,255,.06)}
.frm{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.frm input{flex:1;min-width:150px}
.rowline{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:13px 0;border-bottom:1px solid var(--line)}
.rowline:last-child{border-bottom:0}
.rowmain .nm{font-weight:600}.rowmain .mt{font-size:12px;color:var(--mute);margin-top:3px}
.bcount{font-size:12.5px;color:var(--mute);margin:0 0 12px}.bcount b{color:var(--text)}
.brow{display:flex;align-items:center;gap:13px;padding:14px 0;border-bottom:1px solid var(--line);animation:fadeUp .3s ease both}
.brow:last-child{border-bottom:0}
.bavwrap{position:relative;width:44px;height:44px;flex:none}.bavwrap>*{position:absolute;inset:0}
.bav{width:44px;height:44px;border-radius:50%;object-fit:cover;background:#222;box-shadow:0 0 0 2px rgba(255,255,255,.08)}
.binitial{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:17px;color:#fff;background:linear-gradient(135deg,#ef4444,#7f1d1d);box-shadow:0 0 0 2px rgba(239,68,68,.25)}
.bmain{flex:1;min-width:0}.bname{font-weight:700;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bsub{font-size:12px;color:var(--mute);margin-top:3px}
.breason{display:inline-block;margin-top:8px;font-size:12px;color:#ffc9c9;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);padding:3px 10px;border-radius:8px}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;color:var(--mute);font-weight:600;padding:9px 10px;border-bottom:1px solid var(--line);font-size:10.5px;text-transform:uppercase;letter-spacing:.5px}
.tbl td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
.sk{height:78px;border-radius:16px;margin-bottom:12px;background:linear-gradient(100deg,rgba(255,255,255,.04) 30%,rgba(255,255,255,.09) 50%,rgba(255,255,255,.04) 70%);background-size:200% 100%;animation:shimmer 1.3s linear infinite}
.gate{min-height:70vh;display:grid;place-items:center}
.gate .card{max-width:420px;text-align:center}
.gate .big{font-size:42px}
.gate h2{margin:12px 0 6px}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:#1a1a22;border:1px solid var(--line);color:var(--text);padding:11px 18px;border-radius:12px;opacity:0;pointer-events:none;transition:.2s;font-weight:600;font-size:13.5px;box-shadow:0 12px 40px rgba(0,0,0,.5);display:flex;align-items:center;gap:8px}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.bad{border-color:rgba(239,68,68,.4);color:#ffb0b0}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;animation:pulse 1.4s ease-in-out infinite}
.cmdk{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);backdrop-filter:blur(6px);display:none;align-items:flex-start;justify-content:center;padding-top:12vh}
.cmdk.show{display:flex}
.cmdkbox{width:min(560px,92vw);background:#121218;border:1px solid rgba(255,255,255,.12);border-radius:18px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.6);animation:pop .14s ease}
.cmdkbox input{border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;padding:16px 18px;font-size:15px}
.cmdkList{max-height:50vh;overflow:auto;padding:6px}
.ci{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;font-size:14px}
.ci:hover,.ci.sel{background:rgba(255,255,255,.08)}
.ci .cig{font-size:15px;width:20px;text-align:center}
.ci .cis{margin-left:auto;font-size:11px;color:var(--mute)}
.cmdkFoot{padding:9px 14px;border-top:1px solid var(--line);font-size:11px;color:var(--mute);display:flex;gap:14px}
.cmdkFoot kbd{background:rgba(255,255,255,.1);border-radius:5px;padding:1px 6px;font-family:inherit}
.confirm{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.62);backdrop-filter:blur(7px);display:none;align-items:center;justify-content:center;padding:20px}
.confirm.show{display:flex}
.confirmbox{width:min(400px,94vw);background:linear-gradient(180deg,#191921,#131319);border:1px solid rgba(255,255,255,.12);border-radius:20px;padding:24px;box-shadow:0 30px 80px rgba(0,0,0,.65);animation:pop .15s ease;text-align:center}
.confirmbox .ci2{font-size:38px;line-height:1;margin-bottom:10px}
.confirmbox h4{margin:0 0 7px;font-size:17px;font-weight:800}
.confirmbox p{margin:0 0 20px;color:var(--mute);font-size:13.5px;line-height:1.5}
.confirmbtns{display:flex;gap:10px}.confirmbtns button{flex:1;padding:11px}
.danger-solid{background:linear-gradient(135deg,#ef4444,#b91c1c) !important;border-color:transparent !important;color:#fff !important;box-shadow:0 8px 24px rgba(239,68,68,.25) !important}

/* ===== tactical-blue system (matches the landing) ===== */
@keyframes gridpan{to{background-position:0 46px}}
body::before{display:block!important;background:transparent!important;background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px)!important;background-size:46px 46px!important;inset:0!important;filter:none!important;opacity:1!important;-webkit-mask:radial-gradient(135% 90% at 50% 0,#000 32%,transparent 80%);mask:radial-gradient(135% 90% at 50% 0,#000 32%,transparent 80%);animation:gridpan 60s linear infinite}
body::after{content:""!important;position:fixed!important;inset:0!important;z-index:-1!important;pointer-events:none!important;background:radial-gradient(90% 55% at 50% -6%,rgba(47,107,255,.13),transparent 60%)!important;filter:none!important;opacity:1!important;animation:none!important;transform:none!important}
.aura,.aurora{display:none!important}
body{background:#09090a!important}
h1,h2,h3,.hdr h1,.brandname,.logo-txt,.brand,.gatecard h2{font-family:'Oswald',Inter,sans-serif!important;text-transform:uppercase;letter-spacing:.5px;-webkit-text-fill-color:currentColor;background:none!important;color:var(--text)}
.brand-btn,button.brand-btn{background:linear-gradient(180deg,var(--brand2),var(--brand))!important;border:1px solid var(--brand2)!important;color:#fff!important;border-radius:4px!important;font-family:'JetBrains Mono',ui-monospace,monospace!important;text-transform:uppercase;letter-spacing:.8px;font-size:12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.22),0 10px 26px rgba(47,107,255,.3)!important}
.brand-btn:hover,button.brand-btn:hover{filter:brightness(1.08)}
.tab{border-radius:4px!important;font-family:'JetBrains Mono',ui-monospace,monospace;text-transform:uppercase;letter-spacing:.6px;font-size:12px}
.tab.active{background:var(--brand)!important;color:#fff!important;box-shadow:0 6px 18px rgba(47,107,255,.4)!important}
.card,.gatecard,.stat,.statc{border-radius:4px!important}
.card{border-color:rgba(255,255,255,.08)!important}
.pill,.chip,.badge{border-radius:4px!important}
input,textarea,select{border-radius:4px!important}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--brand)!important;box-shadow:0 0 0 3px rgba(47,107,255,.2)!important}
button.ghost,.ghost{border-radius:4px!important}
button.ghost:hover,.ghost:hover{border-color:var(--brand)!important;color:var(--text)}
.hint,.sub,.mute,.toolbar .count,.kcap{font-family:'JetBrains Mono',ui-monospace,monospace;letter-spacing:.3px}
a.link,.link{color:var(--brand2)}
::selection{background:var(--brand);color:#fff}
::-webkit-scrollbar-thumb{background:rgba(47,107,255,.35)!important}
/* login gate: kill silver gradient + rounded glow frame */
#gate.gate,#gate{background:#09090a!important}
#gate.gate::before,#gate.gate::after,.gatecard::before,.gatecard::after{display:none!important}
.gatecard{border-radius:4px!important;border:1px solid rgba(255,255,255,.1)!important}

</style></head><body>
<div class="aura"><i></i><i></i><i></i></div>
<header>
  <div class="logo"><img src="/gm-logo.png" alt="Gang Manager" onerror="this.style.display='none'"></div>
  <div class="brand">GANG MANAGER<small>Admin Control</small></div>
  <div class="kbtn" id=cmdkOpen><span class=sicon>⌕</span>Search<kbd>⌘K</kbd></div>
  <div class="userchip" id=uchip style=display:none>
    <div class=uavwrap><div class=uinitial id=uinitial>·</div><img class=uav id=uav alt=""></div>
    <div class=uinfo><span class=uname id=uname>Account</span><span class=urole id=urole>Staff</span></div>
  </div>
  <a href="/logout"><button class="ghost sm">Log out</button></a>
</header>
<div id=gate class="wrap gate"></div>
<div id=app class=wrap style=display:none>
  <div class=stats>
    <div class=stat data-k=servers><div class=ic>🌐</div><div class=n id=statServers>0</div><div class=l>Servers</div></div>
    <div class=stat data-k=active><div class=ic>✅</div><div class=n id=statActive>0</div><div class=l>Active</div></div>
    <div class=stat data-k=life><div class=ic>♾️</div><div class=n id=statLife>0</div><div class=l>Lifetime</div></div>
    <div class=stat data-k=members><div class=ic>👥</div><div class=n id=statMembers>0</div><div class=l>Members</div></div>
    <div class=stat data-k=bans><div class=ic>⛔</div><div class=n id=statBans>0</div><div class=l>Banned</div></div>
    <div class=stat data-k=staff><div class=ic>🛡️</div><div class=n id=statStaff>0</div><div class=l>Staff</div></div>
  </div>
  <div class=tabs>
    <div class="tab active" id=tab-subs>Subscriptions</div>
    <div class=tab id=tab-bans>Bans</div>
    <div class=tab id=tab-shop>Shop log</div>
    <div class=tab id=tab-staff>Staff access</div>
    <div class=tab id=tab-main style=display:none>🏠 Main Discord</div>
    <div class=tab id=tab-bots style=display:none>🤖 Bots</div>
    <div class=tab id=tab-log style=display:none>📋 Changelog</div>
    <div class=tab id=tab-rules style=display:none>🛡️ Rules</div>
    <div class=tab id=tab-audit style=display:none>📜 Audit</div>
  </div>
  <div id=pan-subs>
    <div class="card bd" id=subHealth style=display:none></div>
    <div class=toolbar>
      <input id=subSearch placeholder="Search servers by name or ID…" autocomplete=off>
      <select id=subSort><option value=members>Most members</option><option value=days>Days left</option><option value=name>Name A-Z</option><option value=created>Newest server</option></select>
      <button class="ghost sm" id=liveBtn>⟳ Live</button>
      <button class="ghost sm" id=subRefresh>↻ Refresh</button>
    </div>
    <div class=filters id=subFilters></div>
    <details class=card style="padding:14px 18px"><summary style="cursor:pointer;font-weight:700;font-size:14px">➕ Add time to a server by ID</summary>
      <p class=hint style="margin-top:10px">For a server the bot isn't in yet, or a custom amount. Positive adds days, negative removes.</p>
      <div class=frm><input id=cdGuild placeholder="Server (guild) ID"><input id=cdDays placeholder="Days (e.g. 30)" style="max-width:150px" inputmode=numeric><button class="brand-btn" id=cdBtn>Apply</button></div>
    </details>
    <div id=subsList></div>
  </div>
  <div id=pan-bans style=display:none>
    <div class=card><h3>Ban a Discord user</h3><p class=hint>A ban is global - the bot ignores them in every server: no commands, buttons, tickets or menus.</p>
      <div class=frm><input id=banId placeholder="Discord user ID"><input id=banReason placeholder="Reason (optional)"><button class="danger" id=banBtn>Ban</button></div>
    </div>
    <div class=card><div class=toolbar style=margin-bottom:4px><input id=banSearch placeholder="Search bans…" autocomplete=off></div><div id=bansList></div></div>
  </div>
  <div id=pan-shop style=display:none>
    <style>
    #shopCard .shopstats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 4px}
    #shopCard .sstat{display:flex;gap:11px;align-items:center;background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
    #shopCard .sstat .si{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-size:16px;background:rgba(47,107,255,.12);border:1px solid rgba(47,107,255,.25);flex:none}
    #shopCard .sstat .n{font-family:"Space Grotesk",sans-serif;font-weight:700;font-size:18px;color:#f0f0f4;line-height:1.1}
    #shopCard .sstat .k{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
    #shopCard .shoptools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0 12px}
    #shopCard .shoptools input{flex:1;min-width:180px}
    #shopCard .chips{display:flex;gap:6px}
    #shopCard .chip{padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--mute);font:inherit;font-size:13px;cursor:pointer}
    #shopCard .chip.on{background:var(--brand,#2f6bff);border-color:var(--brand,#2f6bff);color:#fff}
    #shopCard .shopcount{font-size:12px;color:var(--mute);margin:0 2px 10px}
    #shopCard .shoprow{display:flex;gap:12px;align-items:center;padding:11px 13px;border:1px solid var(--line);border-radius:11px;margin-bottom:8px;background:rgba(255,255,255,.015)}
    #shopCard .shoprow:hover{border-color:rgba(47,107,255,.35);background:rgba(47,107,255,.04)}
    #shopCard .skind{width:40px;height:40px;border-radius:10px;display:grid;place-items:center;font-size:18px;flex:none;background:rgba(47,107,255,.12);border:1px solid rgba(47,107,255,.25)}
    #shopCard .skind.renewal{background:rgba(150,150,160,.1);border-color:rgba(255,255,255,.14)}
    #shopCard .skind.test{background:rgba(234,179,8,.12);border-color:rgba(234,179,8,.3)}
    #shopCard .smain{flex:1;min-width:0}
    #shopCard .sl1{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
    #shopCard .semail{font-weight:600;color:#ececf1}
    #shopCard .sserver{font-size:12px;color:#bfc3cf;background:rgba(255,255,255,.05);border:1px solid var(--line);border-radius:6px;padding:2px 8px}
    #shopCard .sl2{font-size:12px;color:var(--mute);margin-top:5px;display:flex;gap:7px;flex-wrap:wrap;align-items:center}
    #shopCard .sl2 .mono{color:#aeb4c2}
    #shopCard .sl2 .dot{opacity:.4}
    #shopCard .samt{font-family:"Space Grotesk",sans-serif;font-weight:800;font-size:17px;color:#eaf0ff;text-align:right;flex:none;white-space:nowrap}
    #shopCard .samt small{display:block;font-size:10px;font-weight:600;color:var(--mute);letter-spacing:.5px}
    #shopCard .samt.mut{color:var(--mute);font-weight:600}
    @media(max-width:720px){#shopCard .shopstats{grid-template-columns:1fr 1fr}}
    #testBuyCard .tbrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
    #testBuyCard .tbrow input,#testBuyCard .tbrow select{flex:1;min-width:150px;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.28);color:#ececf1;font:inherit}
    #testBuyCard .tbrow input:focus,#testBuyCard .tbrow select:focus{outline:none;border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.22)}
    #testBuyCard .tbrow input#tbAmount{max-width:120px;flex:none}
    #testBuyCard .tbrow button{flex:none}
    </style>
    <div class=card id=testBuyCard style=display:none>
      <h3>🧪 Send a test purchase</h3>
      <p class=hint>Runs the real purchase flow for one user - DMs them a test invoice and posts the purchase log - with no charge and nothing recorded. The user must be in the support server. Use it to check the DM and logs look right.</p>
      <div class=tbrow>
        <input id=tbUser placeholder="Discord user ID" autocomplete=off inputmode=numeric>
        <select id=tbServer><option value="">No specific server</option></select>
        <input id=tbAmount placeholder="9.99" autocomplete=off inputmode=decimal>
        <button class=brand-btn id=tbSend>Send test</button>
      </div>
      <p class=hint id=tbMsg style=margin:9px 0 0></p>
    </div>
    <div class=card id=testLogCard style=display:none>
      <h3>🧪 Send test logs</h3>
      <p class=hint>Fires one sample of every admin log kind - purchases, subscriptions, sign-ins, security, moderation, staff, pool and servers - through the real path, so each lands in its log channel looking exactly like a live log, just with test info.</p>
      <button class=brand-btn id=tlSend>Send test logs</button>
      <p class=hint id=tlMsg style=margin:9px 0 0></p>
    </div>
    <div class=card id=shopCard><h3>Shop log</h3><p class=hint>Every purchase and renewal - email, server, IP and invoice captured at checkout.</p>
      <div class=shopstats id=shopStats></div>
      <div class=shoptools>
        <div class=chips id=shopChips><button class="chip on" data-sk=all>All</button><button class=chip data-sk=purchase>💳 Purchases</button><button class=chip data-sk=renewal>🔄 Renewals</button><button class=chip data-sk=test>🧪 Tests</button></div>
        <input id=shopSearch placeholder="Search email, IP, server or invoice…" autocomplete=off>
        <button class="ghost sm" id=csvBtn>⬇ CSV</button>
      </div>
      <div id=shopList></div>
    </div>
  </div>
  <div id=pan-staff style=display:none>
    <div class=card><h3>Authorised staff</h3><p class=hint>Staff you authorise here can open this admin site and manage subs, bans and the shop log. Only you (owner) can add or remove them.</p>
      <div class=frm id=staffForm style=display:none><input id=staffId placeholder="Discord user ID to authorise"><button class="brand-btn" id=staffBtn>Authorise</button></div>
      <p class=hint id=staffOwnerNote style=display:none>Only the bot owner can change who has access.</p>
    </div>
    <div class=card><div id=staffList></div></div>
  </div>
  <div id=pan-main style=display:none>
    <div class=card>
      <div class=poolhead>
        <div><h3 style=margin:0>🏠 Main Discord</h3><p class=hint style="margin:4px 0 0">Your hub / admin server - where the main bot and every pool (sub) bot sit so you can watch the whole fleet in one place.</p></div>
      </div>
      <div class=frm style="margin-top:10px;align-items:center;flex-wrap:wrap">
        <input id=mainHub placeholder="Hub server ID" autocomplete=off inputmode=numeric style="min-width:200px">
        <button class=ghost id=mainHubSave>Save</button>
      </div>
      <p class=hint id=mainHubMsg style="margin:8px 0 0"></p>
      <p class=hint style="margin:6px 0 0;opacity:.7">To get the ID: Discord → Settings → Advanced → turn on Developer Mode, then right-click your server → Copy Server ID.</p>
    </div>
    <div class=card>
      <div class=poolhead>
        <div><h3 style=margin:0>Sub-bot commands in the hub</h3><p class=hint style="margin:4px 0 0">Turn the pool (sub) bots' slash commands off <b>inside this hub server only</b>. Every customer's own server keeps working - this just stops a stack of pool bots all answering the same command here, so you drive the hub with the main bot.</p></div>
        <button class=poolsw id=hubCmdSwitch title="When off, the pool (sub) bots ignore their slash commands in the hub server only"><span class=knob></span><span id=hubCmdSwitchTxt>Sub-bot commands on</span></button>
      </div>
      <p class=hint id=hubCmdMsg style="margin:10px 0 0;opacity:.7">Takes effect instantly - no restart. The main bot's own commands here are not touched.</p>
    </div>
    <div class=card>
      <div class=poolhead>
        <div><h3 style=margin:0>📋 Admin logs</h3><p class=hint style="margin:4px 0 0">Your operator logs on the <b>staff bot</b> - purchases, subscriptions, admin sign-ins, security, bans, staff changes, pool actions and servers added/removed. Each one posts to its own channel.</p></div>
      </div>
      <button class=ghost id=mainLogSetup style="margin-top:12px">✨ Create the admin log channels</button>
      <p class=hint id=mainLogSetupMsg style="margin:8px 0 0"></p>
      <p class=hint style="margin:6px 0 0;opacity:.7">Builds a <b>📋 Gang Manager Logs</b> category on the staff bot with a channel per admin event, and skips any already set. The staff bot needs Manage Channels in its server (your support server).</p>
      <div style="margin-top:14px;padding-top:14px;border-top:1px solid rgba(255,255,255,.08)">
        <button class=ghost id=mainTestLogs>🧪 Send test logs</button>
        <p class=hint id=mainTestLogsMsg style="margin:8px 0 0"></p>
        <p class=hint style="margin:6px 0 0;opacity:.7">Fires one sample of every admin event into its channel - same depth as the real logs, just test info. Create the channels first.</p>
      </div>
    </div>
  </div>
  <div id=pan-bots style=display:none>
    <style>
    .fleetgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
    .fbot{border:1px solid rgba(255,255,255,.1);border-radius:10px;padding:14px;background:rgba(255,255,255,.02)}
    .fbh{display:flex;align-items:center;gap:9px;margin-bottom:6px}
    .fbh b{font-weight:700;font-size:14.5px}
    .fdot{width:9px;height:9px;border-radius:50%;background:#555;flex:none}
    .fdot.on{background:#35c06b;box-shadow:0 0 8px rgba(53,192,107,.6)}
    .fdot.off{background:#e5362b}.fdot.unk{background:#eaa12a}
    .ftag{margin-left:auto;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:#9a9aa6;border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:1px 7px}
    .fbs{font-size:12px;color:#9a9aa6;margin-bottom:11px;font-family:'JetBrains Mono',ui-monospace,monospace}
    .fbtns{display:flex;gap:7px;flex-wrap:wrap}
    @media(max-width:560px){.fleetgrid{grid-template-columns:1fr}}
    </style>
    <div class=card id=fleetCard>
      <h3>🛰️ Staff &amp; distribution</h3>
      <p class=hint>Your two lead bots - the <b>staff</b> bot (GM Support) and the <b>distribution</b> bot (the main service). The pool workers are the <b>sub-fleet</b> below.</p>
      <div class=fleetgrid>
        <div class=fbot>
          <div class=fbh><span class=fdot id=fStaffDot></span><b id=fStaffName>Staff bot</b><span class=ftag>Staff</span></div>
          <div class=fbs id=fStaffState>…</div>
          <div class=fbtns>
            <button class="ghost sm" data-fleet=staff data-act=off id=fStaffOff>⏻ Turn off</button>
            <button class="ghost sm" data-fleet=staff data-act=on id=fStaffOn style=display:none>▶ Turn on</button>
            <button class="ghost sm" data-fleet=staff data-act=restart>↻ Restart</button>
          </div>
        </div>
        <div class=fbot>
          <div class=fbh><span class=fdot id=fMainDot></span><b id=fMainName>Main bot</b><span class=ftag>Distribution</span></div>
          <div class=fbs id=fMainState>…</div>
          <div class=fbtns>
            <button class="ghost sm" data-fleet=main data-act=off id=fMainOff>⏻ Turn off</button>
            <button class="ghost sm" data-fleet=main data-act=on id=fMainOn style=display:none>▶ Turn on</button>
            <button class="ghost sm" data-fleet=main data-act=restart>↻ Restart</button>
          </div>
        </div>
      </div>
    </div>
    <div class=card>
      <div class=poolhead>
        <div><h3 style=margin:0>🤖 Pool overview</h3><p class=hint style="margin:4px 0 0">Give every customer their own Gang Manager bot. Pool bots run the same code as your main bot, share the same data and website, and are managed right here.</p></div>
        <button class=poolsw id=poolSwitch title="When on, a newly subscribed server is given its own free pool bot instead of the main bot"><span class=knob></span><span id=poolSwitchTxt>Hand-outs off</span></button>
      </div>
      <div class=poolstats id=poolStats></div>
      <div class=poolwarn id=poolCrypto style=display:none></div>
    </div>
    <div class=card>
      <h3 style="margin:0 0 4px">🤖 Sub-fleet</h3><p class=hint style="margin:0 0 10px">The pool workers handed out to servers.</p>
      <div class=toolbar style=margin-bottom:8px><input id=poolSearch placeholder="Search bots or servers…" autocomplete=off><button class="ghost sm" id=poolRefresh>↻ Refresh</button><span style=flex:1></span><button class="ghost sm" id=poolAllRestart>🔄 Restart all</button><button class="ghost sm" id=poolAllOn>▶ Turn all on</button><button class="ghost sm" id=poolAllOff>⏻ Turn all off</button></div>
      <div id=poolList></div>
    </div>
    <div class=card>
      <h3>Add bots</h3>
      <p class=hint>Paste one or more bot tokens, one per line. Each is checked to be yours, given Gang Manager's avatar, icon and description, has its Server Members Intent switched on, and comes online straight away. Tokens are stored encrypted and never shown again.</p>
      <textarea id=poolTokens rows=4 placeholder="Paste bot tokens here - one per line" spellcheck=false autocomplete=off></textarea>
      <div class=frm style="margin-top:10px;align-items:center"><button class=brand-btn id=poolAddBtn>➕ Add bots</button><span class=hint id=poolAddMsg style=margin:0></span></div>
      <div id=poolResults></div>
      <details class=howto><summary>How to make a pool bot (about a minute each)</summary><ol>
        <li>Open <b>discord.com/developers/applications</b> → <b>New Application</b> → name it <b>Gang Manager</b> → Create.</li>
        <li>Go to the <b>Bot</b> tab → make sure <b>Public Bot</b> is on and <b>Requires OAuth2 Code Grant</b> is off.</li>
        <li>Still on <b>Bot</b> → <b>Reset Token</b> → confirm → <b>Copy</b>.</li>
        <li>Paste it in the box above. Everything else (avatar, icon, description, members intent) is set up for you.</li>
        <li>Make them a few at a time - creating lots of apps at once can get a Discord account flagged.</li>
      </ol></details>
    </div>
    <div class=card>
      <div class=poolhead>
        <div><h3 style=margin:0>Admin / hub server</h3><p class=hint style="margin:4px 0 0">Keep every bot in one server so you can watch the whole fleet. Bots sit here on top of the server they serve - they won’t leave it.</p></div>
      </div>
      <div class=frm style="margin-top:10px;align-items:center;flex-wrap:wrap">
        <input id=poolHub placeholder="Hub server ID" autocomplete=off inputmode=numeric style="min-width:200px">
        <button class=ghost id=poolHubSave>Save</button>
        <button class=brand-btn id=poolHubAdd style=display:none>🏠 Add all bots here</button>
      </div>
      <p class=hint id=poolHubMsg style="margin:8px 0 0"></p>
      <p class=hint style="margin:6px 0 0;opacity:.7">To get the ID: Discord → Settings → Advanced → turn on Developer Mode, then right-click your server → Copy Server ID.</p>
    </div>
    <style>
    #brandCard .bwrap{display:grid;grid-template-columns:1fr 340px;gap:18px;margin-top:14px;align-items:start}
    #brandCard textarea{width:100%;margin-top:10px;padding:11px 13px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.28);color:#ececf1;font:inherit;line-height:1.5;resize:vertical;min-height:74px}
    #brandCard textarea:focus{outline:none;border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.22)}
    .upzone{display:flex;align-items:center;gap:12px;padding:11px 13px;border:1px dashed rgba(255,255,255,.18);border-radius:10px;background:rgba(255,255,255,.02);cursor:pointer;transition:.16s;margin-bottom:10px}
    .upzone:hover,.upzone.drag{border-color:#2f6bff;background:rgba(47,107,255,.08)}
    .upzone.set{border-style:solid;border-color:#2f6bff}
    .upzone .uthumb{width:46px;height:46px;border-radius:8px;background:#000;border:1px solid rgba(255,255,255,.12);flex:none;display:grid;place-items:center;font-size:18px;color:#5b93ff;overflow:hidden}
    .upzone .uthumb img{width:100%;height:100%;object-fit:cover}
    .upzone .utxt{min-width:0;display:flex;flex-direction:column}
    .upzone .utt{font-weight:700;font-size:13.5px;color:#ececf1}
    .upzone .uts{font-size:12px;color:#9a9aa6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:'JetBrains Mono',ui-monospace,monospace}
    .brandrow{display:flex;align-items:center;gap:10px;margin-top:12px}
    .plabel{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:#9a9aa6;margin:0 0 7px}
    .dprev{border-radius:10px;overflow:hidden;background:#232428;border:1px solid rgba(255,255,255,.08);box-shadow:0 14px 40px rgba(0,0,0,.45)}
    .dprev .dbanner{height:94px;background:linear-gradient(135deg,#2f6bff,#0c0c10);background-size:cover;background-position:center}
    .dprev .dbody{padding:0 16px 16px}
    .dprev .dav{width:76px;height:76px;border-radius:50%;border:6px solid #232428;background:#000;margin-top:-40px;object-fit:cover}
    .dprev .dav.ph{display:grid;place-items:center;color:#5b93ff;font-size:26px}
    .dprev .dnm{font-weight:800;font-size:17px;margin-top:9px;display:flex;align-items:center;gap:7px;color:#f2f3f5}
    .dprev .dapp{font-size:9px;font-weight:700;background:#5865f2;color:#fff;padding:1px 5px;border-radius:4px;letter-spacing:.3px}
    .dprev .dsec{margin-top:13px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;letter-spacing:1px;color:#b5bac1;text-transform:uppercase}
    .dprev .dbio{margin-top:5px;font-size:13px;color:#dbdee1;line-height:1.5;white-space:pre-wrap;word-break:break-word}
    .dprev .dbio.empty{color:#72767d}
    .rollout{margin-top:14px;border-top:1px solid rgba(255,255,255,.08);padding-top:12px;display:none}
    .rollhead{display:flex;align-items:center;gap:10px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;letter-spacing:.5px;color:#9a9aa6;margin-bottom:9px}
    .rollbar{flex:1;height:5px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden}
    .rollbar i{display:block;height:100%;background:#2f6bff;width:0;transition:width .4s}
    .rolllist{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:6px;max-height:168px;overflow:auto}
    .rollitem{display:flex;align-items:center;gap:7px;font-size:12px;padding:5px 9px;border-radius:6px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)}
    .rollitem .rdot{width:7px;height:7px;border-radius:50%;flex:none}
    .rdot.ok{background:#35c06b}.rdot.wait{background:#eaa12a}.rdot.bad{background:#e5362b}.rdot.pend{background:#5b93ff;animation:bpulse 1.2s infinite}
    .rollitem b{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .rollitem em{font-style:normal;font-size:10px;color:#9a9aa6;font-family:'JetBrains Mono',ui-monospace,monospace}
    @keyframes bpulse{50%{opacity:.35}}
    @media(max-width:720px){#brandCard .bwrap{grid-template-columns:1fr}}
    </style>
    <div class=card id=brandCard>
      <h3>🎨 Fleet branding</h3>
      <p class=hint>One avatar, banner and bio pushed to <b>every bot</b> - main + all pool bots. Discord rate-limits avatar changes, so a big fleet rolls out gradually and some may need a re-apply.</p>
      <div class=bwrap>
        <div>
          <label class=upzone id=zoneAvatar><input type=file accept="image/png,image/jpeg,image/gif,image/webp" id=brandAvatar hidden><span class=uthumb id=avThumb>🖼️</span><span class=utxt><span class=utt>Avatar · pfp</span><span class=uts id=avName>Click or drop an image</span></span></label>
          <label class=upzone id=zoneBanner><input type=file accept="image/png,image/jpeg,image/gif,image/webp" id=brandBanner hidden><span class=uthumb id=bnThumb>🖼️</span><span class=utxt><span class=utt>Banner</span><span class=uts id=bnName>Click or drop an image</span></span></label>
          <textarea id=brandBio maxlength=400 rows=3 placeholder="Bio / About Me shown on every bot's profile…"></textarea>
          <div class=brandrow><span class=hint id=brandCount>0 / 400</span><span style=flex:1></span><button class=brand-btn id=brandApply>Apply to all bots</button></div>
        </div>
        <div>
          <p class=plabel>Live preview</p>
          <div class=dprev>
            <div class=dbanner id=pvBanner></div>
            <div class=dbody>
              <img class=dav id=pvAvatar alt="" style=display:none>
              <div class="dav ph" id=pvAvatarPh>🤖</div>
              <div class=dnm><span id=pvName>Gang Manager</span><span class=dapp>APP</span></div>
              <div class=dsec>About Me</div>
              <div class="dbio empty" id=pvBio>Your bio shows here…</div>
            </div>
          </div>
        </div>
      </div>
      <div class=rollout id=rollout>
        <div class=rollhead><span id=rollLabel>Rolling out…</span><span class=rollbar><i id=rollFill></i></span><span id=rollCount>0 / 0</span></div>
        <div class=rolllist id=rollList></div>
      </div>
      <p class=hint id=brandStatus style="margin:10px 0 0"></p>
    </div>
  </div>
  <div id=pan-log style=display:none>
    <style>
    #clCard .clwrap{display:grid;grid-template-columns:1fr 372px;gap:18px;margin-top:14px;align-items:start}
    #clCard .clhead{display:grid;grid-template-columns:1fr 150px 150px;gap:10px}
    #clCard input,#clCard select,#clCard textarea{width:100%;padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.28);color:#ececf1;font:inherit}
    #clCard textarea{line-height:1.5;resize:vertical;min-height:58px}
    #clCard input:focus,#clCard select:focus,#clCard textarea:focus{outline:none;border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.22)}
    #clCard .cllbl{display:block;margin:12px 0 5px;font-size:12px;font-weight:700;letter-spacing:.3px;color:#cdd0da}
    #clCard .clcat{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
    #clCard .clrow{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}
    #clCard .clprev{background:#2b2d31;border-radius:8px;overflow:hidden;border:1px solid rgba(0,0,0,.4)}
    #clCard .clp-inner{border-left:4px solid #2f6bff;border-radius:4px;background:#313338;padding:14px 16px}
    #clCard .clp-title{font-weight:700;color:#f2f3f5;font-size:15px;margin-bottom:6px}
    #clCard .clp-sum{color:#b5bac1;font-size:14px;line-height:1.45;margin-bottom:8px;white-space:pre-wrap;border-left:3px solid #4e5058;padding-left:10px}
    #clCard .clp-sec{font-weight:700;color:#f2f3f5;font-size:13px;margin:10px 0 3px}
    #clCard .clp-items{color:#dbdee1;font-size:14px;line-height:1.5}
    #clCard .clp-empty{color:#949ba4;font-size:13px}
    #clCard .clp-foot{display:flex;align-items:center;gap:6px;margin-top:12px;color:#949ba4;font-size:12px;border-top:1px solid rgba(255,255,255,.06);padding-top:8px}
    #clCard .clh-row{display:flex;gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--line);font-size:13px}
    #clCard .clh-v{font-weight:700;color:#e8e8ee}
    #clCard .clh-m{color:var(--mute);flex:1}
    #clCard .clh-row a{color:#5b93ff}
    @media(max-width:860px){#clCard .clwrap{grid-template-columns:1fr}#clCard .clcat{grid-template-columns:1fr}#clCard .clhead{grid-template-columns:1fr}}
    </style>
    <div class=card id=clCard>
      <h3>📋 Changelog</h3>
      <p class=hint>Write an update, build it up section by section, preview it, then post it to your server - rendered in the same embed the bots use. The staff bot posts it.</p>
      <div class=clwrap>
        <div>
          <div class=clhead>
            <div><label class=cllbl>Title</label><input id=clTitle maxlength=120 placeholder="Gang Manager · Update"></div>
            <div><label class=cllbl>Version</label><input id=clVersion maxlength=40 placeholder="v1.4.0"></div>
            <div><label class=cllbl>Date</label><input id=clDate maxlength=40 placeholder="2026-11-18"></div>
          </div>
          <label class=cllbl>Summary <span class=hint style=font-weight:400>- optional intro line</span></label>
          <textarea id=clSummary maxlength=600 rows=2 placeholder="What this update is about…"></textarea>
          <div class=clcat>
            <div><label class=cllbl>✨ Added</label><textarea id=cl_added rows=3 placeholder="One change per line"></textarea></div>
            <div><label class=cllbl>⚡ Improved</label><textarea id=cl_improved rows=3 placeholder="One change per line"></textarea></div>
            <div><label class=cllbl>🔧 Fixed</label><textarea id=cl_fixed rows=3 placeholder="One change per line"></textarea></div>
            <div><label class=cllbl>🔀 Changed</label><textarea id=cl_changed rows=3 placeholder="One change per line"></textarea></div>
            <div><label class=cllbl>🗑️ Removed</label><textarea id=cl_removed rows=3 placeholder="One change per line"></textarea></div>
          </div>
          <label class=cllbl>Post to</label>
          <div style=display:flex;gap:10px;align-items:center><select id=clChannel></select><button class="ghost sm" id=clRefresh title="Reload channels">↻</button></div>
          <p class=hint id=clChanHint style=margin:6px 0 0></p>
          <label class=cllbl>Ping role <span class=hint style=font-weight:400>- optional, @mentioned above the post</span></label>
          <select id=clPing><option value="">No ping</option></select>
          <div class=clrow>
            <button class=ghost id=clSaveBtn>💾 Save draft</button>
            <span style=flex:1></span>
            <button class=ghost id=clRepostBtn style=display:none>↻ Repost last</button>
            <button class=brand-btn id=clPostBtn>📣 Post now</button>
          </div>
        </div>
        <div>
          <p class=plabel>Live preview</p>
          <div class=clprev><div class=clp-inner id=clPrev></div></div>
          <p class=plabel style=margin-top:16px>Recently posted</p>
          <div id=clHistory><div class=hint>No changelogs posted yet.</div></div>
        </div>
      </div>
    </div>
  </div>
  <div id=pan-rules style=display:none>
    <style>
    #rlCard .rlwrap{display:grid;grid-template-columns:1fr 372px;gap:18px;margin-top:14px;align-items:start}
    #rlCard input,#rlCard select,#rlCard textarea{width:100%;padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.28);color:#ececf1;font:inherit}
    #rlCard textarea{line-height:1.5;resize:vertical}
    #rlCard input:focus,#rlCard select:focus,#rlCard textarea:focus{outline:none;border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.22)}
    #rlCard .cllbl{display:block;margin:12px 0 5px;font-size:12px;font-weight:700;letter-spacing:.3px;color:#cdd0da}
    #rlCard .clrow{display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap}
    #rlCard .clprev{background:#2b2d31;border-radius:8px;overflow:hidden;border:1px solid rgba(0,0,0,.4)}
    #rlCard .clp-inner{border-left:4px solid #2f6bff;border-radius:4px;background:#313338;padding:14px 16px}
    #rlCard .rlp-title{font-weight:700;color:#f2f3f5;font-size:15px;margin-bottom:8px}
    #rlCard .rlp-intro{color:#b5bac1;font-size:14px;line-height:1.45;margin-bottom:10px;white-space:pre-wrap}
    #rlCard .rlp-rule{color:#dbdee1;font-size:14px;line-height:1.5;margin:4px 0}
    #rlCard .rlp-foot{margin-top:12px;color:#949ba4;font-size:12px;border-top:1px solid rgba(255,255,255,.06);padding-top:8px}
    @media(max-width:860px){#rlCard .rlwrap{grid-template-columns:1fr}}
    /* styled dropdown (shared by the changelog + rules channel/role pickers) */
    .cs{position:relative;flex:1;min-width:0}
    .cs-trig{width:100%;display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.28);color:#ececf1;font:inherit;cursor:pointer;text-align:left}
    .cs-trig:hover{border-color:rgba(255,255,255,.22)}
    .cs.open .cs-trig{border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.22)}
    .cs-ic{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:6px;background:rgba(255,255,255,.08);color:#9aa0b4;font-weight:800;font-size:12px;flex:none}
    .cs-lbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .cs-arw{color:#8a8f9e;font-size:11px;transition:transform .15s}
    .cs.open .cs-arw{transform:rotate(180deg)}
    .cs-menu{position:absolute;z-index:60;top:calc(100% + 6px);left:0;right:0;max-height:260px;overflow:auto;background:#1e1f26;border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:6px;box-shadow:0 18px 44px rgba(0,0,0,.55)}
    .cs-row{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;cursor:pointer;color:#dbdee1;font-size:14px}
    .cs-row:hover{background:rgba(255,255,255,.07)}
    .cs-row.on{background:rgba(47,107,255,.20);color:#fff}
    </style>
    <div class=card id=rlCard>
      <h3>🛡️ Rules</h3>
      <p class=hint>Write your server rules, preview them, then post them to a channel - rendered in the same branded embed the bots use. Supports **bold**, *italics*, `code`, [links](url) and ||spoilers||. The staff bot posts it.</p>
      <div class=rlwrap>
        <div>
          <label class=cllbl>Title</label>
          <input id=rlTitle maxlength=120 placeholder="Server Rules">
          <label class=cllbl>Intro <span class=hint style=font-weight:400>- optional line above the rules</span></label>
          <textarea id=rlIntro maxlength=600 rows=2 placeholder="Follow these to keep the server friendly…"></textarea>
          <label class=cllbl>Rules <span class=hint style=font-weight:400>- one rule per line, auto-numbered</span></label>
          <textarea id=rlRules rows=10 placeholder="Be respectful to everyone&#10;No spam or self-promo&#10;Keep posts in the right channels"></textarea>
          <label class=cllbl>Footer <span class=hint style=font-weight:400>- optional</span></label>
          <input id=rlFooter maxlength=200 placeholder="Breaking a rule can mean a mute or ban">
          <label class=cllbl>Post to</label>
          <div style=display:flex;gap:10px;align-items:center><select id=rlChannel></select><button class="ghost sm" id=rlRefresh title="Reload channels">↻</button></div>
          <p class=hint id=rlChanHint style=margin:6px 0 0></p>
          <label class=cllbl>Ping role <span class=hint style=font-weight:400>- optional, @mentions them when the rules post</span></label>
          <select id=rlPing></select>
          <div class=clrow>
            <button class=ghost id=rlSaveBtn>💾 Save draft</button>
            <span style=flex:1></span>
            <button class=brand-btn id=rlPostBtn>📣 Post now</button>
          </div>
        </div>
        <div>
          <p class=plabel>Live preview</p>
          <div class=clprev><div class=clp-inner id=rlPrev></div></div>
        </div>
      </div>
    </div>
  </div>
  <div id=pan-audit style=display:none>
    <div class=card><h3>Admin audit log</h3><p class=hint>Every privileged action taken on this admin site - who did it, what they did, when, and from which IP. Owner-only, newest first.</p>
      <div class=toolbar style=margin-bottom:10px><input id=auditSearch placeholder="Search action, person or target…" autocomplete=off><button class="ghost sm" id=auditRefresh>↻ Refresh</button></div>
      <div id=auditList></div>
    </div>
  </div>
</div>
<div class=cmdk id=cmdk><div class=cmdkbox><input id=cmdkInput placeholder="Jump to a server or section…" autocomplete=off><div class=cmdkList id=cmdkList></div><div class=cmdkFoot><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span></div></div></div>
<div class=confirm id=confirmModal><div class=confirmbox><div class=ci2 id=confirmIcon>⚠️</div><h4 id=confirmTitle>Are you sure?</h4><p id=confirmMsg></p><div id=confirmInputWrap style="display:none;margin:2px 0 4px"><input id=confirmInput autocomplete=off maxlength=300 style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:9px;border:1px solid var(--line,#2a2c36);background:#141520;color:#e8e8ee;font-size:14px"></div><div class=confirmbtns><button class=ghost id=confirmCancel>Cancel</button><button class="brand-btn" id=confirmOk>Confirm</button></div></div></div>
<div class=toast id=toast></div>
<script>
(function(){
  var $=function(s){return document.querySelector(s)};
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function nf(n){try{return Number(n).toLocaleString()}catch(e){return n}}
  function fmtTs(t){if(!t)return '-';try{return new Date(t*1000).toLocaleString()}catch(e){return '-'}}
  function fmtIso(s){if(!s)return '-';try{return new Date(s).toLocaleString()}catch(e){return '-'}}
  function money(c,cur){if(!c)return '-';return (c/100).toFixed(2)+' '+(cur||'')}
  function rel(ts){if(!ts)return '';var s=Math.floor(Date.now()/1000)-ts;if(s<60)return 'just now';var m=Math.floor(s/60);if(m<60)return m+'m ago';var h=Math.floor(m/60);if(h<24)return h+'h ago';var d=Math.floor(h/24);if(d<30)return d+'d ago';try{return new Date(ts*1000).toLocaleDateString()}catch(e){return ''}}
  function initial(n){return (String(n||'?').trim()[0]||'?').toUpperCase()}
  function copyspan(v,cls){if(!v)return '<span class=mute>-</span>';return '<span class="'+(cls||'id')+' copy" data-copy="'+esc(v)+'" title="Click to copy">'+esc(v)+'</span>'}
  function idv(name,id){var n=name?esc(name)+' ':'';return id?(n+copyspan(id,'id')):(n||'<span class=mute>-</span>')}
  function animateNum(el,to){var from=0,st=performance.now();function tick(t){var k=Math.min(1,(t-st)/650);el.textContent=Math.round(from+(to-from)*(1-Math.pow(1-k,3))).toLocaleString();if(k<1)requestAnimationFrame(tick)}requestAnimationFrame(tick)}
  var STATE={owner:false},SERVERS=[],SHOP=[],BANS=[],firstStats=true,LIVE=false,liveTimer=null,curTab='subs';
  var SUBFILTER='all',SUBSORT='members',SUBQ='',SHOPQ='',SHOPKIND='all',BANQ='';
  function api(path,method,body,ms){var ctrl=new AbortController(),to=setTimeout(function(){ctrl.abort()},ms||12000);return fetch(path,{method:method||'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,credentials:'same-origin',signal:ctrl.signal}).then(function(r){return r.json().catch(function(){return {ok:false,error:'bad response'}})}).catch(function(){return {ok:false,error:'network'}}).then(function(j){clearTimeout(to);return j})}
  function toast(msg,bad){var t=$('#toast');t.innerHTML='<span>'+(bad?'⚠️':'✓')+'</span>'+esc(msg);t.className='toast show'+(bad?' bad':'');setTimeout(function(){t.className='toast'},2400)}
  function confirmBox(o){o=o||{};var m=$('#confirmModal');$('#confirmIcon').textContent=o.icon||'⚠️';$('#confirmTitle').textContent=o.title||'Are you sure?';$('#confirmMsg').textContent=o.message||'';var ok=$('#confirmOk');ok.textContent=o.confirmText||'Confirm';ok.className=o.danger?'brand-btn danger-solid':'brand-btn';
    var wrap=$('#confirmInputWrap'),inp=$('#confirmInput');if(o.input){inp.placeholder=o.input;inp.value='';wrap.style.display='';setTimeout(function(){inp.focus()},40)}else{wrap.style.display='none';setTimeout(function(){ok.focus()},30)}
    m.classList.add('show');
    return new Promise(function(resolve){function done(v){m.classList.remove('show');ok.onclick=null;$('#confirmCancel').onclick=null;m.onclick=null;document.removeEventListener('keydown',key,true);resolve(v)}ok.onclick=function(){done(o.input?inp.value:true)};$('#confirmCancel').onclick=function(){done(o.input?null:false)};m.onclick=function(e){if(e.target===m)done(o.input?null:false)};function key(e){if(e.key==='Escape'){e.preventDefault();e.stopPropagation();done(o.input?null:false)}else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();done(o.input?inp.value:true)}}document.addEventListener('keydown',key,true)});}
  function boot(){api('/api/admin/me').then(function(me){if(!me||!me.authed){showGate('login');return}if(!me.access){showGate('denied');return}if(me.mfa){showMfaGate(me);return}STATE.owner=!!me.owner;var tbm=$('#tab-main');if(tbm)tbm.style.display=STATE.owner?'':'none';var tbb=$('#tab-bots');if(tbb)tbb.style.display=STATE.owner?'':'none';var tbl=$('#tab-log');if(tbl)tbl.style.display=STATE.owner?'':'none';var tbr=$('#tab-rules');if(tbr)tbr.style.display=STATE.owner?'':'none';var tba=$('#tab-audit');if(tba)tba.style.display=STATE.owner?'':'none';var u=me.user||{};$('#uname').textContent=u.name||'Account';$('#uinitial').textContent=initial(u.name);var av=$('#uav');if(u.avatar){av.src=u.avatar;av.onerror=function(){av.style.display='none'}}else{av.style.display='none'}var role=$('#urole');role.textContent=STATE.owner?'Owner':'Staff';role.classList.toggle('owner',STATE.owner);$('#uchip').style.display='';$('#gate').style.display='none';$('#app').style.display='';selectTab('subs');loadSubs()}).catch(function(){showGate('error')})}
  function showGate(kind){$('#app').style.display='none';var g=$('#gate');g.style.display='';if(kind==='login')g.innerHTML='<div class=card><div class=big>🔐</div><h2>Admin sign-in</h2><p class=mute>Log in with a Discord account that owns or is authorised for the bot.</p><a href="/login?next=/admin"><button class="brand-btn" style=margin-top:12px>Log in with Discord</button></a></div>';else if(kind==='denied')g.innerHTML='<div class=card><div class=big>⛔</div><h2>Not authorised</h2><p class=mute>This account isn\'t on the admin list. Ask the owner to authorise your Discord ID.</p><a href="/logout"><button class=ghost style=margin-top:12px>Switch account</button></a></div>';else g.innerHTML='<div class=card><div class=big>⚠️</div><h2>Couldn\'t load</h2><p class=mute>Something went wrong. Refresh to try again.</p><a href="/admin"><button class=ghost style=margin-top:12px>Refresh</button></a></div>'}
  function showMfaGate(me){$('#app').style.display='none';var g=$('#gate');g.style.display='';var inv=(me&&me.support_invite)||'';var joinBtn=inv?'<a href="'+inv+'" target=_blank rel=noopener><button class="brand-btn" style=margin-top:10px>Join GM Support</button></a>':'';var help='<div id=mfaHelp style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line,rgba(255,255,255,.08))"><div class=mute style="font-size:13px;line-height:1.6">No code arriving? GM Support can only DM you if you share a server with it and have direct messages switched on.</div>'+joinBtn+'<ol class=mute style="text-align:left;font-size:12px;margin:10px auto 0;max-width:330px;line-height:1.7;padding-left:18px"><li>'+(inv?'Join the GM Support server with the button above.':'Join a server that GM Support is in.')+'</li><li>Right-click that server, open <b>Privacy Settings</b>, and turn on <b>Direct Messages</b>.</li><li>Come back here and press <b>Resend code</b>.</li></ol></div>';g.innerHTML='<div class=card><div class=big>🔐</div><h2>Verify it\'s you</h2><p class=mute>Confirm this sign-in with a one-time code - GM Support will DM it to your Discord.</p><div id=mfa1><button class="brand-btn" id=mfaSend style=margin-top:12px>Send my code</button></div><div id=mfa2 style="display:none;margin-top:12px"><input id=mfaCode placeholder="6-digit code" inputmode=numeric maxlength=6 autocomplete=one-time-code style="text-align:center;letter-spacing:6px;font-size:18px"><button class="brand-btn" id=mfaVerify style=margin-top:10px>Verify</button><div class=mute style=margin-top:8px><a href=# id=mfaResend>Resend code</a></div></div><div class=mute id=mfaMsg style=margin-top:10px></div>'+help+'<div style=margin-top:14px><a href="/logout"><button class=ghost>Switchaccount</button></a></div></div>';function send(){$('#mfaMsg').textContent='Sending…';api('/api/admin/mfa/send','POST',{}).then(function(r){if(r&&r.ok){$('#mfa1').style.display='none';$('#mfa2').style.display='';$('#mfaMsg').textContent='Code sent to your Discord DMs from GM Support.';var c=$('#mfaCode');if(c)setTimeout(function(){c.focus()},40)}else{$('#mfaMsg').textContent=(r&&r.error)||'Could not send the code.'}})}function verify(){var c=($('#mfaCode').value||'').trim();if(!c){$('#mfaMsg').textContent='Enter the code.';return}$('#mfaMsg').textContent='Checking…';api('/api/admin/mfa/verify','POST',{code:c}).then(function(r){if(r&&r.ok){boot()}else{$('#mfaMsg').textContent=(r&&r.error)||'Wrong code.'}})}$('#mfaSend').onclick=send;$('#mfaVerify').onclick=verify;$('#mfaResend').onclick=function(e){e.preventDefault();send()};$('#mfaCode').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();verify()}})}
  function selectTab(id){curTab=id;['subs','bans','shop','staff','main','bots','log','rules','audit'].forEach(function(t){var on=t===id,tab=$('#tab-'+t),pan=$('#pan-'+t);if(tab)tab.classList.toggle('active',on);if(pan)pan.style.display=on?'':'none'});if(id==='subs')loadSubs();if(id==='bans')loadBans();if(id==='shop')loadShop();if(id==='staff')loadStaff();if(id==='main')loadMain();if(id==='bots')loadBots();if(id==='log')loadChangelog();if(id==='rules')loadRules();if(id==='audit')loadAudit()}
  function setStats(c){c=c||{};var map={statServers:c.servers||0,statActive:c.active||0,statLife:c.lifetime||0,statMembers:c.members||0,statBans:c.bans||0,statStaff:c.staff||0};for(var k in map){var el=document.getElementById(k);if(!el)continue;if(firstStats)animateNum(el,map[k]);else el.textContent=nf(map[k])}firstStats=false}
  function bucket(s){if(s.lifetime)return 'lifetime';if(s.active)return 'active';if(s.status==='expired')return 'expired';return 'none'}
  function badge(s){var b=bucket(s);if(b==='lifetime')return '<span class="badge pur">Lifetime</span>';if(b==='active')return '<span class="badge live">Active</span>';if(b==='expired')return '<span class="badge red">Expired</span>';return '<span class="badge off">Inactive</span>'}
  function botChip(s){if(!s.bot_in)return '<span class="botin no">⚠ Bot not in server</span>';return s.pool_bot?'<span class="botin pool" title="Served by its own pool bot">🤖 '+esc(s.served_name||'Pool bot')+'</span>':'<span class="botin yes">✓ Bot in server</span>'}
  function expiry(s){if(s.lifetime)return 'Never (lifetime)';if(s.active&&s.days_left!=null)return s.days_left+' days left'+(s.expires_at?' · '+fmtIso(s.expires_at):'');if(s.expires_at)return fmtIso(s.expires_at);return '-'}
  function kv(k,v){return '<div class=kv><span class=k>'+k+'</span><span class=v>'+v+'</span></div>'}
  function loadSubs(){var box=$('#subsList');if(!SERVERS.length)box.innerHTML='<div class=sk></div><div class=sk></div>';api('/api/admin/overview').then(function(d){if(!d.ok){box.innerHTML='<div class=pad>Could not load.</div>';return}setStats(d.counts);SERVERS=d.servers||[];renderHealth();renderFilters();renderSubs()})}
  function renderHealth(){var c={active:0,lifetime:0,expired:0,none:0};SERVERS.forEach(function(s){c[bucket(s)]++});var tot=SERVERS.length||1;function pc(n){return (n/tot*100).toFixed(1)+'%'}var el=$('#subHealth');if(!SERVERS.length){el.style.display='none';return}el.style.display='';el.innerHTML='<div class=bdbar><i class=sa style=width:'+pc(c.active)+'></i><i class=sl style=width:'+pc(c.lifetime)+'></i><i class=se style=width:'+pc(c.expired)+'></i><i class=sn style=width:'+pc(c.none)+'></i></div><div class=bdleg><span><i class="dot" style=background:var(--ok)></i>Active <b>'+c.active+'</b></span><span><i class="dot" style=background:var(--purple)></i>Lifetime <b>'+c.lifetime+'</b></span><span><i class="dot" style=background:var(--bad)></i>Expired <b>'+c.expired+'</b></span><span><i class="dot" style=background:rgba(255,255,255,.3)></i>Inactive <b>'+c.none+'</b></span></div>'}
  function renderFilters(){var counts={all:SERVERS.length,active:0,lifetime:0,expired:0,none:0};SERVERS.forEach(function(s){counts[bucket(s)]++});var defs=[['all','All'],['active','Active'],['lifetime','Lifetime'],['expired','Expired'],['none','Inactive']];$('#subFilters').innerHTML=defs.map(function(d){return '<div class="fchip'+(SUBFILTER===d[0]?' on':'')+'" data-f="'+d[0]+'">'+d[1]+' <span class=cnt>'+counts[d[0]]+'</span></div>'}).join('');$('#subFilters').querySelectorAll('.fchip').forEach(function(c){c.onclick=function(){SUBFILTER=c.getAttribute('data-f');renderFilters();renderSubs()}})}
  function filteredServers(){var q=SUBQ.toLowerCase();var list=SERVERS.filter(function(s){if(SUBFILTER!=='all'&&bucket(s)!==SUBFILTER)return false;if(q&&!((s.name||'').toLowerCase().indexOf(q)>=0||String(s.id).indexOf(q)>=0))return false;return true});list.sort(function(a,b){if(SUBSORT==='members')return (b.members||0)-(a.members||0);if(SUBSORT==='name')return (a.name||'').localeCompare(b.name||'');if(SUBSORT==='created')return (b.created_at||0)-(a.created_at||0);var da=a.lifetime?1e9:(a.days_left==null?-1:a.days_left),db=b.lifetime?1e9:(b.days_left==null?-1:b.days_left);return da-db});return list}
  function renderSubs(){var box=$('#subsList'),list=filteredServers();if(!list.length){box.innerHTML='<div class=pad>No servers match.</div>';return}box.innerHTML=list.map(function(s){var ic=s.icon?'<img src="'+esc(s.icon)+'" alt="">':'';return '<div class=card><div class=srvhead><div class=srvname>'+ic+esc(s.name||('Server '+s.id))+' '+badge(s)+botChip(s)+'</div><div class=acts><button class=sm data-g="'+s.id+'" data-a=add data-d=7>+7d</button><button class=sm data-g="'+s.id+'" data-a=add data-d=30>+30d</button><button class="sm pur" data-g="'+s.id+'" data-a=life>♾ Lifetime</button><button class="sm danger" data-g="'+s.id+'" data-a=rev>Revoke</button>'+(STATE.owner&&s.bot_in?'<button class="sm danger" data-g="'+s.id+'" data-a=leave>🚪 Remove bot</button>':'')+'</div></div><div class=grid>'+kv('Members',nf(s.members))+kv('Plan',esc(s.plan||'-'))+kv('Expires',esc(expiry(s)))+kv('Server owner',idv(s.owner_name,s.owner_id))+kv('Purchased by',idv(s.purchaser_name,s.purchaser_id))+kv('Server ID',copyspan(s.id,'id'))+kv('Stripe customer',copyspan(s.customer_id,'id'))+kv('Stripe sub',copyspan(s.sub_id,'id'))+kv('Server created',fmtTs(s.created_at))+kv('Sub updated',fmtIso(s.updated_at))+'</div></div>'}).join('');box.querySelectorAll('button[data-a]').forEach(function(b){b.onclick=function(){subAction(b)}})}
  async function subAction(b){var g=b.getAttribute('data-g'),a=b.getAttribute('data-a'),body,label;if(a==='add'){var d=parseInt(b.getAttribute('data-d'),10);body={guild_id:g,action:'add_days',days:d};label='Added '+d+' days';}else if(a==='life'){body={guild_id:g,action:'lifetime'};label='Lifetime granted';}else if(a==='leave'){if(!(await confirmBox({icon:'🚪',title:'Remove the bot from this server?',message:'Gang Manager will leave this Discord server. The subscription stays as-is - you can re-add the bot any time.',confirmText:'Remove bot',danger:true})))return;b.disabled=true;api('/api/admin/leave','POST',{guild_id:g}).then(function(r){b.disabled=false;if(r.ok){toast('Bot removed from '+(r.name||'server'));loadSubs()}else toast(r.error||'Failed',true)});return;}else{if(!(await confirmBox({icon:'🚫',title:'Revoke subscription?',message:'This server loses access immediately until it re-subscribes.',confirmText:'Revoke',danger:true})))return;body={guild_id:g,action:'revoke'};label='Revoked';}b.disabled=true;api('/api/admin/sub','POST',body).then(function(r){b.disabled=false;if(r.ok){toast(label);loadSubs()}else toast(r.error||'Failed',true)})}
  function customDays(){var g=$('#cdGuild').value.trim(),d=parseInt($('#cdDays').value,10);if(!g){toast('Enter a server ID',true);return}if(!d){toast('Enter days',true);return}api('/api/admin/sub','POST',{guild_id:g,action:'add_days',days:d}).then(function(r){if(r.ok){toast('Done');$('#cdDays').value='';loadSubs()}else toast(r.error||'Failed',true)})}
  function loadBans(){var box=$('#bansList');box.innerHTML='<div class=sk style=height:52px></div>';api('/api/admin/bans').then(function(d){if(!d.ok){box.innerHTML='<div class=pad>Could not load.</div>';return}BANS=d.bans||[];renderBans()})}
  function renderBans(){
    var box=$('#bansList'),q=BANQ.toLowerCase();
    var list=BANS.filter(function(b){return !q||(String(b.id).indexOf(q)>=0||(b.name||'').toLowerCase().indexOf(q)>=0||(b.reason||'').toLowerCase().indexOf(q)>=0)});
    var cnt=BANS.length?('<div class=bcount><b>'+BANS.length+'</b> user'+(BANS.length!==1?'s':'')+' banned'+(q&&list.length!==BANS.length?(' · '+list.length+' shown'):'')+'</div>'):'';
    if(!list.length){box.innerHTML=cnt+'<div class=pad>'+(BANS.length?'No bans match.':'No one is banned - nice and clean.')+'</div>';return}
    box.innerHTML=cnt+list.map(function(b){
      var av='<div class=bavwrap><div class=binitial>'+esc(initial(b.name))+'</div>'+(b.avatar?'<img class=bav src="'+esc(b.avatar)+'" alt="" onerror="this.style.display=\'none\'">':'')+'</div>';
      return '<div class=brow>'+av+'<div class=bmain><div class=bname>'+esc(b.name||('ID '+b.id))+' <span class="badge red">Banned</span></div>'
        +'<div class=bsub>'+copyspan(b.id,'mono')+' · '+rel(b.ts)+(b.by_name?(' · by '+esc(b.by_name)):'')+'</div>'
        +(b.reason?'<div class=breason>“'+esc(b.reason)+'”</div>':'')
        +'</div><button class="sm danger" data-u="'+esc(b.id)+'">Unban</button></div>';
    }).join('');
    box.querySelectorAll('button[data-u]').forEach(function(x){x.onclick=function(){confirmBox({icon:'♻️',title:'Unban user?',message:'They will be able to use the bot again.',confirmText:'Unban'}).then(function(ok){if(!ok)return;api('/api/admin/unban','POST',{user_id:x.getAttribute('data-u')}).then(function(r){if(r.ok){toast('Unbanned');loadBans();loadSubs()}else toast(r.error||'Failed',true)})})}});
  }
  function doBan(){var id=$('#banId').value.trim(),rs=$('#banReason').value.trim();if(!id){toast('Enter a Discord ID',true);return}api('/api/admin/ban','POST',{user_id:id,reason:rs}).then(function(r){if(r.ok){toast('Banned');$('#banId').value='';$('#banReason').value='';loadBans();loadSubs()}else toast(r.error||'Failed',true)})}
  function sendTestLogs(){confirmBox({icon:'🧪',title:'Send test logs?',message:'Fires one sample of every admin log kind to its log channel. Test info only, nothing real happens.',confirmText:'Send'}).then(function(go){if(!go)return;var btn=$('#tlSend');btn.disabled=true;btn.textContent='Sending…';$('#tlMsg').textContent='';api('/api/admin/test_logs','POST',{}).then(function(r){btn.disabled=false;btn.textContent='Send test logs';if(r&&r.ok){$('#tlMsg').textContent='✅ Sent '+(r.sent||0)+' test log(s) - check your admin log channels.';toast('Test logs sent')}else{toast((r&&r.error)||'Failed',true);$('#tlMsg').textContent=(r&&r.error)||'Failed'}})})}
  function loadShop(){var box=$('#shopList');var tc=$('#testBuyCard');if(tc)tc.style.display=STATE.owner?'':'none';var tl=$('#testLogCard');if(tl)tl.style.display=STATE.owner?'':'none';box.innerHTML='<div class=sk style=height:40px></div><div class=sk style=height:40px></div>';api('/api/admin/shop_log').then(function(d){if(!d.ok){box.innerHTML='<div class=pad>Could not load.</div>';return}SHOP=d.log||[];var sel=$('#tbServer');if(sel){var cur=sel.value;var o='<option value="">No specific server</option>';(d.servers||[]).forEach(function(s){o+='<option value="'+esc(s.id)+'">'+(s.active?'🟢 ':'⚪ ')+esc(s.name)+'</option>'});sel.innerHTML=o;if(cur)sel.value=cur}renderShop()})}
  function sendTestBuy(){var u=($('#tbUser').value||'').trim();if(!/^\d{5,25}$/.test(u)){toast('Enter a valid Discord user ID',true);return}confirmBox({icon:'🧪',title:'Send a test purchase?',message:'DMs a test invoice to that user and posts a test purchase log. No charge, no record.',confirmText:'Send test'}).then(function(go){if(!go)return;var btn=$('#tbSend');btn.disabled=true;btn.textContent='Sending…';$('#tbMsg').textContent='';api('/api/admin/test_purchase','POST',{user_id:u,server_id:($('#tbServer').value||''),amount:($('#tbAmount').value||'').trim()}).then(function(r){btn.disabled=false;btn.textContent='Send test';if(r&&r.ok){$('#tbMsg').innerHTML=(r.dm==='sent'?'✅ Invoice DM delivered.':'⚠️ Logged, but couldn\'t DM them (their DMs may be closed, or they share no server with the bot).')+' · Invoice <span class="mono copy" data-copy="'+esc(r.invoice||'')+'" title="Click to copy">'+esc(r.invoice||'')+'</span>';toast('Test purchase sent')}else{toast((r&&r.error)||'Failed',true);$('#tbMsg').textContent=(r&&r.error)||'Failed'}})})}
  function relTs(t){if(!t)return '-';var s=Math.floor(Date.now()/1000)-t;if(s<60)return 'just now';var m=Math.floor(s/60);if(m<60)return m+'m ago';var h=Math.floor(m/60);if(h<24)return h+'h ago';var d=Math.floor(h/24);if(d<30)return d+'d ago';try{return new Date(t*1000).toLocaleDateString()}catch(e){return d+'d ago'}}
  function shopFiltered(){var q=SHOPQ.toLowerCase();return SHOP.filter(function(e){if(SHOPKIND!=='all'&&(e.kind||'purchase')!==SHOPKIND)return false;return !q||((e.email||'').toLowerCase().indexOf(q)>=0||(e.ip||'').indexOf(q)>=0||(e.guild_name||'').toLowerCase().indexOf(q)>=0||String(e.guild_id||'').indexOf(q)>=0||String(e.purchaser_id||'').indexOf(q)>=0||(e.session_id||'').toLowerCase().indexOf(q)>=0||(e.customer_id||'').toLowerCase().indexOf(q)>=0)})}
  function renderShopStats(list){var rev={},buyers={},np=0,nr=0;list.forEach(function(e){var k=(e.kind||'purchase');if(k==='test')return;if(e.amount){var c=e.currency||'';rev[c]=(rev[c]||0)+e.amount}if(e.email)buyers[(e.email||'').toLowerCase()]=1;if(k==='renewal')nr++;else np++});var revtxt=Object.keys(rev).map(function(c){return (rev[c]/100).toFixed(2)+(c?' '+c:'')}).join(' + ')||'-';function tile(n,k,ic){return '<div class=sstat><div class=si>'+ic+'</div><div><div class=n>'+n+'</div><div class=k>'+k+'</div></div></div>'}$('#shopStats').innerHTML=tile(esc(revtxt),'Revenue','💰')+tile(nf(np),'Purchases','💳')+tile(nf(nr),'Renewals','🔄')+tile(nf(Object.keys(buyers).length),'Buyers','👤')}
  function renderShop(){var box=$('#shopList'),list=shopFiltered();var chips=$('#shopChips');if(chips)chips.querySelectorAll('[data-sk]').forEach(function(b){b.classList.toggle('on',b.getAttribute('data-sk')===SHOPKIND)});renderShopStats(list);if(!list.length){box.innerHTML='<div class=pad>'+(SHOP.length?'Nothing matches your filter.':'No purchases logged yet - they show here after the next sale.')+'</div>';return}var head='<div class=shopcount>Showing <b>'+list.length+'</b>'+(SHOP.length!==list.length?' of '+SHOP.length:'')+'</div>';box.innerHTML=head+list.map(function(e){var kind=(e.kind||'purchase');var badge=kind==='renewal'?'🔄':(kind==='test'?'🧪':'💳');var amt=e.amount?('<div class=samt>'+(e.amount/100).toFixed(2)+'<small>'+esc(e.currency||'')+'</small></div>'):'<div class="samt mut">-</div>';var server=e.guild_name?('<span class=sserver>'+esc(e.guild_name)+'</span>'):(e.guild_id?('<span class=sserver>'+copyspan(String(e.guild_id),'mono')+'</span>'):'');var bits=['<span>🕒 <span title="'+esc(fmtTs(e.ts))+'">'+esc(relTs(e.ts))+'</span></span>'];if(e.ip)bits.push('<span>IP '+copyspan(e.ip,'mono')+'</span>');if(e.purchaser_id)bits.push('<span>👤 '+copyspan(String(e.purchaser_id),'mono')+'</span>');if(e.session_id)bits.push('<span>🧾 '+copyspan(e.session_id,'mono')+'</span>');return '<div class=shoprow><div class="skind '+kind+'">'+badge+'</div><div class=smain><div class=sl1><span class=semail>'+esc(e.email||'-')+'</span>'+server+'</div><div class=sl2>'+bits.join('<span class=dot>·</span>')+'</div></div>'+amt+'</div>'}).join('')}
  function exportCsv(){var list=shopFiltered();if(!list.length){toast('Nothing to export',true);return}var head=['when','kind','email','ip','amount','currency','server','server_id','buyer_id','invoice','customer_id'];var rows=list.map(function(e){return [fmtTs(e.ts),e.kind||'',e.email||'',e.ip||'',(e.amount?(e.amount/100).toFixed(2):''),e.currency||'',e.guild_name||'',e.guild_id||'',e.purchaser_id||'',e.session_id||'',e.customer_id||''].map(function(v){return '"'+String(v).replace(/"/g,'""')+'"'}).join(',')});var csv=head.join(',')+'\n'+rows.join('\n');var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='shop-log.csv';a.click();toast('CSV exported')}
  function loadAudit(){var box=$('#auditList');box.innerHTML='<div class=sk style=height:40px></div><div class=sk style=height:40px></div>';api('/api/admin/audit_log').then(function(d){if(!d.ok){box.innerHTML='<div class=pad>'+esc(d.error||'Could not load.')+'</div>';return}AUDIT=d.log||[];renderAudit()})}
  function auditFiltered(){var q=AUDITQ.toLowerCase();return AUDIT.filter(function(e){return !q||((e.action||'').toLowerCase().indexOf(q)>=0||(e.actor_name||'').toLowerCase().indexOf(q)>=0||String(e.actor||'').indexOf(q)>=0||String(e.target||'').indexOf(q)>=0||(e.detail||'').toLowerCase().indexOf(q)>=0)})}
  var AUDIT_LBL={admin_signin:'🔑 Sign-in',mfa_lockout:'⛔ Code lockout',ban:'🔨 Ban',unban:'♻️ Unban',staff_add:'🛡️ Staff add',staff_remove:'🚫 Staff remove',bot_leave_server:'👋 Bot left server',pool_add_tokens:'➕ Pool tokens',pool_handouts:'🔀 Hand-outs',pool_hub:'🏠 Hub',sub_add_days:'📅 Sub +days',sub_lifetime:'♾️ Lifetime',sub_revoke:'❌ Sub revoke'};
  function auditLabel(a){if(AUDIT_LBL[a])return AUDIT_LBL[a];if(a&&a.indexOf('pool_')===0)return '🤖 '+esc(a.slice(5));return esc(a||'-')}
  function renderAudit(){var box=$('#auditList'),list=auditFiltered();if(!list.length){box.innerHTML='<div class=pad>'+(AUDIT.length?'Nothing matches.':'No admin actions logged yet - privileged actions will appear here.')+'</div>';return}box.innerHTML='<table class=tbl><thead><tr><th>When</th><th>Action</th><th>By</th><th>Target</th><th>Detail</th></tr></thead><tbody>'+list.map(function(e){var by=(e.actor_name?esc(e.actor_name)+' ':'')+copyspan(String(e.actor||''),'mono');return '<tr><td>'+fmtTs(e.ts)+'</td><td>'+auditLabel(e.action)+'</td><td>'+by+'</td><td>'+(e.target?copyspan(String(e.target),'mono'):'<span class=mute>-</span>')+'</td><td>'+esc(e.detail||'')+'</td></tr>'}).join('')+'</tbody></table>'}
  function loadStaff(){var box=$('#staffList');box.innerHTML='<div class=sk style=height:52px></div>';api('/api/admin/staff').then(function(d){if(!d.ok){box.innerHTML='<div class=pad>Could not load.</div>';return}var canEdit=!!d.owner;$('#staffForm').style.display=canEdit?'':'none';$('#staffOwnerNote').style.display=canEdit?'none':'';if(!d.staff.length){box.innerHTML='<div class=pad>No extra staff authorised - just you.</div>';return}box.innerHTML=d.staff.map(function(s){return '<div class=rowline><div class=rowmain><div class=nm>'+esc(s.name||('ID '+s.id))+'</div><div class=mt>'+copyspan(s.id,'mono')+' · added '+fmtTs(s.ts)+'</div></div>'+(canEdit?'<button class="sm danger" data-s="'+esc(s.id)+'">Remove</button>':'')+'</div>'}).join('');if(canEdit)box.querySelectorAll('button[data-s]').forEach(function(x){x.onclick=function(){confirmBox({icon:'🛡️',title:'Remove staff access?',message:'They will lose access to this admin site.',confirmText:'Remove',danger:true}).then(function(ok){if(!ok)return;api('/api/admin/staff_remove','POST',{user_id:x.getAttribute('data-s')}).then(function(r){if(r.ok){toast('Removed');loadStaff();loadSubs()}else toast(r.error||'Failed',true)})})}})})}
  function addStaff(){var id=$('#staffId').value.trim();if(!id){toast('Enter a Discord ID',true);return}api('/api/admin/staff_add','POST',{user_id:id}).then(function(r){if(r.ok){toast('Authorised');$('#staffId').value='';loadStaff();loadSubs()}else toast(r.error||'Failed',true)})}
  // ---- Bots (bot pool) ----
  var POOL=null,POOLQ='',AUDIT=[],AUDITQ='';
  var _objUrls={};
  function setObjUrl(key,f){if(_objUrls[key]){try{URL.revokeObjectURL(_objUrls[key])}catch(x){}}if(f){var u=URL.createObjectURL(f);_objUrls[key]=u;return u}_objUrls[key]=null;return null}
  function renderPreviewBio(){var t=$('#brandBio'),p=$('#pvBio');if(!p)return;var v=t?t.value.trim():'';if(v){p.textContent=v;p.classList.remove('empty')}else{p.textContent='Your bio shows here…';p.classList.add('empty')}}
  function brandCount(){var t=$('#brandBio');if(t&&$('#brandCount'))$('#brandCount').textContent=t.value.length+' / 400';renderPreviewBio()}
  function setPick(isAv,f){
   var nameEl=$(isAv?'#avName':'#bnName'),th=$(isAv?'#avThumb':'#bnThumb'),zn=$(isAv?'#zoneAvatar':'#zoneBanner');
   if(nameEl)nameEl.textContent=f?f.name:'Click or drop an image';
   if(zn)zn.classList.toggle('set',!!f);
   var u=setObjUrl(isAv?'av':'bn',f);
   if(th)th.innerHTML=u?'<img src="'+u+'" alt="">':'🖼️';
   if(isAv){var av=$('#pvAvatar'),ph=$('#pvAvatarPh');if(u){av.src=u;av.style.display='';if(ph)ph.style.display='none'}else{av.style.display='none';av.removeAttribute('src');if(ph)ph.style.display=''}}
   else{var bnEl=$('#pvBanner');if(bnEl)bnEl.style.backgroundImage=u?'url("'+u+'")':''}
  }
  function botStatus(r){
   if(!r)return['pend','queued…'];
   var e=r.errors||[],ch=r.changed||[];
   if(e.some(function(x){return /rate-limited/.test(x)}))return['wait','rate-limited'];
   if(ch.length)return['ok',ch.join(' + ')];
   if(e.length)return['bad',String(e[0]).slice(0,22)];
   return['ok','updated'];
  }
  function renderRollout(d){
   var ro=$('#rollout');if(!ro)return;
   var prog=d.progress||null;
   var results=(prog&&prog.results)||((d.last_run&&d.last_run.results))||[];
   var running=!!(prog&&prog.running)||!!d.running;
   if(!results.length&&!running){ro.style.display='none';return}
   ro.style.display='';
   var names=(d.fleet&&d.fleet.length)?d.fleet:results.map(function(r){return r.name});
   var total=(prog&&prog.total)||names.length||results.length;
   var done=results.length;
   $('#rollFill').style.width=total?Math.round(done/total*100)+'%':'0';
   $('#rollCount').textContent=done+' / '+total;
   $('#rollLabel').textContent=running?'Rolling out…':'Done';
   var byName={};results.forEach(function(r){byName[r.name]=r});
   $('#rollList').innerHTML=names.map(function(nm){
    var st=botStatus(byName[nm]||null);
    return '<div class=rollitem><span class="rdot '+st[0]+'"></span><b>'+esc(nm)+'</b><em>'+esc(st[1])+'</em></div>';
   }).join('');
  }
  function brandStatusHtml(lr,running){
   if(running)return '<span style="color:#5b93ff">● Rolling out to the fleet…</span>';
   if(!lr)return 'Nothing pushed yet. Add an avatar, banner or bio and apply.';
   var res=lr.results||[],ok=lr.applied||0,tot=lr.total||0;
   var limited=res.filter(function(r){return (r.errors||[]).some(function(e){return /rate-limited/.test(e)})}).length;
   var s='Last roll-out: <b>'+ok+' / '+tot+'</b> bot(s) updated';
   if(limited)s+=' · <span style="color:#eaa12a">'+limited+' rate-limited - hit apply again later for those</span>';
   return s;
  }
  function loadBrand(){api('/api/admin/bot_profile').then(function(d){if(!d||!d.ok)return;
   var b=$('#brandBio');if(b&&document.activeElement!==b){b.value=d.bio||'';brandCount()}
   var nm=$('#pvName');if(nm&&d.bot_name)nm.textContent=d.bot_name;
   renderPreviewBio();renderRollout(d);
   var st=$('#brandStatus');if(st)st.innerHTML=brandStatusHtml(d.last_run,d.running)})}
  function pollRollout(){if(window._rollT)return;(function tick(){api('/api/admin/bot_profile').then(function(d){
   if(d&&d.ok){renderRollout(d);var st=$('#brandStatus');if(st)st.innerHTML=brandStatusHtml(d.last_run,d.running)}
   var run=d&&((d.progress&&d.progress.running)||d.running);
   if(run){window._rollT=setTimeout(function(){window._rollT=null;tick()},2000)}else{window._rollT=null}})})()}
  function applyBrand(){
   var btn=$('#brandApply'),av=$('#brandAvatar'),bn=$('#brandBanner'),bio=$('#brandBio');
   var fd=new FormData();
   if(av&&av.files&&av.files[0])fd.append('avatar',av.files[0]);
   if(bn&&bn.files&&bn.files[0])fd.append('banner',bn.files[0]);
   fd.append('bio',bio?bio.value:'');
   btn.disabled=true;btn.textContent='Applying…';
   fetch('/api/admin/bot_profile',{method:'POST',body:fd,credentials:'same-origin'}).then(function(r){return r.json().catch(function(){return{ok:false,error:'bad response ('+r.status+')'}})}).then(function(j){
    btn.disabled=false;btn.textContent='Apply to all bots';
    if(j.ok){toast(j.message||'Rolling out to the fleet');if(av)av.value='';if(bn)bn.value='';setPick(true,null);setPick(false,null);pollRollout()}
    else toast(j.error||'Failed',true)
   }).catch(function(){btn.disabled=false;btn.textContent='Apply to all bots';toast('Network error',true)})
  }
  document.addEventListener('change',function(e){var t=e.target;if(t.id==='brandAvatar'||t.id==='brandBanner'){setPick(t.id==='brandAvatar',t.files&&t.files[0])}});
  ['zoneAvatar','zoneBanner'].forEach(function(zid){
   document.addEventListener('dragover',function(e){var z=e.target.closest&&e.target.closest('#'+zid);if(z){e.preventDefault();z.classList.add('drag')}});
   document.addEventListener('dragleave',function(e){var z=e.target.closest&&e.target.closest('#'+zid);if(z)z.classList.remove('drag')});
   document.addEventListener('drop',function(e){var z=e.target.closest&&e.target.closest('#'+zid);if(!z)return;e.preventDefault();z.classList.remove('drag');var f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];if(f&&/^image\//.test(f.type)){var inp=$(zid==='zoneAvatar'?'#brandAvatar':'#brandBanner');try{var dt=new DataTransfer();dt.items.add(f);inp.files=dt.files}catch(x){}setPick(zid==='zoneAvatar',f)}});
  });
  function _fdotClass(o){return o.off?'off':(o.online?'on':(o.unreachable?'unk':'off'))}
  function _fstate(o){if(o.unreachable)return 'Unreachable';if(o.off)return 'Turned off';if(o.online)return 'Online';return o.ready?'Online':'Connecting…'}
  function renderFleetBot(pfx,o){o=o||{};var nm=$('#'+pfx+'Name');if(nm&&o.name)nm.textContent=o.name;var dot=$('#'+pfx+'Dot');if(dot)dot.className='fdot '+_fdotClass(o);var st=$('#'+pfx+'State');if(st)st.textContent=_fstate(o);var off=$('#'+pfx+'Off'),on=$('#'+pfx+'On');if(off)off.style.display=o.off?'none':'';if(on)on.style.display=o.off?'':'none'}
  function loadFleet(){api('/api/admin/fleet').then(function(d){if(!d||!d.ok)return;renderFleetBot('fMain',d.main);renderFleetBot('fStaff',d.staff)})}
  function fleetPower(target,action){
   var label=target==='main'?'main':'staff';
   function go(){api('/api/admin/fleet/power','POST',{target:target,action:action}).then(function(r){
    if(r&&r.ok){toast(label+' bot - '+action+' ✓');setTimeout(loadFleet,800);setTimeout(loadFleet,3500)}
    else toast((r&&r.error)||'Failed',true)});}
   if(action==='on'){go();return}
   confirmBox({
    icon:action==='off'?'⏻':'↻',
    title:(action==='off'?'Turn off the '+label+' bot?':'Restart the '+label+' bot?'),
    message:(target==='main'&&action==='off')?'Servers on the main bot stop responding until you turn it back on.':(action==='restart'?'It will drop offline and reconnect in a moment.':'It goes offline until you turn it back on.'),
    confirmText:action==='off'?'Turn off':'Restart',
    danger:action==='off'
   }).then(function(ok){
    if(!ok)return;
    if(target==='main'&&action==='off'){
     confirmBox({icon:'⚠️',title:'Are you absolutely sure?',message:'This takes the main bot offline for every server. Type OFF to confirm.',confirmText:'Turn off the main bot',danger:true,input:'Type OFF'}).then(function(v){if(v===null)return;if(String(v).trim().toUpperCase()==='OFF')go();else toast('Didn’t match - type OFF to confirm',true)});
    }else{go()}
   });
  }
  var CL_CATS=[['added','🎉','Added'],['improved','🔥','Improved'],['fixed','🔨','Fixed'],['changed','🔄','Changed'],['removed','🗑️','Removed']];
  var CL_CHANS=[],CL_BRAND='',CL_BOUND=false,CL_LAST=null;
  function clToday(){try{return new Date().toISOString().slice(0,10)}catch(e){return ''}}
  function clBullets(v){return String(v||'').split('\n').map(function(x){return x.trim()}).filter(Boolean)}
  function clGather(){var sec={};CL_CATS.forEach(function(c){var el=$('#cl_'+c[0]);sec[c[0]]=el?el.value:''});
    return {title:($('#clTitle').value||'').trim(),version:($('#clVersion').value||'').trim(),date:($('#clDate').value||'').trim(),summary:($('#clSummary').value||'').trim(),channel_id:($('#clChannel').value||'').trim(),ping_role_id:($('#clPing').value||''),sections:sec}}
  function clRenderPreview(){var d=clGather();var title=d.title||'Gang Manager · Update';
    var ping='';var ps=$('#clPing');if(ps&&ps.value){var po=ps.options[ps.selectedIndex];if(po&&po.text)ping='<div class=clp-sec>🎉 Heads up</div><div class=clp-items><span style="background:rgba(88,101,242,.28);color:#c9cdfb;padding:2px 7px;border-radius:4px;font-weight:600">'+esc(po.text)+'</span></div>'}
    var html='<div class=clp-title>📋 '+esc(title)+(d.version?' · '+esc(d.version):'')+'</div>';
    if(d.summary)html+='<div class=clp-sum>'+esc(d.summary)+'</div>';var any=false;
    CL_CATS.forEach(function(c){var items=clBullets(d.sections[c[0]]);if(!items.length)return;any=true;
      html+='<div class=clp-sec>'+c[1]+' '+esc(c[2])+'</div><div class=clp-items>'+items.map(function(x){return '• '+esc(x)}).join('<br>')+'</div>'});
    if(!any&&!d.summary)html+='<div class=clp-empty>Fill in a summary or some changes to see them here…</div>';
    html+=ping;
    var foot=CL_BRAND||'GM Support';if(d.version)foot+=' · '+d.version;if(d.date)foot+=' · '+d.date;
    html+='<div class=clp-foot>'+esc(foot)+'</div>';$('#clPrev').innerHTML=html}
  function clRenderHistory(h){var box=$('#clHistory');if(!h||!h.length){box.innerHTML='<div class=hint>No changelogs posted yet.</div>';return}
    box.innerHTML=h.map(function(e){var when='';try{when=e.at?new Date(e.at).toLocaleDateString():''}catch(x){}var lbl=esc(e.version||e.title||'Update');var ch=e.channel?esc(e.channel):'';var lnk=e.url?' · <a href="'+esc(e.url)+'" target=_blank rel=noopener>view</a>':'';
      return '<div class=clh-row><span class=clh-v>'+lbl+'</span><span class=clh-m>'+ch+(when?' · '+esc(when):'')+'</span>'+lnk+'</div>'}).join('')}
  function csEnhance(sel,icon){
    if(!sel||sel._cs)return;icon=icon||'#';
    var strip=new RegExp('^'+icon.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\s*');
    var wrap=document.createElement('div');wrap.className='cs';
    sel.parentNode.insertBefore(wrap,sel);wrap.appendChild(sel);sel.style.display='none';
    var trig=document.createElement('button');trig.type='button';trig.className='cs-trig';
    var menu=document.createElement('div');menu.className='cs-menu';menu.style.display='none';
    wrap.appendChild(trig);wrap.appendChild(menu);
    function disp(o){return o?String(o.textContent).replace(strip,''):'';}
    function paint(){var o=sel.options[sel.selectedIndex];trig.innerHTML='<span class=cs-ic>'+icon+'</span><span class=cs-lbl>'+esc(disp(o)||'Select…')+'</span><span class=cs-arw>▾</span>';}
    function build(){menu.innerHTML='';Array.prototype.forEach.call(sel.options,function(o,i){var row=document.createElement('div');row.className='cs-row'+(i===sel.selectedIndex?' on':'');row.innerHTML='<span class=cs-ic>'+icon+'</span>'+esc(disp(o));row.onclick=function(ev){ev.stopPropagation();sel.selectedIndex=i;try{sel.dispatchEvent(new Event('change',{bubbles:true}))}catch(x){}paint();close();};menu.appendChild(row);});}
    function open(){build();menu.style.display='';wrap.classList.add('open');}
    function close(){menu.style.display='none';wrap.classList.remove('open');}
    trig.onclick=function(e){e.stopPropagation();if(menu.style.display==='none')open();else close();};
    document.addEventListener('click',function(e){if(!wrap.contains(e.target))close();});
    sel._cs={sync:paint};paint();
  }
  function csSync(id){var s=$('#'+id);if(s&&s._cs)s._cs.sync();}
  function clInit(){if(CL_BOUND)return;CL_BOUND=true;
    csEnhance($('#clChannel'),'#');csEnhance($('#clPing'),'@');
    ['clTitle','clVersion','clDate','clSummary'].forEach(function(id){var el=$('#'+id);if(el)el.addEventListener('input',clRenderPreview)});
    CL_CATS.forEach(function(c){var el=$('#cl_'+c[0]);if(el)el.addEventListener('input',clRenderPreview)});
    var ch=$('#clChannel');if(ch)ch.addEventListener('change',clRenderPreview);
    var pg=$('#clPing');if(pg)pg.addEventListener('change',clRenderPreview);
    var sb=$('#clSaveBtn');if(sb)sb.onclick=function(){clSave()};
    var pb=$('#clPostBtn');if(pb)pb.onclick=clPost;
    var rpb=$('#clRepostBtn');if(rpb)rpb.onclick=clRepost;
    var rb=$('#clRefresh');if(rb)rb.onclick=function(){loadChangelog()}}
  function loadChangelog(){clInit();api('/api/admin/changelog').then(function(d){if(!d||!d.ok)return;CL_BRAND=d.brand||'';
    var dr=d.draft||{};$('#clTitle').value=dr.title||'';$('#clVersion').value=dr.version||'';$('#clDate').value=dr.date||clToday();$('#clSummary').value=dr.summary||'';
    var sec=dr.sections||{};CL_CATS.forEach(function(c){var el=$('#cl_'+c[0]);if(el)el.value=((sec[c[0]]||[])).join('\n')});
    CL_CHANS=d.channels||[];var sel=$('#clChannel');var opts='<option value="">'+(d.staff_ok?'Default log channel':'Staff bot offline')+'</option>';
    CL_CHANS.forEach(function(c){opts+='<option value="'+c.id+'">#'+esc(c.name)+'</option>'});sel.innerHTML=opts;if(dr.channel_id)sel.value=dr.channel_id;
    var psel=$('#clPing');if(psel){var popts='<option value="">No ping</option>';(d.roles||[]).forEach(function(r){popts+='<option value="'+esc(r.id)+'">@'+esc(r.name)+'</option>'});psel.innerHTML=popts;if(dr.ping_role_id)psel.value=dr.ping_role_id}
    $('#clChanHint').textContent=d.staff_ok?('Posts in '+(d.guild||'your server')+' · '+CL_CHANS.length+' channel'+(CL_CHANS.length===1?'':'s')+' available'):'Couldn\'t reach the staff bot - it posts the embed, so it needs to be online.';
    csSync('clChannel');csSync('clPing');clRenderHistory(d.history||[]);clRenderPreview()})}
  function clSave(cb){var btn=$('#clSaveBtn');btn.disabled=true;api('/api/admin/changelog','POST',{action:'save',draft:clGather()}).then(function(r){btn.disabled=false;if(r&&r.ok){toast('Draft saved');if(cb)cb()}else{toast((r&&r.error)||'Save failed',true)}})}
  function clChanName(d){return d.channel_id?('#'+(((CL_CHANS.filter(function(c){return c.id===d.channel_id})[0])||{}).name||'channel')):'the default log channel'}
  function clClearBoxes(){['clTitle','clVersion','clSummary'].forEach(function(id){var e=$('#'+id);if(e)e.value=''});CL_CATS.forEach(function(c){var e=$('#cl_'+c[0]);if(e)e.value=''});var dt=$('#clDate');if(dt)dt.value=clToday();clRenderPreview()}
  function clShowRepost(){var rb=$('#clRepostBtn');if(rb){rb.style.display='';rb.title='Repost '+((CL_LAST&&(CL_LAST.version||CL_LAST.title))||'the last changelog')}}
  function clSendPost(d,fromBoxes){var btn=$('#clPostBtn');btn.disabled=true;btn.textContent='Posting…';var rb=$('#clRepostBtn');if(rb)rb.disabled=true;
    api('/api/admin/changelog','POST',{action:'post',draft:d}).then(function(r){btn.disabled=false;btn.textContent='📣 Post now';if(rb)rb.disabled=false;
      if(r&&r.ok){toast('Posted to '+(r.channel||'the channel'));if(r.history)clRenderHistory(r.history);CL_LAST=d;clShowRepost();if(fromBoxes)clClearBoxes()}else{toast((r&&r.error)||'Post failed',true)}})}
  function clPost(){var d=clGather();var has=(d.title||d.summary||CL_CATS.some(function(c){return clBullets(d.sections[c[0]]).length}));if(!has){toast('Add a title or at least one change first',true);return}
    confirmBox({icon:'📋',title:'Post this changelog?',message:'It will be posted now to '+clChanName(d)+' in the server\'s embed style.',confirmText:'Post now'}).then(function(go){if(!go)return;clSendPost(d,true)})}
  function clRepost(){if(!CL_LAST){toast('Nothing to repost yet',true);return}
    confirmBox({icon:'↻',title:'Repost the last changelog?',message:'Posts the same update ('+esc(CL_LAST.version||CL_LAST.title||'last changelog')+') again to '+clChanName(CL_LAST)+'.',confirmText:'Repost'}).then(function(go){if(!go)return;clSendPost(CL_LAST,false)})}
  function loadBots(){loadFleet();loadBrand();var box=$('#poolList');if(!POOL)box.innerHTML='<div class=sk style=height:68px></div><div class=sk style=height:68px></div>';api('/api/admin/pool').then(function(d){if(!d||!d.ok){box.innerHTML='<div class=pad>'+esc((d&&d.error)||'Could not load.')+'</div>';return}POOL=d;renderPool()})}
  function renderPool(){var d=POOL;if(!d)return;var sw=$('#poolSwitch');sw.classList.toggle('on',!!d.enabled);$('#poolSwitchTxt').textContent=d.enabled?'Hand-outs on':'Hand-outs off';
    var c=d.counts||{};$('#poolStats').innerHTML=[['🤖',c.total,'Pool bots'],['🟢',c.online,'Online'],['🏴',c.serving,'Servers'],['⏳',c.reserved,'Reserved'],['🎟️',c.slots,'Open slots']].map(function(x){return '<div class=pst><span>'+x[0]+'</span><b>'+nf(x[1]||0)+'</b><small>'+x[2]+'</small></div>'}).join('');
    var hb=$('#poolHub');if(hb&&document.activeElement!==hb)hb.value=d.admin_guild||'';var hba=$('#poolHubAdd');if(hba)hba.style.display=(d.admin_guild&&(d.bots||[]).length)?'':'none';
    var cr=d.crypto||{},cw=$('#poolCrypto');if(cr.ok){cw.style.display='none'}else{cw.style.display='';cw.textContent='⚠ '+(cr.why||'Token storage is unavailable.')}
    var all=d.bots||[],q=POOLQ.toLowerCase(),box=$('#poolList');
    if(!all.length){box.innerHTML='<div class=pad>No pool bots yet - add tokens above. Until hand-outs are on, every customer keeps getting your main bot.</div>';return}
    var list=all.filter(function(b){return !q||(b.name||'').toLowerCase().indexOf(q)>=0||String(b.id).indexOf(q)>=0||(b.servers||[]).some(function(s){return (s.name||'').toLowerCase().indexOf(q)>=0||String(s.id).indexOf(q)>=0})});
    if(!list.length){box.innerHTML='<div class=pad>Nothing matches.</div>';return}
    var LBL={online:'Online',off:'Turned off',starting:'Starting…',offline:'Offline',error:'Crashed - retrying',bad_token:'Token rejected',locked:'Locked - re-add token',stopped:'Stopped'};
    box.innerHTML=list.map(function(b){var n=all.indexOf(b)+1,dot=b.online?'on':(b.status==='starting'?'wait':'off');
      var srv=b.servers||[],cap=b.cap||10;
      var chips=srv.map(function(s){var nm=esc(s.name||s.id);return s.in_guild
          ?'<span class="botin pool">🏴 '+nm+' <button class=botx title="Remove this bot from this server" data-pa=free data-b="'+esc(b.id)+'" data-g="'+esc(s.id)+'">×</button></span>'
          :'<span class="botin wait">⏳ '+nm+(s.invite?' <a class=botlink href="'+esc(s.invite)+'" target=_blank rel=noopener>add</a>':'')+' <button class=botx title="Cancel this reservation" data-pa=free data-b="'+esc(b.id)+'" data-g="'+esc(s.id)+'">×</button></span>'}).join('');
      var capb='<span class="botcap'+(b.count>=cap?' full':'')+'" title="Servers this bot serves, of its limit">'+(b.count||0)+'/'+cap+'</span>';
      var serveRow=srv.length?chips:(b.online?'<span class="botin yes">✨ Free · room for '+cap+'</span>':'<span class="botin off">Not available</span>');
      var pct=Math.round((b.load||0)*100),lvl=(b.count>=cap?'full':(pct>=70?'heavy':(pct>=40?'mod':(b.count?'light':'idle'))));
      var slab={idle:'Idle',light:'Light',mod:'Moderate',heavy:'Heavy',full:'Full'}[lvl];
      var lat=(b.latency_ms!=null)?(' · '+b.latency_ms+'ms ping'):'';
      var strain=b.online?'<div class="bstrain '+lvl+'"><div class=sbar><i style="width:'+(b.count?Math.max(pct,7):0)+'%"></i></div><span class=slab>'+slab+' strain · '+(b.count||0)+'/'+cap+' servers'+lat+'</span></div>':'';
      var hub=b.hub_invite?'<a class="sm ghost" href="'+esc(b.hub_invite)+'" target=_blank rel=noopener>🏠 Add to hub</a>':'';
      var w=[];if(!b.public)w.push('Public Bot is off - customers can’t add it. Turn it on in its Developer Portal → Bot page, then Restart.');if(b.code_grant)w.push('“Requires OAuth2 Code Grant” is on - turn it off or invites fail.');if(b.online&&!b.members_intent)w.push('Server Members Intent is off - no welcome messages or full member lists. Turn it on in its Developer Portal → Bot page, then Restart.');if(b.status==='bad_token')w.push('Discord rejected its token - reset the token and paste the new one above.');if(b.status==='locked')w.push('Its saved token can’t be decrypted (the encryption key changed) - paste the token again above.');
      return '<div class="bcard'+(b.off?' boff':'')+'"><div class=bhead><div class=bav>'+(b.avatar?'<img src="'+esc(b.avatar)+'" alt="">':'🤖')+'<span class="bdot '+dot+'"></span></div><div class=bmain><div class=bname>'+esc(b.name)+' <span class=bnum>#'+n+'</span> '+capb+'</div><div class=mt>'+copyspan(b.id,'mono')+' · '+esc(LBL[b.status]||b.status||'')+(b.added_at?' · added '+fmtIso(b.added_at):'')+'</div></div></div>'
        +'<div class=bservers>'+serveRow+'</div>'+strain
        +(b.off&&b.off_reason?'<div class=bwarn style="border-color:rgba(107,114,128,.35);color:#9aa0ab"><div>⏻ '+esc(b.off_reason)+(b.off_at?' · '+fmtTs(b.off_at):'')+'</div></div>':'')
        +(w.length?'<div class=bwarn>'+w.map(function(x){return '<div>⚠ '+esc(x)+'</div>'}).join('')+'</div>':'')
        +'<div class=bacts>'+hub+(b.off?'<button class="sm ghost" data-pa=enable data-b="'+esc(b.id)+'">▶ Turn on</button>':'<button class="sm ghost" data-pa=disable data-b="'+esc(b.id)+'" data-g="'+(b.in_count?'1':'')+'">⏻ Turn off</button><button class="sm ghost" data-pa=restart data-b="'+esc(b.id)+'">⟳ Restart</button>')+'<button class="sm danger" data-pa=remove data-b="'+esc(b.id)+'" data-g="'+(b.count?'1':'')+'">🗑 Remove</button></div></div>'}).join('');
    box.querySelectorAll('[data-pa]').forEach(function(x){x.onclick=function(){poolAct(x)}})}
  function poolAct(x){var a=x.getAttribute('data-pa'),b=x.getAttribute('data-b');
    if(a==='copy'){var v=x.getAttribute('data-v');try{navigator.clipboard.writeText(v);toast('Invite link copied')}catch(e){toast('Copy failed',true)}return}
    if(a==='restart'){x.disabled=true;api('/api/admin/pool/action','POST',{action:'restart',bot_id:b}).then(function(r){x.disabled=false;if(r.ok){toast('Restarting…');setTimeout(loadBots,3000)}else toast(r.error||'Failed',true)});return}
    if(a==='disable'){var g=x.getAttribute('data-g');confirmBox({icon:'⏻',title:'Turn this bot off?',message:g?'This bot is serving a server - turning it off takes that server offline until you turn it back on. Its token and assignments are kept.':'The bot goes offline and stays off across restarts. Its token and assignments are kept - turn it back on anytime.',confirmText:'Turn off',danger:true,input:'Reason (optional) - shows on the status card'}).then(function(reason){if(reason===null)return;x.disabled=true;api('/api/admin/pool/action','POST',{action:'disable',bot_id:b,reason:reason}).then(function(r){x.disabled=false;if(r.ok){toast('Bot turned off');loadBots();loadSubs()}else toast(r.error||'Failed',true)})});return}
    if(a==='enable'){x.disabled=true;api('/api/admin/pool/action','POST',{action:'enable',bot_id:b}).then(function(r){x.disabled=false;if(r.ok){toast('Turning on…');setTimeout(loadBots,3000)}else toast(r.error||'Failed',true)});return}
    if(a==='free'){var g=x.getAttribute('data-g');confirmBox({icon:'↩️',title:'Remove from this server?',message:'The bot leaves this server and that slot frees up. The server can add a bot again from the panel - its subscription isn’t touched.',confirmText:'Remove bot',danger:true}).then(function(ok){if(!ok)return;x.disabled=true;api('/api/admin/pool/action','POST',{action:'free',bot_id:b,guild:g}).then(function(r){x.disabled=false;if(r.ok){toast('Removed from the server');loadBots();loadSubs()}else toast(r.error||'Failed',true)})});return}
    if(a==='remove'){var g=x.getAttribute('data-g');confirmBox({icon:'🗑️',title:'Remove from the pool?',message:g?'This bot is serving a server - removing it makes it leave that server and forgets its token.':'Its token is forgotten. You can add it back any time.',confirmText:'Remove',danger:true}).then(function(ok){if(!ok)return;x.disabled=true;api('/api/admin/pool/action','POST',{action:'remove',bot_id:b,force:!!g}).then(function(r){x.disabled=false;if(r.ok){toast('Removed');loadBots();loadSubs()}else toast(r.error||'Failed',true)})})}}
  function poolAll(a){
    function go(reason){var ids=['poolAllRestart','poolAllOn','poolAllOff'];ids.forEach(function(id){var b=$('#'+id);if(b)b.disabled=true});
      api('/api/admin/pool/action','POST',{action:a,reason:reason||''}).then(function(r){ids.forEach(function(id){var b=$('#'+id);if(b)b.disabled=false});
        if(r.ok){toast((a==='all_off'?'Turned off ':a==='all_on'?'Turning on ':'Restarting ')+(r.count||0)+'/'+(r.total||0)+' bots');setTimeout(function(){loadBots();loadSubs()},a==='all_off'?600:3000)}
        else toast(r.error||'Failed',true)})}
    if(a==='all_off'){confirmBox({icon:'⏻',title:'Turn off every bot?',message:'All pool bots go offline and stay off across restarts. Any server a bot is serving goes offline until you turn the fleet back on - tokens and server assignments are kept.',confirmText:'Turn all off',danger:true,input:'Reason (optional) - shows on the status card'}).then(function(v){if(v!==null)go(v)});return}
    if(a==='all_restart'){confirmBox({icon:'🔄',title:'Restart every bot?',message:'Every enabled bot reconnects. Servers blip offline for a few seconds while they come back.',confirmText:'Restart all'}).then(function(ok){if(ok)go()});return}
    go()}
  function togglePool(){if(!POOL)return;var on=!POOL.enabled;function go(){api('/api/admin/pool/action','POST',{action:'toggle',enabled:on}).then(function(r){if(r.ok){POOL.enabled=r.enabled;renderPool();toast(r.enabled?'Hand-outs on - new servers get their own bot':'Hand-outs off - new servers get the main bot')}else toast(r.error||'Failed',true)})}
    if(on&&!((POOL.counts||{}).free>0)){confirmBox({icon:'🤖',title:'Turn hand-outs on?',message:'No pool bot is online and free right now, so new servers will keep getting the main bot until one is.',confirmText:'Turn on'}).then(function(ok){if(ok)go()})}else go()}
  function saveHub(){var v=(($('#poolHub').value)||'').replace(/[^0-9]/g,'');api('/api/admin/pool/action','POST',{action:'hub',guild:v}).then(function(r){if(r.ok){if(POOL){POOL.admin_guild=r.admin_guild;POOL.main_hub_invite=r.main_hub_invite}$('#poolHubMsg').textContent=r.admin_guild?'Hub saved. Add the bots with the button, or the 🏠 link on each bot.':'Hub server cleared.';loadBots()}else toast(r.error||'Failed',true)})}
  function renderMain(){var d=POOL;if(!d)return;var hb=$('#mainHub');if(hb&&document.activeElement!==hb)hb.value=d.admin_guild||'';var sw=$('#hubCmdSwitch');if(sw){sw.classList.toggle('on',!d.hub_commands_off);var tx=$('#hubCmdSwitchTxt');if(tx)tx.textContent=d.hub_commands_off?'Sub-bot commands off':'Sub-bot commands on'}}
  function setupMainLogs(){var b=$('#mainLogSetup');if(b)b.disabled=true;$('#mainLogSetupMsg').textContent='Creating the admin log channels on the staff bot...';api('/api/admin/pool/action','POST',{action:'hub_logs_setup'}).then(function(r){if(b)b.disabled=false;if(r.ok){$('#mainLogSetupMsg').textContent='Created '+r.created+' admin log channel'+(r.created===1?'':'s')+(r.kept?(', '+r.kept+' already there'):'')+(r.server?(' in '+r.server):'')+'.';toast('Admin log channels ready ('+r.created+' new)')}else{$('#mainLogSetupMsg').textContent='';toast(r.error||'Failed',true)}})}
  function sendAdminTestLogs(){var b=$('#mainTestLogs');if(b)b.disabled=true;$('#mainTestLogsMsg').textContent='Sending...';api('/api/admin/test_logs','POST',{}).then(function(r){if(b)b.disabled=false;if(r&&r.ok){$('#mainTestLogsMsg').textContent='Sent '+(r.sent||0)+' test log(s) - check your admin log channels.';toast('Test logs sent')}else{$('#mainTestLogsMsg').textContent='';toast((r&&r.error)||'Failed',true)}})}
  function loadMain(){api('/api/admin/pool').then(function(d){if(!d||!d.ok){toast((d&&d.error)||'Could not load.',true);return}POOL=d;renderMain()})}
  function saveMainHub(){var v=(($('#mainHub').value)||'').replace(/[^0-9]/g,'');api('/api/admin/pool/action','POST',{action:'hub',guild:v}).then(function(r){if(r.ok){if(POOL){POOL.admin_guild=r.admin_guild;POOL.main_hub_invite=r.main_hub_invite}$('#mainHubMsg').textContent=r.admin_guild?'Saved. Pool bots may sit in this server.':'Hub server cleared.'}else toast(r.error||'Failed',true)})}
  function toggleHubCmds(){var cur=POOL?!!POOL.hub_commands_off:false;var next=!cur;api('/api/admin/pool/action','POST',{action:'hub_cmds',off:next}).then(function(r){if(r.ok){if(POOL)POOL.hub_commands_off=r.hub_commands_off;renderMain();toast(r.hub_commands_off?'Sub-bot commands off in the hub':'Sub-bot commands on in the hub')}else toast(r.error||'Failed',true)})}
  function addAllToHub(){if(!POOL||!POOL.admin_guild){toast('Save a hub server first',true);return}var links=[];if(POOL.main_hub_invite)links.push(POOL.main_hub_invite);(POOL.bots||[]).forEach(function(b){if(b.hub_invite)links.push(b.hub_invite)});if(!links.length){toast('No bots to add yet',true);return}links.forEach(function(u,i){setTimeout(function(){window.open(u,'_blank','noopener')},i*700)});$('#poolHubMsg').textContent='Opening '+links.length+' invite'+(links.length===1?'':'s')+' - click Authorize on each. If pop-ups are blocked, use the 🏠 Add to hub button on each bot below.';}
  function addBots(){var ta=$('#poolTokens'),raw=ta.value.trim();if(!raw){toast('Paste at least one token',true);return}var btn=$('#poolAddBtn');btn.disabled=true;$('#poolAddMsg').textContent='Checking each token with Discord…';
    api('/api/admin/pool/add','POST',{tokens:raw},180000).then(function(r){btn.disabled=false;$('#poolAddMsg').textContent='';var res=(r&&r.results)||[];
      if(!res.length){toast((r&&r.error)||'Failed',true);return}
      ta.value='';
      $('#poolResults').innerHTML='<div class=presults>'+res.map(function(x){x=x||{};return '<div class="pres '+(x.ok?'ok':'bad')+'">'+(x.ok?'✓':'✗')+' Token '+(x.n||'?')+': '+(x.ok?('<b>'+esc(x.name)+'</b> '+(x.updated?'updated':'added')+(x.public===false?' - ⚠ Public Bot is off':'')+(x.members_intent===false?' - ⚠ couldn’t switch on Server Members Intent':'')):esc(x.error||'failed'))+'</div>'}).join('')+'</div>';
      toast((r.added||0)+' bot'+((r.added||0)===1?'':'s')+' added',!(r.added>0));setTimeout(loadBots,2000)})}
  function toggleLive(){LIVE=!LIVE;var b=$('#liveBtn');b.classList.toggle('on',LIVE);b.innerHTML=LIVE?'<span class=live-dot></span> Live':'⟳ Live';if(liveTimer){clearInterval(liveTimer);liveTimer=null}if(LIVE){liveTimer=setInterval(function(){if(document.hidden)return;if(curTab==='subs')loadSubs();else if(curTab==='bans')loadBans();else if(curTab==='shop')loadShop();else if(curTab==='bots')loadBots();else if(curTab==='audit')loadAudit();else if(curTab==='staff')loadStaff()},20000);toast('Live refresh on')}}
  // command palette
  var cmdkItems=[],cmdkSel=0;
  function openCmdk(){var o=$('#cmdk');o.classList.add('show');$('#cmdkInput').value='';cmdkBuild('');setTimeout(function(){$('#cmdkInput').focus()},30)}
  function closeCmdk(){$('#cmdk').classList.remove('show')}
  function cmdkBuild(q){q=(q||'').toLowerCase();var items=[];var nav=[['subs','📂','Subscriptions'],['bans','⛔','Bans'],['shop','🧾','Shop log'],['staff','🛡️','Staff access']];if(STATE.owner){nav.push(['bots','🤖','Bots']);nav.push(['audit','📜','Audit log'])}nav.forEach(function(n){if(!q||n[2].toLowerCase().indexOf(q)>=0)items.push({t:'tab',id:n[0],ic:n[1],label:n[2],sub:'Section'})});SERVERS.forEach(function(s){if(!q||(s.name||'').toLowerCase().indexOf(q)>=0||String(s.id).indexOf(q)>=0)items.push({t:'srv',id:s.id,ic:'🌐',label:s.name,sub:bucket(s)})});cmdkItems=items.slice(0,40);cmdkSel=0;cmdkRender()}
  function cmdkRender(){$('#cmdkList').innerHTML=cmdkItems.length?cmdkItems.map(function(it,i){return '<div class="ci'+(i===cmdkSel?' sel':'')+'" data-i="'+i+'"><span class=cig>'+it.ic+'</span><span>'+esc(it.label)+'</span><span class=cis>'+esc(it.sub)+'</span></div>'}).join(''):'<div class=pad>No matches</div>';$('#cmdkList').querySelectorAll('.ci').forEach(function(el){el.onclick=function(){cmdkSel=parseInt(el.getAttribute('data-i'),10);cmdkGo()}})}
  function cmdkGo(){var it=cmdkItems[cmdkSel];if(!it){return}closeCmdk();if(it.t==='tab'){selectTab(it.id)}else{selectTab('subs');SUBFILTER='all';SUBQ='';$('#subSearch').value=String(it.id);SUBQ=String(it.id);renderFilters();renderSubs();toast('Jumped to '+it.label)}}
  var RL_CHANS=[],RL_BOUND=false;
  function rlGather(){return {title:($('#rlTitle')||{}).value||'',intro:($('#rlIntro')||{}).value||'',footer:($('#rlFooter')||{}).value||'',channel_id:($('#rlChannel')||{}).value||'',ping_role_id:($('#rlPing')||{}).value||'',rules:(($('#rlRules')||{}).value||'').split('\n').map(function(x){return x.trim()}).filter(Boolean)}}
  function rlRenderPreview(){var d=rlGather();var html='<div class=rlp-title>🛡️ '+esc(d.title||'Server Rules')+'</div>';
    if(d.intro)html+='<div class=rlp-intro>'+esc(d.intro)+'</div>';
    if(d.rules.length)html+=d.rules.map(function(r,i){return '<div class=rlp-rule><b>'+(i+1)+'.</b>  '+esc(r)+'</div>'}).join('');
    else html+='<div class=rlp-intro style=color:#949ba4>Add a rule per line to see it here…</div>';
    if(d.footer)html+='<div class=rlp-foot>'+esc(d.footer)+'</div>';$('#rlPrev').innerHTML=html}
  function rlFillChannels(chans){RL_CHANS=chans||[];var sel=$('#rlChannel');if(!sel)return;var cur=sel.value;sel.innerHTML=RL_CHANS.map(function(c){return '<option value="'+c.id+'">#'+esc(c.name)+(c.guild?(' · '+esc(c.guild)):'')+'</option>'}).join('')||'<option value="">No channels</option>';if(cur)sel.value=cur;csSync('rlChannel')}
  function rlInit(){if(RL_BOUND)return;RL_BOUND=true;csEnhance($('#rlChannel'),'#');csEnhance($('#rlPing'),'@');['rlTitle','rlIntro','rlRules','rlFooter'].forEach(function(id){var el=$('#'+id);if(el)el.addEventListener('input',rlRenderPreview)});var sb=$('#rlSaveBtn');if(sb)sb.onclick=rlSave;var pb=$('#rlPostBtn');if(pb)pb.onclick=rlPost;var rb=$('#rlRefresh');if(rb)rb.onclick=function(){loadRules()}}
  function loadRules(){rlInit();api('/api/admin/rules').then(function(d){if(!d||!d.ok)return;var dr=d.draft||{};
    if($('#rlTitle')&&!$('#rlTitle').value)$('#rlTitle').value=dr.title||'';
    if($('#rlIntro')&&!$('#rlIntro').value)$('#rlIntro').value=dr.intro||'';
    if($('#rlFooter')&&!$('#rlFooter').value)$('#rlFooter').value=dr.footer||'';
    if($('#rlRules')&&!$('#rlRules').value&&(dr.rules||[]).length)$('#rlRules').value=(dr.rules||[]).join('\n');
    rlFillChannels(d.channels);if(dr.channel_id&&$('#rlChannel'))$('#rlChannel').value=dr.channel_id;csSync('rlChannel');
    var psel=$('#rlPing');if(psel){var popts='<option value="">No ping</option>';(d.roles||[]).forEach(function(r){popts+='<option value="'+esc(r.id)+'">@'+esc(r.name)+'</option>'});psel.innerHTML=popts;if(dr.ping_role_id)psel.value=dr.ping_role_id;csSync('rlPing')}
    var h=$('#rlChanHint');if(h)h.textContent=d.staff_ok?'':'Staff bot offline - channels may be stale.';rlRenderPreview()})}
  function rlSave(){var btn=$('#rlSaveBtn');btn.disabled=true;api('/api/admin/rules','POST',{action:'save',draft:rlGather()}).then(function(r){btn.disabled=false;toast(r&&r.ok?'Draft saved':((r&&r.error)||'Save failed'),!(r&&r.ok))})}
  function rlPost(){var d=rlGather();if(!d.rules.length&&!d.intro.trim()){toast('Add at least one rule first',true);return}
    var chName=d.channel_id?('#'+(((RL_CHANS.filter(function(c){return c.id===d.channel_id})[0])||{}).name||'channel')):'the default channel';
    confirmBox({icon:'🛡️',title:'Post these rules?',message:'They will be posted now to '+chName+' in the server\'s embed style.',confirmText:'Post now'}).then(function(go){if(!go)return;var btn=$('#rlPostBtn');btn.disabled=true;btn.textContent='Posting…';
      api('/api/admin/rules','POST',{action:'post',draft:d}).then(function(r){btn.disabled=false;btn.textContent='📣 Post now';toast(r&&r.ok?('Posted to '+(r.channel||'the channel')):((r&&r.error)||'Post failed'),!(r&&r.ok))})})}
  document.addEventListener('click',function(e){var t=e.target;if(t.id==='tab-subs')selectTab('subs');if(t.id==='tab-bans')selectTab('bans');if(t.id==='tab-shop')selectTab('shop');if(t.id==='tab-staff')selectTab('staff');if(t.id==='tab-main')selectTab('main');if(t.id==='tab-bots')selectTab('bots');if(t.id==='tab-log')selectTab('log');if(t.id==='tab-rules')selectTab('rules');if(t.id==='tab-audit')selectTab('audit');if(t.id==='auditRefresh')loadAudit();if(t.id==='poolAddBtn')addBots();if(t.id==='poolRefresh')loadBots();if(t.id==='brandApply')applyBrand();var _fb=t.closest&&t.closest('[data-fleet]');if(_fb){fleetPower(_fb.getAttribute('data-fleet'),_fb.getAttribute('data-act'))}if(t.id==='poolAllRestart')poolAll('all_restart');if(t.id==='poolAllOn')poolAll('all_on');if(t.id==='poolAllOff')poolAll('all_off');if(t.id==='poolHubSave')saveHub();if(t.id==='poolHubAdd')addAllToHub();if(t.id==='mainHubSave')saveMainHub();if(t.id==='mainLogSetup')setupMainLogs();if(t.id==='mainTestLogs')sendAdminTestLogs();if(t.id==='hubCmdSwitch'||(t.closest&&t.closest('#hubCmdSwitch')))toggleHubCmds();if(t.id==='poolSwitch'||(t.closest&&t.closest('#poolSwitch')))togglePool();if(t.id==='banBtn')doBan();if(t.id==='staffBtn')addStaff();if(t.id==='cdBtn')customDays();if(t.id==='subRefresh')loadSubs();if(t.id==='liveBtn')toggleLive();if(t.id==='csvBtn')exportCsv();var _sk=t.closest&&t.closest('[data-sk]');if(_sk){SHOPKIND=_sk.getAttribute('data-sk');renderShop()}if(t.id==='tbSend')sendTestBuy();if(t.id==='tlSend')sendTestLogs();if(t.id==='cmdkOpen'||(t.closest&&t.closest('#cmdkOpen')))openCmdk();if(t.id==='cmdk')closeCmdk();if(t.classList&&t.classList.contains('copy')){var v=t.getAttribute('data-copy');try{navigator.clipboard.writeText(v);toast('Copied '+v)}catch(x){toast('Copy failed',true)}}});
  document.addEventListener('input',function(e){var t=e.target;if(t.id==='subSearch'){SUBQ=t.value;renderSubs()}if(t.id==='shopSearch'){SHOPQ=t.value;renderShop()}if(t.id==='banSearch'){BANQ=t.value;renderBans()}if(t.id==='poolSearch'){POOLQ=t.value;renderPool()}if(t.id==='brandBio'){brandCount()}if(t.id==='auditSearch'){AUDITQ=t.value;renderAudit()}if(t.id==='cmdkInput'){cmdkBuild(t.value)}});
  document.addEventListener('change',function(e){if(e.target.id==='subSort'){SUBSORT=e.target.value;renderSubs()}});
  document.addEventListener('keydown',function(e){
    if((e.metaKey||e.ctrlKey)&&(e.key==='k'||e.key==='K')){e.preventDefault();openCmdk();return}
    if($('#cmdk').classList.contains('show')){if(e.key==='Escape'){closeCmdk()}else if(e.key==='ArrowDown'){e.preventDefault();cmdkSel=Math.min(cmdkItems.length-1,cmdkSel+1);cmdkRender()}else if(e.key==='ArrowUp'){e.preventDefault();cmdkSel=Math.max(0,cmdkSel-1);cmdkRender()}else if(e.key==='Enter'){e.preventDefault();cmdkGo()}return}
    if(e.key==='Enter'){var id=e.target.id;if(id==='banId'||id==='banReason')doBan();if(id==='staffId')addStaff();if(id==='cdGuild'||id==='cdDays')customDays()}
  });
  boot();
})();
</script></body></html>"""


def _admin_json(payload, status: int = 200):
    return _aioweb.json_response(payload, status=status,
                                headers={"Cache-Control": "no-store, max-age=0"})


def _admin_ctx(request):
    """(uid, owner_flag) when the requester may use the admin site, else (None, False)."""
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid or not has_admin_access(uid):
        return None, False
    if not _mfa_ok(request, uid):   # 2FA on but not verified this session
        return None, False
    return uid, is_owner(uid)


def _user_label(uid) -> str:
    try:
        u = get_user_any(int(uid))
    except (TypeError, ValueError):
        return ""
    return str(u) if u else ""


async def _h_admin(request):
    return _aioweb.Response(text=_bust(_ADMIN_HTML), content_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})


async def _h_admin_me(request):
    brand = _brand_info()
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid:
        return _admin_json({"authed": False, "reason": "login", "brand": brand})
    prof = _cached_profile(uid) or {}
    if not has_admin_access(uid):
        return _admin_json({"authed": True, "access": False, "user": prof, "brand": brand})
    if not _mfa_ok(request, uid):
        return _admin_json({"authed": True, "access": True, "owner": is_owner(uid),
                            "mfa": True, "support_invite": await _support_invite_resolved(),
                            "user": prof, "brand": brand})
    return _admin_json({"authed": True, "access": True, "owner": is_owner(uid),
                        "user": prof, "brand": brand})


async def _h_admin_mfa_send(request):
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid or not has_admin_access(uid):
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    if not _mfa_on():
        return _admin_json({"ok": False, "error": "Two-step sign-in isn't enabled."}, 400)
    # Stop a stolen session (or a stuck client) spamming code DMs.
    if not _rl_ok(f"mfasend:{uid}", 5, 300):
        return _admin_json({"ok": False, "error": "Too many code requests - wait a few minutes."}, 429)
    ok, err = await _mfa_send_code(uid)
    if not ok:
        return _admin_json({"ok": False, "error": _mfa_friendly(err)}, 502)
    return _admin_json({"ok": True})


async def _h_admin_mfa_verify(request):
    uid = _read_session(request.cookies.get(_COOKIE))
    if not uid or not has_admin_access(uid):
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    if not _mfa_on():
        return _admin_json({"ok": True})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    ok, err = _mfa_verify(uid, str(body.get("code") or ""))
    if not ok:
        if err.startswith("Too many"):
            _audit(request, uid, "mfa_lockout", str(uid), "too many bad codes")
            _fire(_alert_owners(
                f"⚠️ **Multiple failed sign-in codes** for {_user_label(uid) or 'an admin'} "
                f"(`{uid}`) from `{_client_ip(request)}`. If that wasn't them, review admin access."))
        return _admin_json({"ok": False, "error": err}, 400)
    resp = _admin_json({"ok": True})
    resp.set_cookie(_MFA_COOKIE, _sign_mfa(uid), max_age=43200, httponly=True, secure=True, samesite="Lax")
    role = "owner" if is_owner(uid) else "staff"
    _audit(request, uid, "admin_signin", str(uid), role)
    _fire(_alert_owners(
        f"✅ **Admin sign-in** - {_user_label(uid) or 'an admin'} (`{uid}`, {role}) "
        f"signed in to the panel from `{_client_ip(request)}`."))
    return resp


async def _h_admin_overview(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    servers = []
    seen = set()
    for g in sorted(all_guilds(), key=lambda x: (x.member_count or 0), reverse=True):
        seen.add(str(g.id))
        st = effective_state(g.id)
        sub = guild_store(DATA, g.id).get("subscription") or {}
        owner_id = getattr(g, "owner_id", None)
        owner_m = g.get_member(owner_id) if owner_id else None
        owner_name = (str(owner_m) if owner_m else _user_label(owner_id)) if owner_id else ""
        pid = sub.get("purchaser_id") or ""
        me = getattr(g, "me", None)
        main_uid = getattr(getattr(bot, "user", None), "id", None)
        servers.append({
            "id": str(g.id), "name": g.name, "members": g.member_count or 0,
            "icon": (g.icon.url if g.icon else None), "bot_in": True,
            "served_by": str(me.id) if me is not None else "",
            "served_name": (getattr(me, "name", "") or str(me)) if me is not None else "",
            "pool_bot": bool(me is not None and main_uid is not None and me.id != main_uid),
            "mode": st.get("mode"), "active": bool(st.get("active")),
            "status": st.get("status"), "lifetime": bool(st.get("lifetime")),
            "days_left": st.get("days_left"), "expires_at": st.get("expires_at"),
            "plan": st.get("plan") or sub.get("plan") or "",
            "customer_id": st.get("customer_id") or "", "sub_id": st.get("sub_id") or "",
            "owner_id": str(owner_id or ""), "owner_name": owner_name,
            "owner_av": (owner_m.display_avatar.url if owner_m else None),
            "purchaser_id": str(pid), "purchaser_name": (_user_label(pid) if pid else ""),
            "updated_at": sub.get("updated_at"),
            "created_at": int(g.created_at.timestamp()) if getattr(g, "created_at", None) else None,
        })
    # Paid servers the bot is NOT in (removed/kicked) - still worth seeing so the
    # owner notices a customer who's missing the bot. Only surface active/lifetime
    # ones; long-dead test servers stay out.
    for gid_str, store in list((DATA.get("guilds") or {}).items()):
        if gid_str in seen or not str(gid_str).isdigit():
            continue
        sub = (store or {}).get("subscription") or {}
        st = effective_state(int(gid_str))
        if not st.get("active"):
            continue
        pid = sub.get("purchaser_id") or ""
        servers.append({
            "id": str(gid_str), "name": "", "members": 0, "icon": None, "bot_in": False,
            "mode": st.get("mode"), "active": bool(st.get("active")),
            "status": st.get("status"), "lifetime": bool(st.get("lifetime")),
            "days_left": st.get("days_left"), "expires_at": st.get("expires_at"),
            "plan": st.get("plan") or sub.get("plan") or "",
            "customer_id": st.get("customer_id") or "", "sub_id": st.get("sub_id") or "",
            "owner_id": "", "owner_name": "", "owner_av": None,
            "purchaser_id": str(pid), "purchaser_name": (_user_label(pid) if pid else ""),
            "updated_at": sub.get("updated_at"), "created_at": None,
        })
    active = sum(1 for s in servers if s["active"])
    lifetime = sum(1 for s in servers if s["lifetime"])
    total_members = sum(s.get("members") or 0 for s in servers)
    no_bot = sum(1 for s in servers if not s["bot_in"])
    return _admin_json({"ok": True, "owner": owner, "servers": servers,
                        "counts": {"servers": len(servers), "active": active,
                                   "lifetime": lifetime, "members": total_members,
                                   "no_bot": no_bot,
                                   "bans": len(list_bans()), "staff": len(list_admin_staff())}})


async def _h_admin_sub(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    try:
        gid = int(body.get("guild_id"))
    except (TypeError, ValueError):
        return _admin_json({"ok": False, "error": "bad guild"}, 400)
    action = str(body.get("action") or "")
    if action == "add_days":
        try:
            days = int(body.get("days"))
        except (TypeError, ValueError):
            return _admin_json({"ok": False, "error": "bad days"}, 400)
        if days == 0:
            return _admin_json({"ok": False, "error": "days can't be 0"}, 400)
        days = max(-3650, min(3650, days))
        st = grant(gid, days=days, plan=DEFAULT_PLAN, status="active")
    elif action == "lifetime":
        st = grant(gid, lifetime=True, plan=DEFAULT_PLAN, status="active")
    elif action == "revoke":
        st = expire(gid)
    else:
        return _admin_json({"ok": False, "error": "unknown action"}, 400)
    log.info("admin sub action=%s guild=%s by=%s", action, gid, uid)
    _audit(request, uid, f"sub_{action}", str(gid),
           f"{days:+d} days" if action == "add_days" else action)
    return _admin_json({"ok": True, "state": {k: st.get(k) for k in
                       ("active", "mode", "status", "days_left", "expires_at", "lifetime")}})


async def _h_admin_leave(request):
    """Make the bot leave a server. Owner-only - it's destructive and the
    subscription is deliberately left untouched so the bot can be re-added."""
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    if not owner:
        return _admin_json({"ok": False, "error": "Only the bot owner can remove the bot from a server."}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    try:
        gid = int(body.get("guild_id"))
    except (TypeError, ValueError):
        return _admin_json({"ok": False, "error": "bad guild"}, 400)
    g = get_guild_any(gid)
    if g is None:
        return _admin_json({"ok": False, "error": "The bot isn't in that server."}, 404)
    name = g.name
    try:
        await g.leave()
    except discord.HTTPException as exc:
        log.warning("admin leave failed guild=%s: %s", gid, exc)
        return _admin_json({"ok": False, "error": "Discord wouldn't let the bot leave - try again."}, 502)
    log.info("admin leave guild=%s (%s) by=%s", gid, name, uid)
    _audit(request, uid, "bot_leave_server", str(gid), name)
    return _admin_json({"ok": True, "name": name})


# ---- Admin → Bots: the bot pool (owner only) --------------------------------- #

def _origin_ok(request) -> bool:
    """Reject cross-site browser posts - defence in depth on top of the
    SameSite=Lax session cookie and JSON-only bodies."""
    origin = request.headers.get("Origin") or ""
    if not origin:
        return True
    from urllib.parse import urlparse
    host = (urlparse(origin).netloc or "").lower()
    allowed = {(request.host or "").lower(), (request.headers.get("X-Forwarded-Host") or "").lower()}
    try:
        allowed.add((urlparse(_canonical_base(request)).netloc or "").lower())
    except Exception:  # noqa: BLE001
        pass
    allowed.discard("")
    return host in allowed


def _admin_owner_only(request):
    """(uid, None) for the bot owner, else (None, error response)."""
    uid, owner = _admin_ctx(request)
    if uid is None:
        return None, _admin_json({"ok": False, "error": "forbidden"}, 403)
    if not owner:
        return None, _admin_json({"ok": False, "error": "Only the bot owner can manage pool bots."}, 403)
    return uid, None


async def _admin_body(request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


_FLEET_BRAND = {"running": False}


async def _apply_fleet_brand(avatar, banner, bio, by):
    """Push one look to every bot - main + pool. Sequential and gentle; Discord
    rate-limits avatar/banner edits hard, so each bot gets a timeout and the
    per-bot outcome is recorded for the admin to read back. Runs in the
    background so the request returns straight away."""
    import asyncio
    _FLEET_BRAND["running"] = True
    results = []
    clients = [bot, *list(WORKERS)]
    _FLEET_BRAND["progress"] = {"done": 0, "total": len(clients), "results": [], "running": True}
    try:
        for c in clients:
            user = getattr(c, "user", None)
            name = str(getattr(user, "name", "bot"))
            changed, errs = [], []
            ue = {}
            if avatar is not None:
                ue["avatar"] = avatar
            if banner is not None:
                ue["banner"] = banner
            if ue and user is not None:
                try:
                    await asyncio.wait_for(user.edit(**ue), timeout=30)
                    changed += list(ue.keys())
                except asyncio.TimeoutError:
                    errs.append("rate-limited")
                except TypeError:
                    # older discord.py: no banner kwarg - set the avatar alone
                    if "avatar" in ue:
                        try:
                            await asyncio.wait_for(user.edit(avatar=avatar), timeout=30)
                            changed.append("avatar")
                        except Exception:  # noqa: BLE001
                            errs.append("avatar failed")
                    if "banner" in ue:
                        errs.append("banner unsupported")
                except Exception as exc:  # noqa: BLE001
                    errs.append((str(getattr(exc, "text", "") or exc) or "failed")[:50])
            if bio is not None or avatar is not None:
                try:
                    app = getattr(c, "application", None) or await c.application_info()
                    ch = {}
                    if bio is not None:
                        ch["description"] = bio
                    if avatar is not None:
                        ch["icon"] = avatar
                    if ch and hasattr(app, "edit"):
                        await asyncio.wait_for(app.edit(**ch), timeout=30)
                        if "description" in ch:
                            changed.append("bio")
                except asyncio.TimeoutError:
                    errs.append("bio rate-limited")
                except Exception as exc:  # noqa: BLE001
                    errs.append("bio: " + (str(getattr(exc, "text", "") or exc) or "")[:40])
            results.append({"name": name, "changed": changed, "errors": errs})
            _FLEET_BRAND["progress"] = {"done": len(results), "total": len(clients),
                                        "results": list(results), "running": True}
            await asyncio.sleep(1.0)
        # push the same look to the staff bot too (separate service)
        try:
            sres = await _staff_brand_call(avatar, banner, bio)
        except Exception:  # noqa: BLE001
            sres = None
        if sres is not None:
            results.append({"name": sres.get("name") or "Gang Manager Staff",
                            "changed": sres.get("changed") or [],
                            "errors": sres.get("errors") or []})
    finally:
        _FLEET_BRAND["running"] = False
        _FLEET_BRAND["progress"] = {"done": len(results), "total": len(results),
                                    "results": results, "running": False}
        brand = DATA.setdefault("fleet_brand", {})
        brand["last_run"] = {
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "by": str(by),
            "applied": sum(1 for r in results if r["changed"]),
            "total": len(results),
            "results": results,
        }
        save_data(DATA)


async def _h_admin_bot_profile(request):
    """Owner-only. GET returns the saved fleet look + last roll-out; POST takes an
    avatar, a banner and/or a bio (multipart) and pushes them to every bot."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if request.method == "GET":
        brand = DATA.get("fleet_brand", {}) or {}
        try:
            bn = str(bot.user.name) if getattr(bot, "user", None) else ""
        except Exception:  # noqa: BLE001
            bn = ""
        fleet_names = [bn or "Gang Manager"]
        for b in pool_overview().get("bots", []):
            fleet_names.append(b.get("name") or "bot")
        try:
            sp = await _staff_power_call("GET")
            if sp and sp.get("name"):
                fleet_names.append(sp["name"])
        except Exception:  # noqa: BLE001
            pass
        return _admin_json({
            "ok": True,
            "bio": brand.get("bio", ""),
            "avatar_at": brand.get("avatar_at"),
            "banner_at": brand.get("banner_at"),
            "last_run": brand.get("last_run"),
            "running": _FLEET_BRAND["running"],
            "progress": _FLEET_BRAND.get("progress"),
            "bot_name": bn or "Gang Manager",
            "fleet": fleet_names,
        })
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    if _FLEET_BRAND["running"]:
        return _admin_json({"ok": False, "error": "A fleet update is still running - give it a moment."}, 409)
    import asyncio
    import io as _io
    from PIL import Image
    try:
        post = await request.post()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "Couldn't read the upload."}, 400)

    def _read(field):
        f = post.get(field)
        if f is None:
            return None
        try:
            return f.file.read() if hasattr(f, "file") else (
                bytes(f) if isinstance(f, (bytes, bytearray)) else None)
        except Exception:  # noqa: BLE001
            return None

    avatar = _read("avatar") or None
    banner = _read("banner") or None
    bio_raw = post.get("bio")
    bio = None if bio_raw is None else str(bio_raw).strip()[:400]
    if not avatar and not banner and bio is None:
        return _admin_json({"ok": False, "error": "Add an avatar, banner or bio first."}, 400)
    for label, data in (("avatar", avatar), ("banner", banner)):
        if data:
            if len(data) > 8 * 1024 * 1024:
                return _admin_json({"ok": False, "error": f"The {label} is over 8 MB."}, 400)
            try:
                Image.open(_io.BytesIO(data)).verify()
            except Exception:  # noqa: BLE001
                return _admin_json({"ok": False, "error": f"That {label} isn't a readable image."}, 400)

    brand = DATA.setdefault("fleet_brand", {})
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if bio is not None:
        brand["bio"] = bio
    if avatar:
        brand["avatar_at"] = now_iso
    if banner:
        brand["banner_at"] = now_iso
    save_data(DATA)

    n = 1 + len(WORKERS)
    _audit(request, uid, "fleet_brand", "", f"avatar={bool(avatar)} banner={bool(banner)} bio={bio is not None}")
    asyncio.create_task(_apply_fleet_brand(avatar, banner, bio, uid))
    return _admin_json({
        "ok": True, "applying": n,
        "message": f"Rolling out to {n} bot(s). Discord rate-limits avatar changes, "
                   "so a big fleet can take a while - refresh to watch progress.",
    })


def _staff_base():
    return (os.getenv("STAFF_BOT_URL") or "").strip().rstrip("/")


async def _staff_power_call(method, action=None):
    """Talk to the staff bot's /internal/power (GET status, or POST an action).
    Authed with the shared secret. Returns None if the staff bot is unreachable."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    url = base + "/internal/power"
    headers = {"X-Guard-Token": secret}
    try:
        timeout = _aiohttp.ClientTimeout(total=8)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            if method == "GET":
                async with s.get(url, headers=headers) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
            async with s.post(url, headers=headers, json={"action": action}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                return {"ok": False, "error": "staff bot returned " + str(r.status)}
    except Exception:  # noqa: BLE001
        return None


async def _staff_brand_call(avatar, banner, bio):
    """Push the fleet avatar/banner/bio to the staff bot (separate service), so it
    changes together with the pool. Multipart. Returns the staff bot's result, or
    None if it's unreachable."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    try:
        fd = _aiohttp.FormData()
        if avatar is not None:
            fd.add_field("avatar", avatar, filename="avatar.png", content_type="application/octet-stream")
        if banner is not None:
            fd.add_field("banner", banner, filename="banner.png", content_type="application/octet-stream")
        if bio is not None:
            fd.add_field("bio", bio)
        timeout = _aiohttp.ClientTimeout(total=45)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(base + "/internal/brand", data=fd, headers={"X-Guard-Token": secret}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                return {"ok": False, "name": "Gang Manager Staff", "changed": [], "errors": ["staff bot " + str(r.status)]}
    except Exception:  # noqa: BLE001
        return {"ok": False, "name": "Gang Manager Staff", "changed": [], "errors": ["unreachable"]}


async def _h_admin_fleet(request):
    """Owner-only. Status of the two lead bots - the main bot (this service) and
    the staff bot (separate service, fetched live)."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    try:
        main_name = str(bot.user.name) if getattr(bot, "user", None) else "Gang Manager"
    except Exception:  # noqa: BLE001
        main_name = "Gang Manager"
    main = {"name": main_name or "Gang Manager",
            "online": bool(bot.is_ready()) and not MAIN_OFF["v"],
            "off": MAIN_OFF["v"], "ready": bool(bot.is_ready())}
    staff = await _staff_power_call("GET")
    if staff is None:
        staff = {"name": "Gang Manager Staff", "online": False, "off": False, "unreachable": True}
    return _admin_json({"ok": True, "main": main, "staff": staff})


async def _h_admin_fleet_power(request):
    """Owner-only. Turn the main or staff bot off/on, or restart it."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    body = await _admin_body(request)
    target = str(body.get("target") or "")
    action = str(body.get("action") or "").lower()
    if action not in ("off", "on", "restart"):
        return _admin_json({"ok": False, "error": "bad action"}, 400)
    if target == "main":
        # The main bot shares this process with the web panel, so a true
        # process restart isn't safe here - "restart" just brings it back online
        # (clears maintenance + restores presence). "off" goes invisible + gated.
        await set_main_power(action == "off")
        _audit(request, uid, "main_power", "", action)
        return _admin_json({"ok": True, "main_off": MAIN_OFF["v"]})
    if target == "staff":
        res = await _staff_power_call("POST", action)
        if res is None:
            return _admin_json({"ok": False, "error": "Couldn't reach the staff bot."}, 502)
        _audit(request, uid, "staff_power", "", action)
        return _admin_json(res if isinstance(res, dict) else {"ok": True})
    return _admin_json({"ok": False, "error": "bad target"}, 400)


# ---- changelog composer ---------------------------------------------------- #
_CHANGELOG_CATS = ("added", "improved", "fixed", "changed", "removed")


async def _staff_changelog_call(method, payload=None):
    """Talk to the staff bot's /internal/changelog - GET lists postable channels,
    POST renders + posts the changelog in the server's embed style. Authed with
    the shared secret. Returns None if the staff bot is unreachable."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    url = base + "/internal/changelog"
    headers = {"X-Guard-Token": secret}
    try:
        timeout = _aiohttp.ClientTimeout(total=15)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            if method == "GET":
                async with s.get(url, headers=headers) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
            async with s.post(url, headers=headers, json=payload or {}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                try:
                    j = await r.json(content_type=None)
                except Exception:  # noqa: BLE001
                    j = {}
                return {"ok": False, "error": (j or {}).get("error") or ("staff bot returned " + str(r.status))}
    except Exception:  # noqa: BLE001
        return None


async def _staff_rules_call(method, payload=None):
    """Talk to the staff bot's /internal/rules - GET lists postable channels, POST
    renders + posts the rules embed. Authed with the shared secret."""
    base = _staff_base()
    secret = (os.getenv("MFA_SECRET") or os.getenv("WEBHOOK_SECRET") or "").strip()
    if not base or not secret or _aiohttp is None:
        return None
    url = base + "/internal/rules"
    headers = {"X-Guard-Token": secret}
    try:
        timeout = _aiohttp.ClientTimeout(total=15)
        async with _aiohttp.ClientSession(timeout=timeout) as s:
            if method == "GET":
                async with s.get(url, headers=headers) as r:
                    return await r.json(content_type=None) if r.status == 200 else None
            async with s.post(url, headers=headers, json=payload or {}) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                try:
                    j = await r.json(content_type=None)
                except Exception:  # noqa: BLE001
                    j = {}
                return {"ok": False, "error": (j or {}).get("error") or ("staff bot returned " + str(r.status))}
    except Exception:  # noqa: BLE001
        return None


def _sanitise_rules(d):
    """Normalise a submitted rules draft into the shape the staff bot expects."""
    d = d if isinstance(d, dict) else {}
    raw = d.get("rules")
    if isinstance(raw, list):
        rules = [str(x).strip()[:300] for x in raw if str(x).strip()]
    else:
        rules = [ln.strip()[:300] for ln in str(raw or "").splitlines() if ln.strip()]
    return {
        "title": str(d.get("title") or "")[:120],
        "intro": str(d.get("intro") or "")[:600],
        "footer": str(d.get("footer") or "")[:200],
        "channel_id": "".join(ch for ch in str(d.get("channel_id") or "") if ch.isdigit())[:25],
        "ping_role_id": "".join(ch for ch in str(d.get("ping_role_id") or "") if ch.isdigit())[:25],
        "rules": rules[:40],
    }


async def _h_admin_rules(request):
    """Owner-only. GET returns the saved rules draft + the staff bot's postable
    channels; POST saves (action=save) or posts (action=post) the rules embed."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    rl = DATA.setdefault("rules", {})
    rl.setdefault("draft", {})
    if request.method == "GET":
        channels, roles, staff_ok, guild_name, brand = [], [], False, "", ""
        res = await _staff_rules_call("GET")
        if isinstance(res, dict) and res.get("ok"):
            channels = res.get("channels") or []
            roles = res.get("roles") or []
            guild_name = res.get("guild") or ""
            brand = res.get("brand") or ""
            staff_ok = True
        return _admin_json({"ok": True, "draft": rl.get("draft") or {}, "channels": channels,
                            "roles": roles, "staff_ok": staff_ok, "guild": guild_name, "brand": brand})
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    body = await _admin_body(request)
    action = str(body.get("action") or "save").lower()
    draft = _sanitise_rules(body.get("draft") or {})
    rl["draft"] = draft
    if action == "save":
        save_data(DATA)
        _audit(request, uid, "rules_save", "", draft.get("title") or "")
        return _admin_json({"ok": True, "saved": True, "draft": draft})
    if action == "post":
        if not draft.get("rules") and not str(draft.get("intro") or "").strip():
            return _admin_json({"ok": False, "error": "Add at least one rule before posting."}, 400)
        res = await _staff_rules_call("POST", {**draft, "by": uid})
        if res is None:
            save_data(DATA)
            return _admin_json({"ok": False, "error": "Couldn't reach the staff bot to post."}, 502)
        if not res.get("ok"):
            save_data(DATA)
            return _admin_json({"ok": False, "error": res.get("error") or "Post failed."}, 502)
        save_data(DATA)
        _audit(request, uid, "rules_post", res.get("channel") or "", draft.get("title") or "")
        return _admin_json({"ok": True, "posted": True, "url": res.get("url") or "",
                            "channel": res.get("channel") or "", "draft": draft})
    return _admin_json({"ok": False, "error": "bad action"}, 400)


def _sanitise_changelog(d):
    """Normalise a submitted draft into the shape the staff bot expects."""
    d = d if isinstance(d, dict) else {}
    out = {
        "title": str(d.get("title") or "")[:120],
        "version": str(d.get("version") or "")[:40],
        "date": str(d.get("date") or "")[:40],
        "summary": str(d.get("summary") or "")[:600],
        "channel_id": "".join(ch for ch in str(d.get("channel_id") or "") if ch.isdigit())[:25],
        "ping_role_id": "".join(ch for ch in str(d.get("ping_role_id") or "") if ch.isdigit())[:25],
        "sections": {},
    }
    secs = d.get("sections") if isinstance(d.get("sections"), dict) else {}
    for cat in _CHANGELOG_CATS:
        raw = secs.get(cat)
        if isinstance(raw, list):
            lines = [str(x).strip()[:300] for x in raw if str(x).strip()]
        else:
            lines = [ln.strip()[:300] for ln in str(raw or "").splitlines() if ln.strip()]
        out["sections"][cat] = lines[:25]
    return out


def _changelog_has_content(d):
    if str(d.get("title") or "").strip() or str(d.get("summary") or "").strip():
        return True
    return any((d.get("sections") or {}).get(c) for c in _CHANGELOG_CATS)


async def _h_admin_changelog(request):
    """Owner-only. GET returns the saved draft + the staff bot's postable channels;
    POST either saves the draft (action=save) or posts it (action=post)."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    cl = DATA.setdefault("changelog", {})
    cl.setdefault("draft", {})
    cl.setdefault("history", [])
    if request.method == "GET":
        channels, roles, staff_ok, guild_name, brand = [], [], False, "", ""
        res = await _staff_changelog_call("GET")
        if isinstance(res, dict) and res.get("ok"):
            channels = res.get("channels") or []
            roles = res.get("roles") or []
            guild_name = res.get("guild") or ""
            brand = res.get("brand") or ""
            staff_ok = True
        return _admin_json({
            "ok": True,
            "draft": cl.get("draft") or {},
            "history": (cl.get("history") or [])[:5],
            "channels": channels, "roles": roles, "staff_ok": staff_ok,
            "guild": guild_name, "brand": brand,
        })
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    body = await _admin_body(request)
    action = str(body.get("action") or "save").lower()
    draft = _sanitise_changelog(body.get("draft") or {})
    cl["draft"] = draft
    if action == "save":
        save_data(DATA)
        _audit(request, uid, "changelog_save", "", draft.get("version") or "")
        return _admin_json({"ok": True, "saved": True, "draft": draft})
    if action == "post":
        if not _changelog_has_content(draft):
            return _admin_json({"ok": False, "error": "Add a title or at least one change before posting."}, 400)
        res = await _staff_changelog_call("POST", {**draft, "by": uid})
        if res is None:
            save_data(DATA)
            return _admin_json({"ok": False, "error": "Couldn't reach the staff bot to post."}, 502)
        if not res.get("ok"):
            save_data(DATA)
            return _admin_json({"ok": False, "error": res.get("error") or "Post failed."}, 502)
        entry = {
            "version": draft.get("version") or "", "title": draft.get("title") or "",
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "by": uid, "url": res.get("url") or "", "channel": res.get("channel") or "",
        }
        cl["history"] = ([entry] + (cl.get("history") or []))[:20]
        save_data(DATA)
        _audit(request, uid, "changelog_post", res.get("channel") or "", draft.get("version") or "")
        return _admin_json({
            "ok": True, "posted": True, "url": res.get("url") or "",
            "channel": res.get("channel") or "", "history": cl["history"][:5],
        })
    return _admin_json({"ok": False, "error": "bad action"}, 400)


async def _h_admin_pool(request):
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    data = pool_overview()
    hub = str(data.get("admin_guild") or "")
    for b in data["bots"]:
        b["hub_invite"] = _invite_url(hub, client_id=b["id"]) if hub else ""
        for s in b.get("servers", []):
            s["invite"] = _invite_url(s["id"], client_id=b["id"])
    data["main_hub_invite"] = _invite_url(hub) if hub else ""
    data["hub_commands_off"] = hub_commands_off()
    data["ok"] = True
    return _admin_json(data)


async def _h_admin_pool_add(request):
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    raw = str((await _admin_body(request)).get("tokens") or "")
    if len(raw) > 20000:
        return _admin_json({"ok": False, "error": "That's too much at once - paste up to about 100 tokens."}, 400)
    res = await add_tokens(raw)
    log.info("admin pool add by=%s added=%s", uid, res.get("added"))
    _audit(request, uid, "pool_add_tokens", "", f"added {res.get('added')}")
    return _admin_json(res, 200 if res.get("ok") else 400)


async def _h_admin_pool_action(request):
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    body = await _admin_body(request)
    action = str(body.get("action") or "")
    if action == "toggle":
        on = set_pool_enabled(bool(body.get("enabled")))
        log.info("admin pool toggle=%s by=%s", on, uid)
        _audit(request, uid, "pool_handouts", "", "on" if on else "off")
        return _admin_json({"ok": True, "enabled": on})
    if action == "hub":
        g = set_admin_guild(body.get("guild"))
        log.info("admin pool hub=%s by=%s", g or "(cleared)", uid)
        _audit(request, uid, "pool_hub", str(g or ""), "cleared" if not g else "")
        return _admin_json({"ok": True, "admin_guild": g,
                            "main_hub_invite": _invite_url(g) if g else ""})
    if action == "hub_cmds":
        off = set_hub_commands_off(bool(body.get("off")))
        log.info("admin pool hub_cmds off=%s by=%s", off, uid)
        _audit(request, uid, "pool_hub_cmds", "", "off" if off else "on")
        return _admin_json({"ok": True, "hub_commands_off": off})
    if action == "hub_logs_setup":
        # Build the admin-log channels on the GM Support staff bot (purchases,
        # subscriptions, sign-ins, security, bans, staff, pool, servers).
        res = await _staff_gmlogs_call()
        if res is None:
            return _admin_json({"ok": False,
                "error": "Couldn't reach the staff bot. Check STAFF_BOT_URL + the shared secret."}, 502)
        if not res.get("ok"):
            return _admin_json({"ok": False, "error": res.get("error") or "Setup failed on the staff bot."}, 502)
        log.info("admin gmlogs setup created=%s kept=%s by=%s", res.get("created"), res.get("kept"), uid)
        _audit(request, uid, "admin_logs_setup", str(res.get("created") or 0),
               f"{res.get('kept') or 0} kept")
        return _admin_json({"ok": True, "created": res.get("created", 0), "kept": res.get("kept", 0),
                            "category": res.get("category", ""), "server": res.get("guild", "")})
    if action in ("all_off", "all_on", "all_restart"):
        if action == "all_off":
            res = await disable_all_bots(reason=str(body.get("reason") or ""), by=uid)
        elif action == "all_on":
            res = await enable_all_bots()
        else:
            res = await restart_all_bots()
        log.info("admin pool %s by=%s -> %s/%s", action, uid, res.get("count"), res.get("total"))
        _audit(request, uid, f"pool_{action}", "", f"{res.get('count')}/{res.get('total')} bots")
        return _admin_json(res)
    bid = str(body.get("bot_id") or "")
    if not bid.isdigit():
        return _admin_json({"ok": False, "error": "bad bot id"}, 400)
    try:
        if action == "free":
            res = await free_bot(bid, body.get("guild") or None)
        elif action == "remove":
            res = await remove_bot(bid, force=bool(body.get("force")))
        elif action == "restart":
            res = await restart_bot(bid)
        elif action == "disable":
            res = await disable_bot(bid, reason=str(body.get("reason") or ""), by=uid)
        elif action == "enable":
            res = await enable_bot(bid)
        else:
            return _admin_json({"ok": False, "error": "unknown action"}, 400)
    except KeyError:
        return _admin_json({"ok": False, "error": "That bot isn't in the pool."}, 404)
    except ValueError as exc:
        return _admin_json({"ok": False, "error": str(exc)}, 400)
    log.info("admin pool action=%s bot=%s by=%s", action, bid, uid)
    _audit(request, uid, f"pool_{action}", bid)
    return _admin_json(res if isinstance(res, dict) else {"ok": True})


async def _h_admin_bans(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    bans = []
    for b in list_bans():
        name = b.get("name") or ""
        avatar = None
        try:
            u = get_user_any(int(b.get("id")))
            if u:
                name = name or str(u)
                avatar = u.display_avatar.url
        except (TypeError, ValueError):
            pass
        by_name = ""
        by = b.get("by")
        if by:
            by_name = _user_label(by) or (_cached_profile(by) or {}).get("name", "")
        bans.append({**b, "name": name, "avatar": avatar, "by_name": by_name})
    return _admin_json({"ok": True, "bans": bans})


async def _h_admin_ban(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    raw = str(body.get("user_id") or "").strip()
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return _admin_json({"ok": False, "error": "That isn't a valid Discord ID."}, 400)
    if is_owner(tid):
        return _admin_json({"ok": False, "error": "You can't ban a bot operator."}, 400)
    reason = str(body.get("reason") or "")[:200]
    ban_user(tid, by=uid, name=_user_label(tid), reason=reason)
    _audit(request, uid, "ban", str(tid), reason)
    return _admin_json({"ok": True})


async def _h_admin_unban(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    target = str(body.get("user_id") or "").strip()
    unban_user(target)
    _audit(request, uid, "unban", target)
    return _admin_json({"ok": True})


async def _h_admin_staff(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    staff = []
    for s in list_admin_staff():
        name = s.get("name") or ""
        if not name:
            prof = _cached_profile(s.get("id")) or {}
            name = prof.get("name") or ""
            if not name and str(s.get("id")).isdigit():
                try:  # resolve from Discord once, then remember it
                    u = await bot.fetch_user(int(s["id"]))
                    name = getattr(u, "global_name", None) or u.name or ""
                except Exception:  # noqa: BLE001
                    name = ""
            if name:
                set_admin_staff_name(s["id"], name)
        staff.append({**s, "name": name})
    return _admin_json({"ok": True, "owner": owner, "staff": staff})


async def _h_admin_staff_add(request):
    uid, owner = _admin_ctx(request)
    if uid is None or not owner:
        return _admin_json({"ok": False, "error": "Only the bot owner can manage staff."}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    raw = str(body.get("user_id") or "").strip()
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return _admin_json({"ok": False, "error": "That isn't a valid Discord ID."}, 400)
    name = _user_label(tid)
    if not name:
        try:  # pull their real Discord name even if we share no server
            u = await bot.fetch_user(tid)
            name = getattr(u, "global_name", None) or u.name or ""
        except Exception:  # noqa: BLE001
            name = ""
    add_admin_staff(tid, by=uid, name=name)
    _audit(request, uid, "staff_add", str(tid), name)
    return _admin_json({"ok": True})


async def _h_admin_staff_remove(request):
    uid, owner = _admin_ctx(request)
    if uid is None or not owner:
        return _admin_json({"ok": False, "error": "Only the bot owner can manage staff."}, 403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _admin_json({"ok": False, "error": "bad json"}, 400)
    target = str(body.get("user_id") or "").strip()
    remove_admin_staff(target)
    _audit(request, uid, "staff_remove", target)
    return _admin_json({"ok": True})


async def _h_admin_shop_log(request):
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    rows = []
    for e in shop_log_list(300):
        gname = ""
        try:
            g = get_guild_any(int(e.get("guild_id")))
            gname = g.name if g else ""
        except (TypeError, ValueError):
            pass
        rows.append({**e, "guild_name": gname})
    servers = {}
    for c in [bot, *WORKERS]:
        for g in getattr(c, "guilds", []) or []:
            if g.id not in servers:
                try:
                    active = bool(subscription_state(g.id).get("active"))
                except Exception:  # noqa: BLE001
                    active = False
                servers[g.id] = {"id": str(g.id), "name": g.name, "active": active}
    srv_list = sorted(servers.values(), key=lambda s: (not s["active"], s["name"].lower()))[:200]
    return _admin_json({"ok": True, "log": rows, "servers": srv_list})


async def _h_admin_test_logs(request):
    """Owner-only: fire one sample of every admin log kind through the real
    forwarder, so each lands in its log channel looking exactly like the live log,
    just with test info."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    me = f"You\n<@{uid}> · `{uid}`"
    tsrv = "Test Server\n`123456789012345678`"
    ip = {"name": "🌐 IP", "value": "Client: `198.51.100.24`\nVia (edge): `172.16.0.5`", "inline": True}
    dev = {"name": "🖥️ Device", "value": "Chrome · Windows", "inline": True}
    samples = [
        ("purchase", "🧪 Test - new purchase",
         "A customer bought a plan. Test info - nothing was charged.",
         [{"name": "🏠 Server", "value": tsrv, "inline": True},
          {"name": "👤 Buyer", "value": me, "inline": True},
          {"name": "💵 Amount", "value": "**$9.99 USD**", "inline": True},
          {"name": "📦 Plan", "value": "Gang Manager Monthly", "inline": True},
          {"name": "📧 Email", "value": "buyer@example.com", "inline": True},
          {"name": "🌍 Country", "value": "US", "inline": True}, ip]),
        ("subscription", "🧪 Test - subscription granted",
         "An admin granted a subscription to a server via the staff bot.",
         [{"name": "🏠 Server", "value": tsrv, "inline": True},
          {"name": "⚙️ Action", "value": "`grant`", "inline": True},
          {"name": "📦 Change", "value": "+30 day(s) (monthly)", "inline": True},
          {"name": "👤 By", "value": f"<@{uid}>", "inline": True},
          {"name": "📊 Status now", "value": "✅ Active · 30d left", "inline": True},
          {"name": "🏷️ Plan", "value": "monthly", "inline": True}]),
        ("signin", "🧪 Test - admin sign-in",
         "Someone signed in to the admin panel.",
         [{"name": "👤 Account", "value": me, "inline": True},
          {"name": "⚙️ Action", "value": "`admin_signin`", "inline": True}, ip, dev,
          {"name": "🌍 Country", "value": "AU", "inline": True},
          {"name": "🗣️ Language", "value": "en-AU", "inline": True},
          {"name": "📄 From page", "value": "`/admin`", "inline": True},
          {"name": "🔎 Full user-agent",
           "value": "`Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0`", "inline": False}]),
        ("security", "🧪 Test - failed sign-in",
         "A sign-in code was entered wrong a few times.",
         [{"name": "👤 Account", "value": me, "inline": True},
          {"name": "⚙️ Action", "value": "`signin_failed`", "inline": True},
          {"name": "📝 Detail", "value": "3 wrong codes - locked out 10m", "inline": False}, ip, dev]),
        ("moderation", "🧪 Test - user blocked",
         "A user was banned from the bot.",
         [{"name": "👤 Account", "value": me, "inline": True},
          {"name": "⚙️ Action", "value": "`ban_add`", "inline": True},
          {"name": "🎯 Target", "value": "`123456789012345678`", "inline": True},
          {"name": "📝 Detail", "value": "Chargeback", "inline": False}, ip, dev]),
        ("staff", "🧪 Test - staff access changed",
         "Admin staff access was updated.",
         [{"name": "👤 Account", "value": me, "inline": True},
          {"name": "⚙️ Action", "value": "`staff_add`", "inline": True},
          {"name": "🎯 Target", "value": f"`{uid}`", "inline": True},
          {"name": "📝 Detail", "value": "Added as staff", "inline": False}, ip, dev]),
        ("pool", "🧪 Test - pool action",
         "A pool bot was handed out to a server.",
         [{"name": "👤 Account", "value": me, "inline": True},
          {"name": "⚙️ Action", "value": "`pool_assign`", "inline": True},
          {"name": "🤖 Bot", "value": "Bot 3", "inline": True},
          {"name": "🏠 Server", "value": tsrv, "inline": True}]),
        ("server", "🧪 Test - bot added to a server",
         "The bot was added to a server.",
         [{"name": "🏠 Server", "value": tsrv, "inline": True},
          {"name": "👥 Members", "value": "128", "inline": True},
          {"name": "👑 Owner", "value": f"<@{uid}>", "inline": True}]),
    ]
    for kind, title, desc, fields in samples:
        _fire(_gmlog(kind, title, description=desc, fields=fields))
    _audit(request, uid, "test_logs", "", f"{len(samples)} kinds")
    return _admin_json({"ok": True, "sent": len(samples)})


async def _h_admin_test_purchase(request):
    """Owner-only: fire the real purchase flow for a chosen user - DM them a test
    invoice and post the purchase log - without charging or recording anything."""
    uid, err = _admin_owner_only(request)
    if err is not None:
        return err
    if not _origin_ok(request):
        return _admin_json({"ok": False, "error": "Cross-site request blocked."}, 403)
    body = await _admin_body(request)
    target = "".join(c for c in str(body.get("user_id") or "") if c.isdigit())
    if not (5 <= len(target) <= 25):
        return _admin_json({"ok": False, "error": "Enter the Discord user ID to send the test to."}, 400)
    try:
        amt = int(round(float(str(body.get("amount") or "9.99").replace(",", ".")) * 100))
    except (TypeError, ValueError):
        amt = 999
    amt = max(0, min(amt, 9_999_999))
    cur = (str(body.get("currency") or "USD").upper()[:8]) or "USD"
    sid = "".join(c for c in str(body.get("server_id") or "") if c.isdigit())
    server = (str(body.get("server") or "").strip())[:100]
    if sid:
        g = get_guild_any(sid)
        if g is not None:
            server = g.name
    server = server or "Test Server"
    if await _in_support(target) is False:
        return _admin_json({"ok": False, "error": "That user isn't in the support server - they need to join GM Support first."}, 400)
    invoice = "test_" + secrets.token_hex(8)
    # Record it (flagged as a test) so the invoice actually verifies in a Purchase
    # Help ticket and the real server/bot/sub show up - excluded from revenue.
    try:
        shop_log_add({
            "ts": int(time.time()), "kind": "test",
            "guild_id": sid, "email": "", "ip": "",
            "amount": amt, "currency": cur, "customer_id": "",
            "purchaser_id": target, "session_id": invoice,
        })
    except Exception:  # noqa: BLE001
        log.info("test shop log write skipped")
    dm_ok = await _dm_invoice(target, invoice, server, amt, cur, is_renewal=False)
    _fire(_gmlog(
        "purchase",
        "🧪 TEST purchase",
        description=f"Test purchase sent to <@{target}> by an admin - no charge, no record created.",
        fields=[
            {"name": "👤 Buyer", "value": f"<@{target}>\n`{target}`", "inline": True},
            {"name": "💵 Amount", "value": f"**{amt/100:.2f} {cur}**", "inline": True},
            {"name": "🏠 Server", "value": (f"{server}\n`{sid}`" if sid else server), "inline": True},
            {"name": "🧾 Invoice ID", "value": f"`{invoice}`", "inline": False},
            {"name": "📩 Invoice DM", "value": ("Delivered ✅" if dm_ok else "Couldn't DM (DMs closed / no shared server)"), "inline": True},
            {"name": "🧪 Note", "value": "Test event - no payment taken and no subscription granted.", "inline": False},
        ],
    ))
    _audit(request, uid, "test_purchase", target, f"{amt/100:.2f} {cur}")
    return _admin_json({"ok": True, "invoice": invoice, "dm": "sent" if dm_ok else "blocked"})


async def _h_admin_audit_log(request):
    """The admin-action audit trail. Owner-only - it records what staff did."""
    uid, owner = _admin_ctx(request)
    if uid is None:
        return _admin_json({"ok": False, "error": "forbidden"}, 403)
    if not owner:
        return _admin_json({"ok": False, "error": "Only the bot owner can view the audit log."}, 403)
    return _admin_json({"ok": True, "log": audit_log_list(300)})


async def start_web_server() -> None:
    """Start the roster web server if PORT is configured. Safe to call once in
    on_ready; a no-op (with a log line) when disabled or aiohttp.web is absent."""
    global _web_started
    if _web_started:
        return
    port = os.getenv("PORT", "")
    if not port.isdigit():
        log.info("PORT not set - web roster manager is off.")
        return
    if _aioweb is None:
        log.warning("aiohttp.web unavailable - web roster manager is off.")
        return
    if not _configured():
        log.warning(
            "PORT is set but DISCORD_CLIENT_SECRET is not - the website loads but "
            "nobody can log in until you set it."
        )
    @_aioweb.middleware
    async def _security_headers(request, handler):
        """Per-IP abuse limiting + baseline hardening headers on every response."""
        def _apply(h):
            h.setdefault("X-Content-Type-Options", "nosniff")
            h.setdefault("X-Frame-Options", "SAMEORIGIN")
            h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
            h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
            h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), browsing-topics=()")
            # Enforcing CSP. Scripts keep 'unsafe-inline' because the panel uses
            # inline on* handlers, but everything else is locked down: no plugins,
            # no framing, no base-tag hijack, forms/XHR only to our own origin,
            # fonts from Google, images from self/Discord. This blocks the common
            # XSS *exfiltration* and object/base/frame vectors even if an escape slips.
            h.setdefault("Content-Security-Policy", _CSP)

        path = request.path
        # Never throttle the Stripe webhook - it must always get through.
        if not path.startswith("/billing/webhook"):
            ip = _client_ip(request)
            blocked = False
            if path in ("/login", "/callback"):
                blocked = not _rl_ok(f"auth:{ip}", 40, 60)        # logins per IP / min
            elif request.method == "POST" and path.startswith("/api/"):
                blocked = not _rl_ok(f"api:{ip}", 150, 60)        # API writes per IP / min
            if blocked:
                resp = _aioweb.json_response(
                    {"error": "Too many requests - give it a moment."},
                    status=429, headers={"Retry-After": "30"},
                )
                _apply(resp.headers)
                return resp

        # CSRF defence in depth: reject cross-site browser POSTs to the API
        # (the Stripe webhook isn't under /api/, and server-to-server calls send
        # no Origin header, so neither is affected).
        if request.method == "POST" and path.startswith("/api/") and not _origin_ok(request):
            resp = _aioweb.json_response({"ok": False, "error": "Cross-site request blocked."}, status=403)
            _apply(resp.headers)
            return resp

        try:
            resp = await handler(request)
        except _aioweb.HTTPException as exc:
            _apply(exc.headers)
            raise
        _apply(resp.headers)
        return resp

    app = _aioweb.Application(middlewares=[_security_headers], client_max_size=12 * 1024 * 1024)
    app.add_routes(
        [
            _aioweb.get("/", _h_landing),
            _aioweb.get("/panel", _h_index),
            *[_aioweb.get(path, _h_asset) for path in _ASSETS],
            _aioweb.get("/terms", _h_terms),
            _aioweb.get("/privacy", _h_privacy),
            _aioweb.get("/pricing", _h_landing),
            _aioweb.get("/invite", _h_invite),
            _aioweb.get("/api/stats", _h_stats),
            _aioweb.get("/api/stats/", _h_stats),
            _aioweb.get("/stats", _h_stats),
            _aioweb.get("/billing/checkout", _h_billing_checkout),
            _aioweb.get("/billing/success", _h_billing_success),
            _aioweb.get("/billing/portal", _h_billing_portal),
            _aioweb.get("/api/sub_check", _h_sub_check),
            _aioweb.get("/api/support_lookup", _h_support_lookup),
            _aioweb.get("/api/support_verify", _h_support_verify),
            _aioweb.post("/internal/subscription", _h_internal_subscription),
            _aioweb.post("/api/sub_move", _h_sub_move),
            _aioweb.post("/billing/webhook", _h_billing_webhook),
            _aioweb.get("/login", _h_login),
            _aioweb.get("/callback", _h_callback),
            _aioweb.get("/logout", _h_logout),
            _aioweb.get("/api/me", _h_me),
            _aioweb.get("/api/access", _h_access),
            _aioweb.get("/api/overview", _h_overview),
            _aioweb.get("/api/roster", _h_roster),
            _aioweb.get("/api/kos", _h_kos),
            _aioweb.post("/api/role", _h_role),
            _aioweb.post("/api/remove", _h_remove),
            _aioweb.post("/api/kos_add", _h_kos_add),
            _aioweb.post("/api/kos_remove", _h_kos_remove),
            _aioweb.get("/api/stash", _h_stash),
            _aioweb.post("/api/stash", _h_stash_set),
            _aioweb.post("/api/stash_remove", _h_stash_remove),
            _aioweb.post("/api/stash_scan", _h_stash_scan),
            _aioweb.get("/api/wars", _h_wars),
            _aioweb.post("/api/war_start", _h_war_start),
            _aioweb.post("/api/war_round", _h_war_round),
            _aioweb.post("/api/war_end", _h_war_end),
            _aioweb.post("/api/war_cancel", _h_war_cancel),
            _aioweb.get("/api/orders", _h_orders),
            _aioweb.post("/api/order_create", _h_order_create),
            _aioweb.post("/api/order_paid", _h_order_paid),
            _aioweb.post("/api/order_close", _h_order_close),
            _aioweb.post("/api/orders_clear_closed", _h_orders_clear_closed),
            _aioweb.post("/api/ticket_type_create", _h_ticket_type_create),
            _aioweb.post("/api/ticket_type_remove", _h_ticket_type_remove),
            _aioweb.get("/api/strikes", _h_strikes),
            _aioweb.post("/api/strike_remove", _h_strike_remove),
            _aioweb.post("/api/strike_clear", _h_strike_clear),
            _aioweb.get("/api/eois", _h_eois),
            _aioweb.post("/api/eoi_close", _h_eoi_close),
            _aioweb.post("/api/eoi_accept", _h_eoi_accept),
            _aioweb.get("/api/settings", _h_settings),
            _aioweb.post("/api/setting_set", _h_setting_set),
            _aioweb.post("/api/welcome_test", _h_welcome_test),
            _aioweb.post("/api/web_toggle", _h_web_toggle),
            _aioweb.post("/api/web_member_add", _h_web_member_add),
            _aioweb.post("/api/web_member_remove", _h_web_member_remove),
            _aioweb.post("/api/logs_setup", _h_logs_setup),
            _aioweb.post("/api/sub_admin", _h_sub_admin),
            _aioweb.get("/admin", _h_admin),
            _aioweb.get("/api/admin/me", _h_admin_me),
            _aioweb.get("/api/admin/overview", _h_admin_overview),
            _aioweb.post("/api/admin/sub", _h_admin_sub),
            _aioweb.post("/api/admin/leave", _h_admin_leave),
            _aioweb.get("/api/admin/pool", _h_admin_pool),
            _aioweb.get("/api/admin/bot_profile", _h_admin_bot_profile),
            _aioweb.post("/api/admin/bot_profile", _h_admin_bot_profile),
            _aioweb.get("/api/admin/fleet", _h_admin_fleet),
            _aioweb.post("/api/admin/fleet/power", _h_admin_fleet_power),
            _aioweb.get("/api/admin/changelog", _h_admin_changelog),
            _aioweb.post("/api/admin/changelog", _h_admin_changelog),
            _aioweb.get("/api/admin/rules", _h_admin_rules),
            _aioweb.post("/api/admin/rules", _h_admin_rules),
            _aioweb.post("/api/admin/pool/add", _h_admin_pool_add),
            _aioweb.post("/api/admin/pool/action", _h_admin_pool_action),
            _aioweb.get("/api/admin/bans", _h_admin_bans),
            _aioweb.post("/api/admin/ban", _h_admin_ban),
            _aioweb.post("/api/admin/unban", _h_admin_unban),
            _aioweb.get("/api/admin/staff", _h_admin_staff),
            _aioweb.post("/api/admin/staff_add", _h_admin_staff_add),
            _aioweb.post("/api/admin/staff_remove", _h_admin_staff_remove),
            _aioweb.post("/api/admin/mfa/send", _h_admin_mfa_send),
            _aioweb.post("/api/admin/mfa/verify", _h_admin_mfa_verify),
            _aioweb.get("/api/admin/shop_log", _h_admin_shop_log),
            _aioweb.post("/api/admin/test_purchase", _h_admin_test_purchase),
            _aioweb.post("/api/admin/test_logs", _h_admin_test_logs),
            _aioweb.get("/api/admin/audit_log", _h_admin_audit_log),
        ]
    )
    runner = _aioweb.AppRunner(app)
    await runner.setup()
    site = _aioweb.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    _web_started = True
    log.info("🌐 Roster web manager listening on :%s", port)


# --------------------------------------------------------------------------- #
# The page (vanilla JS, no external deps)
# --------------------------------------------------------------------------- #

# RAW string on purpose: served byte-for-byte as written. A plain string lets
# Python rewrite JS escapes (e.g. "\n" -> a real newline inside a JS string),
# which is a syntax error that kills the whole panel script.
_PAGE_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gang Manager</title>
<!--GM_HEAD-->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@700&family=Oswald:wght@500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#07070a; --card:rgba(20,20,26,.72); --cardsolid:#15151b; --line:rgba(255,255,255,.07);
    --text:#f2f2f6; --mute:#9a9aa8; --brand:#2f6bff; --brand2:#5b93ff; --acc:#2f6bff;
    --ok:#22c55e; --bad:#ef4444; --warn:#f59e0b; --blue:#60a5fa; --purple:#a78bfa; --r:18px;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{background:var(--bg);color:var(--text);min-height:100vh;
    font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased}
  body::before{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;
    background:
      radial-gradient(900px 520px at 12% -8%, rgba(255,255,255,.22), transparent 60%),
      radial-gradient(760px 520px at 102% 2%, rgba(130,20,70,.20), transparent 55%),
      radial-gradient(640px 640px at 50% 118%, rgba(60,10,36,.28), transparent 62%);
    animation:drift 20s ease-in-out infinite alternate}
  @keyframes drift{to{transform:translate3d(0,-14px,0) scale(1.04)}}
  a{text-decoration:none;color:inherit}
  ::selection{background:rgba(255,255,255,.35)}

  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
    padding:16px 22px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(7,7,10,.92),rgba(7,7,10,.72));backdrop-filter:blur(12px)}
  .logo{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;font-size:18px;overflow:hidden;
    background:linear-gradient(135deg,#2a0b16,#4c0f22);border:1px solid rgba(255,255,255,.4);
    box-shadow:0 0 22px rgba(255,255,255,.45)}
  .logo.hasimg{background:#0d0d12}
  .logo img{width:100%;height:100%;object-fit:cover;display:block}
  .brand{font-family:"Space Grotesk",sans-serif;font-size:19px;font-weight:700;letter-spacing:.5px;
    background:linear-gradient(90deg,#fff,#e8e8ee);-webkit-background-clip:text;background-clip:text;color:transparent;flex:1}
  .brand small{font-family:"Inter";font-weight:500;font-size:12px;color:var(--mute);letter-spacing:0;display:block;margin-top:-2px}

  .wrap{max-width:900px;margin:0 auto;padding:20px 18px 60px}
  button{font:inherit;cursor:pointer;border:1px solid var(--line);background:rgba(255,255,255,.04);
    color:var(--text);padding:9px 14px;border-radius:11px;transition:.16s ease}
  button:hover{border-color:rgba(255,255,255,.55);background:rgba(255,255,255,.1);transform:translateY(-1px)}
  button:active{transform:translateY(0)}
  button:disabled{opacity:.5;cursor:default;transform:none}
  button.brand-btn{background:linear-gradient(135deg,var(--brand),#3a3a42);border-color:transparent;
    color:#fff;font-weight:700;box-shadow:0 8px 24px rgba(255,255,255,.35)}
  button.ghost{background:transparent}
  button.danger{border-color:rgba(239,68,68,.3);color:#ffb0b0;background:transparent}
  button.danger:hover{background:var(--bad);border-color:var(--bad);color:#fff}
  input[type=text],input[type=number],input:not([type]),select{font:inherit;background:rgba(255,255,255,.04);
    border:1px solid var(--line);color:var(--text);padding:11px 14px;border-radius:12px;outline:none;transition:.16s}
  .cand input,.addbar input{padding:9px 12px}
  input:focus,select:focus{border-color:rgba(255,255,255,.55);background:rgba(255,255,255,.06)}
  select option{background:#15151a;color:#ececf1}
  select option:checked{background:#2a2a33}
  .muted{color:var(--mute)}

  /* ===== gate / login / no-access screens ===== */
  #gate.gate{position:fixed;inset:0;z-index:40;display:grid;place-items:center;padding:24px;overflow:hidden;
    background:linear-gradient(150deg,#050507 0%,#0e0e12 46%,#4a4a52 80%,#ededf2 100%)}
  #gate.gate::before{content:"";position:absolute;inset:-30%;z-index:0;pointer-events:none;
    background:
      conic-gradient(from 0deg at 50% 50%, rgba(255,255,255,.22), rgba(130,20,90,.12), rgba(60,10,40,.20), rgba(255,255,255,.22));
    filter:blur(40px);animation:aurora 16s linear infinite}
  #gate.gate::after{content:"";position:absolute;inset:0;z-index:0;pointer-events:none;opacity:.5;
    background:
      radial-gradient(420px 420px at 18% 22%, rgba(255,255,255,.28), transparent 60%),
      radial-gradient(520px 520px at 84% 78%, rgba(120,20,80,.26), transparent 60%);
    animation:orbfloat 9s ease-in-out infinite alternate}
  @keyframes aurora{to{transform:rotate(360deg)}}
  @keyframes orbfloat{to{transform:translate3d(0,-26px,0) scale(1.08)}}
  .gatecard{position:relative;z-index:1;max-width:440px;width:100%;text-align:center;padding:44px 34px 36px;
    border-radius:28px;background:linear-gradient(180deg,rgba(26,16,22,.86),rgba(12,12,17,.9));
    border:1px solid rgba(255,255,255,.08);backdrop-filter:blur(22px) saturate(1.2);
    box-shadow:0 40px 120px rgba(0,0,0,.7),0 0 0 1px rgba(255,255,255,.08) inset;
    animation:gatepop .55s cubic-bezier(.2,.9,.25,1.3) both}
  .gatecard::before{content:"";position:absolute;inset:-1px;border-radius:29px;padding:1px;pointer-events:none;
    background:linear-gradient(135deg,rgba(255,255,255,.65),transparent 40%,transparent 60%,rgba(255,255,255,.5));
    -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;mask-composite:exclude;opacity:.8;animation:bordshift 6s ease-in-out infinite alternate}
  @keyframes gatepop{from{opacity:0;transform:translateY(22px) scale(.94)}to{opacity:1;transform:none}}
  @keyframes bordshift{to{filter:hue-rotate(-18deg)}}
  .gate .gicon{position:relative;width:96px;height:96px;margin:0 auto 22px;border-radius:50%;display:grid;place-items:center;
    font-size:44px;background:linear-gradient(135deg,#2a0b16,#4c0f22);
    box-shadow:0 0 50px rgba(255,255,255,.55),0 10px 30px rgba(0,0,0,.5);animation:iconfloat 4.5s ease-in-out infinite}
  .gate .gicon::before{content:"";position:absolute;inset:-7px;border-radius:50%;z-index:-1;
    background:conic-gradient(from 0deg,#9a9aa8,#c9c9d2,#3a3a42,#9a9aa8);filter:blur(7px);opacity:.85;
    animation:ringspin 4s linear infinite}
  .gate .gicon .glogo{width:100%;height:100%;border-radius:50%;object-fit:cover;display:block;
    border:2px solid rgba(255,255,255,.12)}
  .gate .gicon .gemoji{line-height:1}
  .gate .gicon .gbadge{position:absolute;right:-4px;bottom:-4px;width:34px;height:34px;border-radius:50%;
    display:grid;place-items:center;font-size:18px;background:#15151b;border:2px solid rgba(255,255,255,.5);
    box-shadow:0 4px 14px rgba(0,0,0,.5)}
  @keyframes iconfloat{50%{transform:translateY(-8px)}}
  @keyframes ringspin{to{transform:rotate(360deg)}}
  .gatecard h2{margin:0 0 10px;font-family:"Space Grotesk",sans-serif;font-size:27px;font-weight:800;letter-spacing:.3px;
    background:linear-gradient(90deg,#fff,#e8e8ee);-webkit-background-clip:text;background-clip:text;color:transparent}
  .gatecard p{margin:0 auto;max-width:330px;color:var(--mute);line-height:1.65;font-size:14.5px}
  #gateActions{margin-top:26px}
  #gateActions button{width:100%;padding:14px;font-size:15px;border-radius:14px}
  #gateActions a{display:block}
  .discordbtn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:13px;
    background:#5865f2;border:1px solid #5865f2;border-radius:13px;color:#fff;font-weight:700;font-size:15px;cursor:pointer;
    box-shadow:0 12px 34px rgba(88,101,242,.45);transition:.16s}
  .discordbtn:hover{filter:brightness(1.08);transform:translateY(-1px)}

  /* stat tiles */
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:12px;margin:4px 0 20px}
  .stat{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:14px 16px;backdrop-filter:blur(10px);transition:.2s;animation:rise .5s both}
  .stat:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.4)}
  .stat .n{font-size:26px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
  .stat .l{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.7px;margin-top:6px;font-weight:600}
  .stat .ic{position:absolute;right:10px;top:9px;font-size:16px;opacity:.5}
  .stat::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--brand);opacity:.8}
  .stat.brand .n{color:#e2e2e8} .stat.brand::after{background:var(--brand)}
  .stat.bad .n{color:#ff8a8a} .stat.bad::after{background:var(--bad)}
  .stat.warn .n{color:#ffcf6b} .stat.warn::after{background:var(--warn)}
  .stat.ok .n{color:#86efac} .stat.ok::after{background:var(--ok)}
  .stat.blue .n{color:#93c5fd} .stat.blue::after{background:var(--blue)}
  .stat.purple .n{color:#c4b5fd} .stat.purple::after{background:var(--purple)}
  @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

  /* tabs */
  .tabs{display:inline-flex;gap:4px;padding:4px;border-radius:14px;background:rgba(255,255,255,.04);
    border:1px solid var(--line);margin-bottom:16px}
  .tab{padding:8px 18px;border-radius:10px;cursor:pointer;color:var(--mute);font-weight:600;transition:.18s}
  .tab.active{background:linear-gradient(135deg,var(--brand),#3a3a42);color:#fff;box-shadow:0 6px 18px rgba(255,255,255,.4)}

  .toolbar{display:flex;gap:10px;margin-bottom:16px;align-items:center}
  .toolbar input{flex:1}
  .count{font-size:13px;color:var(--mute);margin-left:auto;white-space:nowrap}

  .ranklabel{display:flex;align-items:center;gap:10px;margin:22px 2px 10px;font-weight:700;font-size:12px;
    text-transform:uppercase;letter-spacing:1px;color:#e8e8ee}
  .ranklabel::before{content:"";width:3px;height:15px;border-radius:3px;background:linear-gradient(var(--brand),#3a3a42)}
  .ranklabel .pill{margin-left:auto;background:rgba(255,255,255,.05);border:1px solid var(--line);
    border-radius:999px;padding:2px 10px;font-size:11px;color:var(--mute)}

  /* strikes */
  .sflag{display:inline-flex;align-items:center;gap:3px;font-size:10.5px;font-weight:700;padding:1px 7px;
    border-radius:999px;margin-left:8px;vertical-align:middle;background:rgba(245,158,11,.14);
    color:#fcd34d;border:1px solid rgba(245,158,11,.35)}
  .sflag.over{background:rgba(239,68,68,.16);color:#fca5a5;border-color:rgba(239,68,68,.4)}
  .scount{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap}
  .sbar{display:flex;gap:3px;margin-top:6px}
  .sbar i{width:17px;height:7px;border-radius:2px;background:rgba(255,255,255,.08)}
  .sbar i.on{background:linear-gradient(90deg,#f59e0b,#ef4444)}
  .slog{margin-top:9px;display:flex;flex-direction:column;gap:6px;border-top:1px solid var(--line);padding-top:9px}
  .slog .row{font-size:12px;color:var(--mute);display:flex;gap:7px;align-items:baseline;flex-wrap:wrap}
  .slog .row b{color:var(--text);font-weight:600}
  .slog .sid{font-family:ui-monospace,monospace;opacity:.6;font-size:11px}
  .slog .srm{cursor:pointer;color:var(--mute);font-weight:800;font-size:12px;opacity:.5;transition:.15s;margin-left:2px}
  .slog .srm:hover{opacity:1;color:#fca5a5}
  /* war extras */
  .wmeta{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:5px;font-size:12px;color:var(--mute)}
  .wrounds{display:flex;gap:3px;margin-top:9px;flex-wrap:wrap}
  .wrounds i{width:13px;height:13px;border-radius:3px;background:rgba(255,255,255,.08)}
  .wrounds i.w{background:#22c55e}
  .wrounds i.l{background:#ef4444}
  .wrate{height:6px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;margin-top:9px;max-width:240px}
  .wrate>span{display:block;height:100%;background:linear-gradient(90deg,#22c55e,#4ade80)}
  .warbtns{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
  .wb{padding:7px 13px;border-radius:10px;border:1px solid var(--line);background:rgba(255,255,255,.05);color:#e8e8ee;font-weight:700;font-size:13px;cursor:pointer;transition:filter .15s,border-color .15s}
  .wb:hover{filter:brightness(1.15)}
  .wb.win{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.14);color:#86efac}
  .wb.loss{border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.14);color:#fca5a5}
  .wb.end{border-color:rgba(245,158,11,.4);color:#fcd34d}
  .wb.undo{color:var(--mute)}

  ul.members{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
  li.member{display:flex;align-items:center;gap:13px;background:var(--card);border:1px solid var(--line);
    border-radius:15px;padding:12px 14px;backdrop-filter:blur(8px);transition:.18s;animation:rise .4s both}
  li.member:hover{border-color:rgba(255,255,255,.35);background:rgba(255,255,255,.04)}
  li.member .av{width:44px;height:44px;border-radius:50%;flex:none;padding:2px;
    background:linear-gradient(135deg,var(--brand),#2c2c33)}
  li.member .av img{width:100%;height:100%;border-radius:50%;object-fit:cover;background:#000;display:block}
  li.member .av.skull{display:grid;place-items:center;font-size:20px;background:linear-gradient(135deg,#3a0f0f,#5c1212)}
  .mname{font-weight:700;font-size:15px}
  .grow{flex:1;min-width:0}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px;align-items:center}
  .chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;
    background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.28);color:#e8e8ee;transition:.15s}
  .chip.rankrole{background:rgba(96,165,250,.12);border-color:rgba(96,165,250,.3);color:#bcd4ff}
  .chip .x{cursor:pointer;opacity:0;width:0;overflow:hidden;transition:.15s;font-weight:800}
  li.member:hover .chip .x{opacity:.75;width:11px;margin-left:1px}
  .chip .x:hover{opacity:1;color:#fff}
  .chip.locked{opacity:.55}
  .addsel{background:rgba(255,255,255,.03);border:1px dashed var(--line);color:var(--mute);
    border-radius:999px;padding:3px 10px;font-size:11px;cursor:pointer}
  .reason{font-size:12px;color:var(--mute);margin-top:3px}

  .empty{text-align:center;color:var(--mute);padding:46px 0}
  .spinner{width:26px;height:26px;border-radius:50%;border:3px solid rgba(255,255,255,.12);
    border-top-color:var(--brand);animation:spin .7s linear infinite;margin:40px auto}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* toasts */
  #toasts{position:fixed;top:78px;right:14px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:360px}
  .toast{padding:12px 15px;border-radius:13px;font-size:14px;font-weight:500;color:#fff;
    background:rgba(20,20,26,.92);border:1px solid var(--line);backdrop-filter:blur(10px);
    box-shadow:0 10px 30px rgba(0,0,0,.5);animation:toastin .25s ease}
  .toast.ok{border-color:rgba(34,197,94,.45);background:rgba(16,40,24,.92)}
  .toast.err{border-color:rgba(239,68,68,.5);background:rgba(42,16,16,.92)}
  @keyframes toastin{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
  @media(max-width:560px){#toasts{left:14px;right:14px;max-width:none}}

  /* in-site confirm dialog (replaces native confirm()) */
  .confirm{position:fixed;inset:0;z-index:120;display:none;align-items:center;justify-content:center;padding:20px;
    background:rgba(4,4,8,.66);backdrop-filter:blur(5px);opacity:0;transition:opacity .16s}
  .confirm.show{display:flex;opacity:1}
  .confirmbox{width:100%;max-width:400px;background:rgba(18,18,24,.98);border:1px solid rgba(255,255,255,.14);
    border-radius:20px;padding:26px 24px 22px;text-align:center;box-shadow:0 40px 110px rgba(0,0,0,.65);
    transform:translateY(12px) scale(.97);transition:.22s cubic-bezier(.2,.7,.2,1)}
  .confirm.show .confirmbox{transform:none}
  .confirmbox .ci2{font-size:34px;line-height:1;margin-bottom:10px}
  .confirmbox h4{margin:0 0 8px;font-size:18px;font-weight:800;letter-spacing:-.2px}
  .confirmbox p{margin:0 0 20px;color:var(--mute);font-size:14px;line-height:1.55}
  .confirmbtns{display:flex;gap:10px;justify-content:center}
  .confirmbtns button{flex:1;max-width:170px;padding:11px 16px;border-radius:12px;font-weight:700;font-size:14px;cursor:pointer}
  .danger-solid{background:linear-gradient(135deg,#ef4444,#b91c1c)!important;border-color:transparent!important;color:#fff!important}
  footer{color:var(--mute);font-size:12px;text-align:center;padding:30px 18px}

  .banner{padding:12px 15px;border-radius:13px;margin-bottom:14px;font-size:14px;border:1px solid var(--line);background:rgba(20,20,26,.7)}
  .banner a{color:#fff;font-weight:650}
  .banner.bad{border-color:rgba(239,68,68,.5);background:rgba(42,16,16,.6)}
  .banner.warn{border-color:rgba(234,179,8,.45);background:rgba(40,34,12,.6)}
  .setuptop{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
  .pbar{height:9px;border-radius:6px;background:rgba(255,255,255,.08);margin-top:12px;overflow:hidden}
  .pfill{height:100%;background:linear-gradient(90deg,#c9c9d2,#ffffff);border-radius:6px;transition:width .4s}
  .setrow{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05)}
  .setrow:last-child{border-bottom:none}
  .setrow label{flex:1;font-size:14px;min-width:120px}
  .setrow select{flex:1;max-width:260px}
  .setrow.toggle{border-bottom:none}
  .tgl{padding:7px 18px;border-radius:10px;font-weight:650;border:1px solid var(--line);background:rgba(255,255,255,.06);color:var(--mute);cursor:pointer}
  .tgl.on{background:rgba(34,197,94,.22);border-color:rgba(34,197,94,.5);color:#bdf0c8}
  .card h4{margin:0 0 8px;font-size:15px}
  .wmlist{display:flex;flex-wrap:wrap;gap:8px}
  .wmchip{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border-radius:10px;font-size:13px;font-weight:600;
    background:rgba(255,255,255,.07);border:1px solid var(--line)}
  .wmchip .x{background:transparent;border:none;color:var(--mute);cursor:pointer;font-size:13px;padding:0;line-height:1}
  .wmchip .x:hover{color:#ff8b8b}

  .addbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .addbar .i{flex:1;min-width:150px}.addbar .q{width:92px}
  .iconbox{width:42px;height:42px;flex:none;border-radius:11px;display:grid;place-items:center;font-size:21px;
    background:rgba(255,255,255,.05);border:1px solid var(--line)}
  .iconimg{width:42px;height:42px;flex:none;border-radius:11px;object-fit:cover;border:1px solid var(--line);
    background:#000;image-rendering:auto}
  .wchip{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--mute)}
  .wi{width:60px;padding:3px 7px !important;border-radius:8px;font-size:11px;text-align:center}
  .cand .thumb{width:36px;height:36px;flex:none;border-radius:8px;object-fit:cover;border:1px solid var(--line);background:#000}
  .cand .temoji{width:36px;height:36px;flex:none;border-radius:8px;display:grid;place-items:center;
    background:rgba(255,255,255,.05);border:1px solid var(--line);font-size:18px}
  .drop{border:2px dashed var(--line);border-radius:16px;padding:22px;text-align:center;cursor:pointer;
    transition:.18s;background:rgba(255,255,255,.02);margin-bottom:14px}
  .drop:hover,.drop.over{border-color:rgba(255,255,255,.6);background:rgba(255,255,255,.07)}
  .drop b{font-size:15px}
  .cand{display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
  .cand .ci{flex:1}.cand .cq{width:78px}
  .sitem{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);
    border-radius:13px;padding:10px 14px;backdrop-filter:blur(8px)}
  .sitem .q{display:flex;align-items:center;gap:7px}
  .sitem .q button{padding:4px 11px;border-radius:9px;font-weight:800}
  .sitem .qn{min-width:46px;text-align:center;font-weight:800;font-variant-numeric:tabular-nums}
  .warcard{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--line);
    border-radius:14px;padding:14px 16px;backdrop-filter:blur(8px)}
  .warcard .score{font-size:23px;font-weight:800;font-variant-numeric:tabular-nums}
  .badge{font-size:11px;font-weight:700;padding:2px 10px;border-radius:999px;text-transform:uppercase;letter-spacing:.5px}
  .badge.live{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.4)}
  .badge.done{background:rgba(255,255,255,.05);color:var(--mute);border:1px solid var(--line)}

  /* stash: category groups, tags, sort */
  .scat{display:flex;align-items:center;gap:9px;margin:16px 2px 9px;font-weight:800;font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:1px}
  .scat .ic{font-size:14px}
  .scat::before{content:"";width:3px;height:14px;border-radius:2px;background:var(--brand)}
  .scat .pill{margin-left:auto;background:rgba(255,255,255,.05);border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:10.5px;letter-spacing:.4px;color:var(--mute);text-transform:none;font-weight:700}
  .stcat-tag{font-size:10px;font-weight:700;padding:2px 8px;border-radius:999px;background:rgba(255,255,255,.07);border:1px solid var(--line);color:var(--mute);text-transform:uppercase;letter-spacing:.4px}
  .stsort{display:flex;align-items:center;justify-content:flex-end;gap:8px;font-size:12px;color:var(--mute);margin:2px 2px 12px}
  .stsort select{padding:6px 10px;border-radius:10px;background:rgba(0,0,0,.3);border:1px solid var(--line);color:var(--text);font:inherit;font-size:12px}

  /* ===== premium pass: gate-grade styling across the whole panel ===== */
  body::before{
    background:
      radial-gradient(1100px 660px at 8% -12%, rgba(255,255,255,.30), transparent 58%),
      radial-gradient(940px 640px at 106% -2%, rgba(150,20,95,.24), transparent 55%),
      radial-gradient(820px 820px at 50% 122%, rgba(74,12,46,.34), transparent 62%) !important}
  /* living aurora swirling behind the whole app */
  body::after{content:"";position:fixed;inset:-40%;z-index:-1;pointer-events:none;opacity:.55;
    background:conic-gradient(from 0deg at 50% 50%, rgba(255,255,255,.20), rgba(120,20,90,.05), rgba(60,10,44,.18), rgba(255,255,255,.12), rgba(255,255,255,.20));
    filter:blur(64px);animation:aurora 26s linear infinite}
  header{border-bottom:none;position:sticky;
    background:
      linear-gradient(180deg,rgba(10,7,12,.94),rgba(10,7,12,.55)) padding-box,
      linear-gradient(90deg,transparent,rgba(255,255,255,.6),transparent) border-box;
    border-bottom:1px solid transparent;backdrop-filter:blur(14px) saturate(1.1)}
  .logo{position:relative;z-index:1}
  .logo::before{content:"";position:absolute;inset:-4px;border-radius:14px;z-index:-1;
    background:conic-gradient(from 0deg,#9a9aa8,#c9c9d2,#3a3a42,#9a9aa8);filter:blur(7px);opacity:.75;
    animation:ringspin 5s linear infinite}
  @keyframes ringspin{to{transform:rotate(360deg)}}
  .brand{text-shadow:0 0 30px rgba(255,255,255,.25)}

  .stat{border:1px solid rgba(255,255,255,.16);
    background:linear-gradient(180deg,rgba(28,17,23,.72),rgba(14,14,19,.72));
    box-shadow:0 10px 34px rgba(0,0,0,.45)}
  .stat::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;z-index:0;
    background:radial-gradient(150px 90px at 86% -10%, rgba(255,255,255,.16), transparent 70%)}
  .stat>*{position:relative;z-index:1}
  .stat:hover{transform:translateY(-4px) scale(1.015);border-color:rgba(255,255,255,.5);
    box-shadow:0 20px 48px rgba(255,255,255,.22)}
  .stat .n{text-shadow:0 0 24px rgba(255,255,255,.3)}

  .tabs{background:linear-gradient(180deg,rgba(28,17,23,.6),rgba(14,14,19,.6));
    border:1px solid rgba(255,255,255,.08);box-shadow:0 10px 30px rgba(0,0,0,.4)}
  .tab:hover{color:#ffffff}
  .tab.active{animation:tabglow 2.6s ease-in-out infinite}
  @keyframes tabglow{50%{box-shadow:0 8px 28px rgba(255,255,255,.7)}}

  li.member,.warcard,.sitem{box-shadow:0 8px 26px rgba(0,0,0,.35)}
  li.member:hover,.warcard:hover,.sitem:hover{box-shadow:0 16px 42px rgba(255,255,255,.16)}
  button.brand-btn{box-shadow:0 12px 30px rgba(255,255,255,.42)}
  .toolbar input:focus,#search:focus{outline:none;border-color:rgba(255,255,255,.5);
    box-shadow:0 0 0 3px rgba(255,255,255,.14)}
  .ranklabel{color:#c9c9d2}

  /* ===== orders / tickets tab ===== */
  .obar{margin:2px 0 14px}
  .olist{display:flex;flex-direction:column;gap:11px}
  .ocard{background:linear-gradient(180deg,rgba(26,16,22,.72),rgba(14,14,19,.72));
    border:1px solid var(--line);border-radius:16px;padding:15px 17px;box-shadow:0 8px 26px rgba(0,0,0,.35)}
  .ocard.dim{opacity:.6}
  .ohead{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
  .otitle{font-weight:700;font-size:15px}
  .otitle .oid{color:var(--mute);font-family:ui-monospace,monospace;font-size:12px;font-weight:600}
  .obadges{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .obadge{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;border:1px solid var(--line)}
  .obadge.paid{background:rgba(34,197,94,.15);color:#86efac;border-color:rgba(34,197,94,.4)}
  .obadge.unpaid{background:rgba(239,68,68,.14);color:#fca5a5;border-color:rgba(239,68,68,.38)}
  .obadge.closed{background:rgba(255,255,255,.05);color:var(--mute)}
  .opill{font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;background:rgba(255,255,255,.05);
    border:1px solid var(--line);color:var(--mute)}
  .opill.urgent{color:#fca5a5;border-color:rgba(239,68,68,.4)}
  .opill.high{color:#fdba74;border-color:rgba(245,158,11,.4)}
  .ometa{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;font-size:13px;color:var(--mute)}
  .ometa b{color:var(--text);font-weight:600}
  .oactions{margin-top:11px;display:flex;align-items:center;gap:10px}
  .oactions button{padding:7px 13px;font-size:13px;border-radius:11px}
  .oform{display:flex;flex-direction:column;gap:10px;margin-bottom:14px}
  .ofrow{display:flex;gap:10px;flex-wrap:wrap}
  .ofrow>select,.ofrow>input{flex:1;min-width:160px}
  .oform select,.oform input,.oform textarea{width:100%;padding:11px 13px;border-radius:12px;
    background:rgba(0,0,0,.3);border:1px solid var(--line);color:var(--text);font:inherit;font-size:14px}
  .oform textarea{resize:vertical}
  .oform select:focus,.oform input:focus,.oform textarea:focus{outline:none;border-color:rgba(255,255,255,.5);
    box-shadow:0 0 0 3px rgba(255,255,255,.14)}
  .tlist{display:flex;flex-direction:column;gap:6px}
  .trow{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-radius:10px;
    background:rgba(0,0,0,.25);border:1px solid var(--line);font-size:14px}
  .trow .srm{cursor:pointer;color:var(--mute);font-weight:800;opacity:.6;transition:.15s}
  .trow .srm:hover{opacity:1;color:#fca5a5}
  /* order summary panel */
  .osum{margin-bottom:14px;display:flex;flex-direction:column;gap:13px}
  .osum-h{font-weight:700}
  .osum-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  .osum-tiles>div{background:rgba(0,0,0,.28);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .osum-tiles b{display:block;font-size:24px;font-weight:800;font-variant-numeric:tabular-nums;color:#c9c9d2;
    text-shadow:0 0 20px rgba(255,255,255,.3)}
  .osum-tiles span{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mute)}
  .otwrap{overflow-x:auto}
  .otable{width:100%;border-collapse:collapse;font-size:13.5px}
  .otable th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mute);
    padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  .otable td{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:top}
  .otable tbody tr:last-child td{border-bottom:none}
  .otable td b{color:var(--text);font-weight:600}
  @media(max-width:560px){.osum-tiles{grid-template-columns:repeat(2,1fr)}}

  /* ===== GM logo + black↔white gradient backdrop (content keeps its colour) ===== */
  body{background:linear-gradient(150deg,#050507 0%,#0e0e12 46%,#4a4a52 80%,#ededf2 100%) fixed}
  body::before,body::after{display:none}
  #gate.gate::before,#gate.gate::after{display:none}  /* clean B&W login - no coloured aurora */
  .logo.hasimg{width:66px;height:40px;background:transparent;border:none;box-shadow:none;border-radius:0;overflow:hidden}
  .logo.hasimg::before{display:none}
  .logo.hasimg img{width:100%;height:100%;object-fit:contain}
  .gate .gicon{width:auto;max-width:74%;height:auto;border-radius:16px;background:transparent;box-shadow:none;animation:iconfloat 4.5s ease-in-out infinite}
  .gate .gicon::before{display:none}
  .gate .gicon .glogo{width:auto;max-width:260px;height:auto;border-radius:10px;object-fit:contain;border:none;filter:drop-shadow(0 8px 30px rgba(255,255,255,.18))}
  .gate .gicon .gbadge{right:-6px;bottom:-6px}
  /* ---- Settings cards ---- */
  #settingsView .card{position:relative;background:rgba(14,14,20,.62);border:1px solid var(--line);border-radius:18px;
    padding:18px 18px 16px;margin-bottom:14px;backdrop-filter:blur(10px);box-shadow:0 14px 40px rgba(0,0,0,.28);
    animation:cardin .4s cubic-bezier(.2,.7,.2,1) both}
  #settingsView .card:nth-child(2){animation-delay:.04s}#settingsView .card:nth-child(3){animation-delay:.08s}
  #settingsView .card:nth-child(4){animation-delay:.12s}#settingsView .card:nth-child(5){animation-delay:.16s}
  #settingsView .card:nth-child(n+6){animation-delay:.2s}
  @keyframes cardin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  #settingsView .card h4{font-size:15px;margin:0 0 10px}
  .paybtn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:13px;margin-top:4px;
    border:none;border-radius:14px;font-weight:800;font-size:15px;color:#09090c;
    background:linear-gradient(90deg,#c9c9d2,#ffffff);box-shadow:0 10px 30px rgba(220,220,235,.22)}
  .paybtn:hover{filter:brightness(1.04);box-shadow:0 14px 40px rgba(220,220,235,.32)}
  .movebtn{display:inline-flex;align-items:center;justify-content:center;gap:9px;width:100%;padding:12px;margin-top:10px;
    border:1px solid rgba(201,201,210,.45);border-radius:14px;font-weight:800;font-size:14.5px;color:#eef0f6;
    background:linear-gradient(180deg,rgba(201,201,210,.16),rgba(201,201,210,.05));cursor:pointer;transition:border-color .15s,background .15s}
  .movebtn:hover{border-color:rgba(201,201,210,.9);background:linear-gradient(180deg,rgba(201,201,210,.26),rgba(201,201,210,.1))}
  .movehint{margin-top:8px;font-size:12.5px;line-height:1.5;color:var(--mute);text-align:center}
  .movehint b{color:#eef0f6;font-weight:700}
  .addbtn.alt{background:linear-gradient(180deg,rgba(201,201,210,.16),rgba(201,201,210,.05));color:#eef0f6;border:1px solid rgba(201,201,210,.4)}
  .addbtn.alt:hover{background:linear-gradient(180deg,rgba(201,201,210,.26),rgba(201,201,210,.1))}
  .whoami{display:inline-flex;align-items:center;gap:8px;margin-top:4px;font-size:13px;color:var(--mute)}
  .whoami img{width:22px;height:22px;border-radius:50%}
  .gaterow button{width:auto!important;margin-top:0!important}
  .srv button{width:auto!important;margin-top:0!important;flex:none}
  /* ---- living background ---- */
  .aura{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}
  .aura i{position:absolute;border-radius:50%;filter:blur(80px);opacity:.38;animation:aurafloat 22s ease-in-out infinite}
  .aura i:nth-child(1){width:52vw;height:52vw;left:-12vw;top:-18vw;background:radial-gradient(circle,#5a5a66,transparent 65%)}
  .aura i:nth-child(2){width:44vw;height:44vw;right:-10vw;top:22vh;background:radial-gradient(circle,#8a8a98,transparent 65%);animation-delay:-7s}
  .aura i:nth-child(3){width:60vw;height:60vw;left:20vw;bottom:-30vw;background:radial-gradient(circle,#2e2e38,transparent 65%);animation-delay:-13s}
  @keyframes aurafloat{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(4vw,3vh) scale(1.08)}}
  @media(prefers-reduced-motion:reduce){.aura i{animation:none}}
  /* ---- account chip ---- */
  .userchip{display:flex;align-items:center;gap:8px;padding:4px 12px 4px 4px;border-radius:999px;border:1px solid var(--line);
    background:rgba(255,255,255,.05);font-size:13px;font-weight:650;max-width:280px}
  .userchip img{width:28px;height:28px;border-radius:50%;flex:none;box-shadow:0 0 0 2px rgba(255,255,255,.14)}
  .userchip #userName{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .opbadge{font-size:9.5px;font-weight:800;letter-spacing:.8px;padding:3px 7px;border-radius:6px;white-space:nowrap;
    background:linear-gradient(90deg,#c9c9d2,#fff);color:#09090c}
  @media(max-width:700px){.userchip #userName,.opbadge{display:none!important}.userchip{padding:4px}
    #headerInvite button{padding:8px 10px;font-size:12px}}
  /* ---- wide gate: server picker + subscribe ---- */
  .gatecard.wide{max-width:520px}
  .gaterow{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:12px}
  .srvlist{display:flex;flex-direction:column;gap:10px;margin:20px 0 6px;text-align:left;max-height:46vh;overflow:auto;padding:2px}
  .srv{display:flex;align-items:center;gap:12px;padding:12px;border-radius:16px;border:1px solid var(--line);
    background:rgba(255,255,255,.04);transition:.18s;flex-wrap:wrap;row-gap:10px}
  .srv:hover{border-color:rgba(255,255,255,.3);background:rgba(255,255,255,.07);transform:translateY(-1px)}
  .srvic{width:44px;height:44px;border-radius:14px;flex:none;overflow:hidden;display:grid;place-items:center;
    background:linear-gradient(135deg,#2a2a33,#15151b);font-weight:800;font-size:18px;color:#e8e8ee}
  .srvic img{width:100%;height:100%;object-fit:cover}
  .srvmeta{flex:1 1 170px;min-width:0}
  .srvmeta b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .srvmeta small{display:block;color:var(--mute);font-size:12.5px;line-height:1.45;margin-top:2px}
  .srvact{display:flex;gap:8px;align-items:center;flex:0 0 auto;margin-left:auto;flex-wrap:wrap;justify-content:flex-end}
  .srvact a{display:inline-flex}
  .srvtag{display:inline-block;font-size:10.5px;font-weight:800;letter-spacing:.4px;padding:2px 7px;border-radius:6px;
    background:rgba(34,197,94,.16);color:#9fe3b4;margin-right:6px}
  .srvtag.off{background:rgba(239,68,68,.14);color:#fca5a5}
  .addbtn{white-space:nowrap;font-weight:700;background:linear-gradient(90deg,#c9c9d2,#fff);color:#09090c;border:none}
  .addbtn:hover{background:linear-gradient(90deg,#dcdce4,#fff)}
  .srvempty{padding:18px;border-radius:14px;border:1px dashed var(--line);color:var(--mute);text-align:center;font-size:14px}
  .plan{margin:20px 0 16px;padding:18px 18px 8px;border-radius:18px;text-align:left;border:1px solid rgba(255,255,255,.16);
    background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.02))}
  .planprice{font-size:40px;font-weight:850;letter-spacing:-1px;line-height:1}
  .planprice small{font-size:15px;font-weight:600;color:var(--mute);letter-spacing:0}
  .plan ul{list-style:none;margin:14px 0 6px;padding:0}
  .plan li{padding:7px 0;border-top:1px solid rgba(255,255,255,.06);font-size:14px;display:flex;gap:10px}
  .plan li span{color:#8ad18a;font-weight:800}
  /* ---- subscription card (Settings) ---- */
  .subcard{border-color:rgba(255,255,255,.2)!important;background:linear-gradient(160deg,rgba(255,255,255,.07),rgba(255,255,255,.015))!important}
  .subhead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
  .spill{font-size:12px;font-weight:800;padding:5px 11px;border-radius:999px;white-space:nowrap;letter-spacing:.3px}
  .spill.ok{background:rgba(34,197,94,.16);color:#9fe3b4;border:1px solid rgba(34,197,94,.35)}
  .spill.bad{background:rgba(239,68,68,.14);color:#f5a3a3;border:1px solid rgba(239,68,68,.35)}
  .subacts{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
  .opctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px;padding-top:12px;border-top:1px dashed rgba(255,255,255,.12)}
  .opctl span{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--mute);margin-right:4px}
  .opctl button{padding:7px 12px;font-size:13px}

/* ===== tactical-blue system (matches the landing) ===== */
@keyframes gridpan{to{background-position:0 46px}}
body::before{display:block!important;background:transparent!important;background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px)!important;background-size:46px 46px!important;inset:0!important;filter:none!important;opacity:1!important;-webkit-mask:radial-gradient(135% 90% at 50% 0,#000 32%,transparent 80%);mask:radial-gradient(135% 90% at 50% 0,#000 32%,transparent 80%);animation:gridpan 60s linear infinite}
body::after{content:""!important;position:fixed!important;inset:0!important;z-index:-1!important;pointer-events:none!important;background:radial-gradient(90% 55% at 50% -6%,rgba(47,107,255,.13),transparent 60%)!important;filter:none!important;opacity:1!important;animation:none!important;transform:none!important}
.aura,.aurora{display:none!important}
body{background:#09090a!important}
h1,h2,h3,.hdr h1,.brandname,.logo-txt,.brand,.gatecard h2{font-family:'Oswald',Inter,sans-serif!important;text-transform:uppercase;letter-spacing:.5px;-webkit-text-fill-color:currentColor;background:none!important;color:var(--text)}
.brand-btn,button.brand-btn{background:linear-gradient(180deg,var(--brand2),var(--brand))!important;border:1px solid var(--brand2)!important;color:#fff!important;border-radius:4px!important;font-family:'JetBrains Mono',ui-monospace,monospace!important;text-transform:uppercase;letter-spacing:.8px;font-size:12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.22),0 10px 26px rgba(47,107,255,.3)!important}
.brand-btn:hover,button.brand-btn:hover{filter:brightness(1.08)}
.tab{border-radius:4px!important;font-family:'JetBrains Mono',ui-monospace,monospace;text-transform:uppercase;letter-spacing:.6px;font-size:12px}
.tab.active{background:var(--brand)!important;color:#fff!important;box-shadow:0 6px 18px rgba(47,107,255,.4)!important}
.card,.gatecard,.stat,.statc{border-radius:4px!important}
.card{border-color:rgba(255,255,255,.08)!important}
.pill,.chip,.badge{border-radius:4px!important}
input,textarea,select{border-radius:4px!important}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--brand)!important;box-shadow:0 0 0 3px rgba(47,107,255,.2)!important}
button.ghost,.ghost{border-radius:4px!important}
button.ghost:hover,.ghost:hover{border-color:var(--brand)!important;color:var(--text)}
.hint,.sub,.mute,.toolbar .count,.kcap{font-family:'JetBrains Mono',ui-monospace,monospace;letter-spacing:.3px}
a.link,.link{color:var(--brand2)}
::selection{background:var(--brand);color:#fff}
::-webkit-scrollbar-thumb{background:rgba(47,107,255,.35)!important}
/* login gate: kill silver gradient + rounded glow frame */
#gate.gate,#gate{background:#09090a!important}
#gate.gate::before,#gate.gate::after,.gatecard::before,.gatecard::after{display:none!important}
.gatecard{border-radius:4px!important;border:1px solid rgba(255,255,255,.1)!important}

</style>
</head>
<body>
<div class="aura"><i></i><i></i><i></i></div>
<header>
  <div class="logo hasimg" id="brandLogo"><img src="/gm-logo.png" alt="Gang Manager"></div>
  <div class="brand">GANG MANAGER<small id="sub">Admin Panel</small></div>
  <div id="userChip" class="userchip" style="display:none"><img id="userAv" alt=""><span id="userName"></span><span id="opBadge" class="opbadge" style="display:none">BOT OWNER</span></div>
  <a id="headerInvite" href="#servers" onclick="openPicker();return false;" style="display:none"><button class="ghost">➕ Add to a server</button></a>
  <a id="logoutBtn" href="/logout"><button class="ghost">Log out</button></a>
</header>

<div id="toasts"></div>

<div class="confirm" id="confirmModal"><div class="confirmbox"><div class="ci2" id="confirmIcon">⚠️</div><h4 id="confirmTitle">Are you sure?</h4><p id="confirmMsg"></p><div class="confirmbtns"><button class="ghost" id="confirmCancel">Cancel</button><button class="brand-btn" id="confirmOk">Confirm</button></div></div></div>

<div id="gate" class="gate" style="display:none">
  <div class="logo hasimg"><img src="/gm-logo.png" alt="Gang Manager"></div>
  <h2 id="gateTitle">Sign in</h2>
  <p id="gateText" class="muted">Log in with Discord to manage the roster.</p>
  <div id="gateActions"></div>
</div>

<div id="app" class="wrap" style="display:none">
  <div id="guildRow" style="margin-bottom:14px;display:none">
    <label class="muted" for="guildSel" style="margin-right:8px">Server</label>
    <select id="guildSel" onchange="reload()"></select>
  </div>

  <div id="stats" class="stats"></div>

  <div id="subBanner"></div>

  <div class="tabs">
    <div class="tab active" data-tab="roster" onclick="setTab('roster')">👥 Roster</div>
    <div class="tab" data-tab="kos" onclick="setTab('kos')">💀 KOS</div>
    <div class="tab" data-tab="strikes" onclick="setTab('strikes')">⚠️ Strikes</div>
    <div class="tab" data-tab="stash" onclick="setTab('stash')">📦 Stash</div>
    <div class="tab" data-tab="wars" onclick="setTab('wars')">⚔️ Wars</div>
    <div class="tab" data-tab="eois" onclick="setTab('eois')">📨 EOIs</div>
    <div class="tab" data-tab="orders" onclick="setTab('orders')">🎫 Orders</div>
    <div class="tab" data-tab="settings" onclick="setTab('settings')">⚙️ Settings</div>
  </div>

  <div class="toolbar">
    <input id="search" type="text" placeholder="Search members…" oninput="render()">
    <button onclick="reload()">↻ Refresh</button>
    <span id="count" class="count"></span>
  </div>

  <div id="rosterView"></div>
  <div id="kosView" style="display:none"></div>
  <div id="strikesView" style="display:none"></div>
  <div id="stashView" style="display:none"></div>
  <div id="warsView" style="display:none"></div>
  <div id="eoisView" style="display:none"></div>
  <div id="ordersView" style="display:none"></div>
  <div id="settingsView" style="display:none"></div>
</div>

<footer>Only the owner and members they add can open this · changes apply to Discord live · made by pmxl</footer>

<script>
let STATE={members:[],ranks:[],kos:[],guild:""},TAB="roster";
const $=id=>document.getElementById(id);
function h(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function show(id,on){$(id).style.display=on?"":"none";}
function toast(text,kind){
  const t=document.createElement("div");t.className="toast "+(kind||"");t.textContent=text;
  $("toasts").appendChild(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";setTimeout(()=>t.remove(),300);},3800);
}
function confirmBox(o){
  o=o||{};
  var m=$("confirmModal");
  if(!m)return Promise.resolve(window.confirm(o.message||o.title||"Are you sure?"));
  $("confirmIcon").textContent=o.icon||"⚠️";
  $("confirmTitle").textContent=o.title||"Are you sure?";
  $("confirmMsg").textContent=o.message||"";
  var ok=$("confirmOk");ok.textContent=o.confirmText||"Confirm";ok.className=o.danger?"danger-solid":"brand-btn";
  var cancel=$("confirmCancel");
  m.classList.add("show");setTimeout(()=>{try{ok.focus();}catch(e){}},30);
  return new Promise(resolve=>{
    function close(v){m.classList.remove("show");ok.onclick=cancel.onclick=m.onclick=null;document.removeEventListener("keydown",onkey);resolve(v);}
    function onkey(e){if(e.key==="Escape")close(false);else if(e.key==="Enter")close(true);}
    ok.onclick=()=>close(true);
    cancel.onclick=()=>close(false);
    m.onclick=e=>{if(e.target===m)close(false);};
    document.addEventListener("keydown",onkey);
  });
}
async function api(path,opts){
  const r=await fetch(path,opts||{});
  let d={};try{d=await r.json();}catch(e){}
  if(!r.ok) throw Object.assign(new Error(d.error||("HTTP "+r.status)),{status:r.status});
  return d;
}
const DSVG='<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" style="flex:none"><path d="M20.3 4.4A19.8 19.8 0 0 0 15.4 3l-.2.5c1.7.4 3.1 1 4.3 1.6a13 13 0 0 0-11 0C9.6 4.4 11 3.9 12.6 3.5L12.4 3a19.8 19.8 0 0 0-5 1.4C4 8.3 3.2 12.1 3.5 15.8a19.9 19.9 0 0 0 6 3l.8-1.1c-.7-.3-1.4-.6-2-1l.5-.4a14.2 14.2 0 0 0 12 0l.5.4c-.6.4-1.3.7-2 1l.8 1.1a19.9 19.9 0 0 0 6-3c.4-4.3-.7-8.1-3.5-11.4ZM9.3 13.8c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Zm5.4 0c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Z"/></svg>';
var GM_LOGO="/gm-logo.png";
var BRAND={name:"Gang Manager",icon:GM_LOGO};
function applyBrand(b){
  // always the GM logo, whatever the server icon is
  BRAND={name:(b&&b.name)||"Gang Manager",icon:GM_LOGO};
  const hl=$("brandLogo");
  if(hl){ hl.innerHTML=`<img src="${GM_LOGO}" alt="">`; hl.classList.add("hasimg"); }
  try{document.title=(BRAND.name||"Gang Manager")+" · Admin";}catch(e){}
}
function gate(icon,title,text,actions,wide){
  show("app",false);show("gate",true);$("logoutBtn").style.display="";var hi=$("headerInvite");if(hi)hi.style.display="none";
  if(window.__recheck){clearInterval(window.__recheck);window.__recheck=null;}
  window.__picker=false;
  const logo=`<img class="glogo" src="${h(BRAND.icon||GM_LOGO)}" alt="Gang Manager">`;
  const badge=(BRAND.icon&&icon)?`<span class="gbadge">${icon}</span>`:"";
  $("gate").innerHTML=`<div class="gatecard${wide?" wide":""}"><div class="gicon">${logo}${badge}</div><h2>${h(title)}</h2><p class="muted">${h(text)}</p><div id="gateActions">${actions||""}</div></div>`;
}
function inviteBtn(){return `<a href="/invite" target="_blank" rel="noopener"><button class="ghost">➕ Add to a server</button></a>`;}
function renderUser(u){
  var c=$("userChip");if(!c)return;
  if(!u||!u.id){c.style.display="none";return;}
  $("userAv").src=u.avatar||"https://cdn.discordapp.com/embed/avatars/0.png";
  $("userName").textContent=u.name||"You";
  $("opBadge").style.display=u.owner?"":"none";
  c.style.display="";
}
function srvIcon(s){return s.icon?`<img src="${h(s.icon)}" alt="">`:`<span>${h((s.name||"?").trim().charAt(0).toUpperCase())}</span>`;}
function showPicker(me,fromApp){
  var list=me.servers||[];
  var price=((STATE.pub||{}).plan||{}).price||"$7.99 / month";
  var rows=list.map(function(s){
    var status="",action="";
    if(s.bot_in&&s.access){status=s.subscribed?`<span class="srvtag">Active here</span>You manage Gang Manager in this server.`:`<span class="srvtag off">Not subscribed</span>The bot's still here, but this server's subscription has lapsed - open to renew.`;action=`<button class="addbtn" onclick="openGuild('${h(s.id)}')">Open</button>`;}
    else if(s.bot_in){status=`<span class="srvtag">Uses Gang Manager</span>Ask the server owner to add you in Settings → Website access.`;}
    else if(s.can_invite){status=`<span class="srvtag">Subscribed</span>Ready - add the bot to switch everything on.`;action=`<a href="/invite?guild=${h(s.id)}" target="_blank" rel="noopener" onclick="waitForBot('${h(s.id)}')"><button class="addbtn">➕ Add bot</button></a>`;}
    else if(s.can_add){
      if(STATE.moveFrom&&STATE.moveFrom.id!==s.id){status=`Move your sub from <b>${h(STATE.moveFrom.name)}</b>, or start a new one`;action=`<button class="addbtn" onclick="moveSub('${h(s.id)}')">🔄 Move sub here</button><a href="/billing/checkout?guild=${h(s.id)}"><button class="addbtn alt">💳 New sub</button></a>`;}
      else{status=`Step 1: subscribe (${h(price)}) · Step 2: add the bot`;action=`<a href="/billing/checkout?guild=${h(s.id)}"><button class="addbtn">💳 Subscribe</button></a>`;}
    }
    else{status="Runs Gang Manager";}
    return `<div class="srv"><div class="srvic">${srvIcon(s)}</div><div class="srvmeta"><b>${h(s.name)}</b><small>${status}</small></div><div class="srvact">${action}</div></div>`;
  }).join("");
  var empty=`<div class="srvempty">No servers to show yet. Create a Discord server (or ask a server owner to add you), then refresh.</div>`;
  var u=me.user||{};
  var who=u.name?`<div class="whoami"><img src="${h(u.avatar||"")}" alt="">Logged in as <b>${h(u.name)}</b></div>`:"";
  var back=fromApp?`<button onclick="boot()">← Back to the panel</button>`:`<button onclick="boot()">↻ Refresh</button>`;
  gate("➕","Your servers","Subscribe for a server, then add Gang Manager to it. One subscription covers one server.",
    `${who}<div class="srvlist">${rows||empty}</div><div class="gaterow">${back}<a href="/logout"><button class="ghost">Log out</button></a></div>`,true);
  window.__picker=true;
}
function openPicker(){
  if(STATE.me&&Array.isArray(STATE.me.servers))showPicker(STATE.me,true);
  else location.href="/login";  // server list expired - log in again to load it
}
function openGuild(id){STATE.prefGuild=id;boot();}
// After "Add bot" opens Discord in a new tab, watch for the bot to join and then
// open that server here. (Pool bots can't redirect back the way the main bot does.)
function waitForBot(gid){
  if(STATE.waitingFor===gid)return;STATE.waitingFor=gid;
  toast("Finish adding the bot in the new tab - this page opens your server once it joins.","ok");
  var tries=0;
  (function poll(){
    if(STATE.waitingFor!==gid)return;
    tries++;
    api("/api/sub_check?guild="+encodeURIComponent(gid)).then(function(d){
      if(STATE.waitingFor!==gid)return;
      if(d&&d.bot_in){STATE.waitingFor=null;toast("Bot added ✓","ok");openGuild(gid);return;}
      if(tries<144)setTimeout(poll,2500);else STATE.waitingFor=null;
    }).catch(function(){if(tries<144)setTimeout(poll,4000);else STATE.waitingFor=null;});
  })();
}
function inviteAndWait(gid){
  var url="/invite?guild="+encodeURIComponent(gid);
  var w=null;try{w=window.open(url,"_blank");}catch(e){}
  if(!w){location.href=url;return;}
  try{w.opener=null;}catch(e){}
  waitForBot(gid);
}
async function moveSub(gid){
  var mf=STATE.moveFrom; if(!mf||mf.id===gid) return;
  if(!await confirmBox({icon:"🔄",title:"Move subscription?",message:mf.name+" will stop using Gang Manager and this server takes over - you can move it back any time.",confirmText:"Move it here"}))return;
  try{
    var r=await post("/api/sub_move",{guild:gid});
    toast(r.moved_from?("Moved from "+r.moved_from+" ✓"):"Subscription moved ✓","ok");
    STATE.moveFrom=null;
    if(r.bot_in){openGuild(gid);} else {inviteAndWait(gid);}
  }catch(e){ if(e.status===401) return boot(); toast(e.message||"Couldn't move the subscription.","err"); }
}
function showSubGate(gid){
  var s=STATE.subs[gid]||{};
  var name=(STATE.guildNames||{})[gid]||"this server";
  var price=((STATE.pub||{}).plan||{}).price||"$7.99 / month";
  var expired=s.status==="expired";
  var multi=Object.keys(STATE.subs||{}).length>1?`<button class="ghost" onclick="boot()">↻ Other server</button>`:"";
  var feats=["Roster, KOS, strikes & wars","Tickets & orders with transcripts","EOI recruitment with voting","This web panel & live logs"].map(function(f){return `<li><span>✓</span>${h(f)}</li>`;}).join("");
  var mf=STATE.moveFrom;
  var move=(mf&&mf.id!==gid)?
    `<button class="movebtn" onclick="moveSub('${h(gid)}')">🔄 Move my subscription here</button>`+
    `<div class="movehint">You're already subscribed on <b>${h(mf.name)}</b>. Move it here for free instead of paying again - that server switches off.</div>`:"";
  gate("🔒",expired?"Subscription expired":"Activate "+name,
    expired?name+"'s subscription has ended. Renew to switch everything back on.":"One subscription unlocks every feature for "+name+".",
    `<div class="plan"><div class="planprice">${h(price.split(" /")[0])}<small> / month</small></div><ul>${feats}</ul></div>`+
    `<a href="/billing/checkout?guild=${encodeURIComponent(gid)}"><button class="paybtn">💳 ${expired?"Renew":"Subscribe"} - ${h(price)}</button></a>`+
    move+
    `<div class="gaterow">${multi}<a href="/logout"><button class="ghost">Log out</button></a></div>`,true);
}
function apiTO(path,ms){
  // like api(), but gives up after `ms` so a stalled request can never spin forever
  var ctrl=("AbortController" in window)?new AbortController():null;
  var timer=ctrl?setTimeout(function(){try{ctrl.abort();}catch(e){}},ms):null;
  return api(path,ctrl?{signal:ctrl.signal}:{}).finally(function(){if(timer)clearTimeout(timer);});
}
var _bootRetryActions="<button onclick='boot()'>↻ Retry</button><a href='/logout'><button class='ghost'>Log out</button></a>";
async function boot(){
  let me;
  try{ me=await apiTO("/api/me",12000); }
  catch(e){
    var why=(e&&e.name==="AbortError")
      ? "The server didn't respond in time - the bot may be restarting."
      : ("Couldn't reach the server"+(e&&e.message?": "+e.message:"")+".");
    gate("⚠️","Can't load the panel",why+" Give it a moment, then retry.",_bootRetryActions);
    return;
  }
  try{
    applyBrand(me.brand);
    STATE.pub=me.public||{};STATE.owner=!!(me.user&&me.user.owner);STATE.me=me;STATE.moveFrom=me.move_from||null;
    STATE.subs={};STATE.canSetup={};STATE.guildNames={};
    (me.guilds||[]).forEach(x=>{STATE.subs[x.id]=x.subscription||null;STATE.canSetup[x.id]=!!x.can_setup;STATE.guildNames[x.id]=x.name;});
    renderUser(me.user);
    if(!me.authed){
      var pricing=`<a href="/#pricing"><button class="ghost">💳 Pricing</button></a>`;
      if(me.reason==="not_configured") gate("⚙️","Setup needed","The site owner still needs to finish setting up Discord login.",pricing);
      else if(me.reason==="no_access"){
        if(Array.isArray(me.servers)) showPicker(me);
        else gate("➕","Add your server","Log in again to load your servers, or add Gang Manager to a server you own.",
          `<a href="/login"><button class="discordbtn">${DSVG} Load my servers</button></a><div class="gaterow"><button class="ghost" onclick="boot()">↻ Refresh</button><a href="/logout"><button class="ghost">Log out</button></a></div>`);
      }
      else gate("","Gang Manager","Log in with Discord to set up and run your gang from the browser.",`<a href="/login"><button class="discordbtn">${DSVG} Log in with Discord</button></a>`+pricing);
      return;
    }
    // pick an active server by default; fall back to the first
    var wantPicker=location.hash==="#servers";
    if(wantPicker){try{history.replaceState(null,"",location.pathname);}catch(e){}}
    if(wantPicker&&Array.isArray(me.servers)){showPicker(me,true);return;}
    // just came back from adding the bot (/callback -> /panel?added=<id>): open that server
    var addedG="";try{addedG=new URLSearchParams(location.search).get("added")||"";}catch(e){}
    if(addedG){try{history.replaceState(null,"",location.pathname);}catch(e){}STATE.prefGuild=addedG;}
    var active=(me.guilds||[]).filter(x=>!(x.subscription&&x.subscription.billing_enabled)||x.subscription.active);
    var pref=STATE.prefGuild&&(me.guilds||[]).find(x=>x.id===STATE.prefGuild);STATE.prefGuild=null;
    var def=((pref||active[0]||me.guilds[0])||{}).id||"";
    var defSub=STATE.subs[def];
    // the bot owner skips the subscribe screen so they can comp servers in Settings
    if(defSub&&defSub.billing_enabled&&!defSub.active&&!STATE.owner){ STATE.guild=def; showSubGate(def); return; }
    show("gate",false);show("app",true);$("logoutBtn").style.display="";
    var hib=$("headerInvite");if(hib)hib.style.display="";
    if(window.__recheck)clearInterval(window.__recheck);
    window.__recheck=setInterval(checkAccess,8000);
    const sel=$("guildSel");
    sel.innerHTML=me.guilds.map(x=>`<option value="${h(x.id)}">${h(x.name)}</option>`).join("");
    show("guildRow",me.guilds.length>1);
    STATE.guild=def;
    if(sel)sel.value=def;
    await reload();
  }catch(err){
    gate("⚠️","Panel error","The panel hit a snag while loading"+(err&&err.message?": "+err.message:"")+". Retry, or log out and back in.",_bootRetryActions);
  }
}
function renderSubBanner(){
  var el=$("subBanner");if(!el)return;
  var s=STATE.subs&&STATE.subs[STATE.guild];el.innerHTML="";
  if(!s||!s.billing_enabled)return;
  var g=encodeURIComponent(STATE.guild);
  if(!s.active){
    el.innerHTML=`<div class="banner bad">🔒 <b>Subscription ${s.status==="expired"?"expired":"inactive"}.</b> The bot's features are paused in Discord. <a href="/billing/checkout?guild=${g}">Subscribe →</a></div>`;
  }else if(s.mode==="paid"&&s.days_left!=null&&s.days_left<=5){
    var link=s.has_billing?`/billing/portal?guild=${g}`:`/billing/checkout?guild=${g}`;
    el.innerHTML=`<div class="banner warn">⏳ Paid up for ${s.days_left} more day(s)${s.has_billing?" - renews automatically.":"."} <a href="${link}">${s.has_billing?"Manage billing →":"Subscribe →"}</a></div>`;
  }
}
async function checkAccess(){
  try{
    const r=await api("/api/access?guild="+encodeURIComponent(STATE.guild||""));
    if(!r.ok) gate("🚫","Access removed","Your website access was removed, so you've been logged out.",'<a href="/logout"><button>Log out</button></a>');
  }catch(e){ if(e.status===401||e.status===403) boot(); }
}
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&window.__recheck)checkAccess();});
window.addEventListener("focus",()=>{if(window.__picker)boot();else if(window.__recheck)checkAccess();});
async function reload(){
  const sel=$("guildSel");if(sel&&sel.value)STATE.guild=sel.value;
  const g=encodeURIComponent(STATE.guild);
  try{
    const [ov,ro,ko,stn,wa,eo,sk,od]=await Promise.all([api("/api/overview?guild="+g),api("/api/roster?guild="+g),api("/api/kos?guild="+g),api("/api/stash?guild="+g),api("/api/wars?guild="+g),api("/api/eois?guild="+g),api("/api/strikes?guild="+g),api("/api/orders?guild="+g)]);
    $("sub").textContent=ro.guild?.name||"Roster";
    renderStats(ov.overview);
    STATE.members=ro.members||[];STATE.ranks=ro.ranks||[];STATE.kos=ko.members||[];STATE.rank_order=ro.rank_order||[];STATE.stash=stn.items||[];STATE.stashTotal=stn.total||0;STATE.wars=wa.wars||[];STATE.eois=eo.eois||[];STATE.strikes=sk.members||[];STATE.strikeLimit=sk.limit||3;STATE.orders=od.orders||[];STATE.ticketTypes=od.types||[];STATE.ticketsOpen=od.open!==false;
    STATE.settings=null;
    renderSubBanner();
    var cs=STATE.canSetup&&STATE.canSetup[STATE.guild];
    var stab=document.querySelector('.tab[data-tab="settings"]');if(stab)stab.style.display=cs?"":"none";
    if(!cs&&TAB==="settings")setTab("roster");
    render();
  }catch(e){ if(e.status===401||e.status===403) return boot(); if(e.status===402) return showSubGate(STATE.guild); toast(e.message||"Failed to load.","err"); }
}
function countUp(el,to){
  to=Number(to); if(isNaN(to)){el.textContent=arguments[1];return;}
  const dur=600,t0=performance.now();
  (function step(t){const p=Math.min((t-t0)/dur,1);el.textContent=Math.round(to*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(step);})(t0);
}
function renderStats(o){
  const tiles=[["ranked","Ranked","🎯","brand"],["kos","On KOS","💀","bad"],["open_eois","Pending EOIs","📨","warn"],
    ["strikes_30d","Strikes 30d","⚠️","warn"],["wars","Wars","⚔️","blue"],["members","In server","👥","ok"]];
  $("stats").innerHTML=tiles.map(([k,l,ic,c],i)=>`<div class="stat ${c}" style="animation-delay:${i*45}ms"><span class="ic">${ic}</span><div class="n" data-c="${h(String(o[k]??'-'))}">0</div><div class="l">${l}</div></div>`).join("")
    +`<div class="stat purple" style="animation-delay:270ms"><span class="ic">📻</span><div class="n" style="font-size:17px">${h(o.radio||'-')}</div><div class="l">Radio</div></div>`;
  document.querySelectorAll('.stat .n[data-c]').forEach(e=>countUp(e,e.dataset.c));
}
function setTab(t){
  TAB=t;document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.dataset.tab===t));
  ["roster","kos","strikes","stash","wars","eois","orders","settings"].forEach(v=>show(v+"View",t===v));
  var tb=document.querySelector(".toolbar");if(tb)tb.style.display=(t==="settings")?"none":"";
  $("search").placeholder={roster:"Search members…",kos:"Search KOS…",strikes:"Search struck members…",stash:"Search items…",wars:"Search wars…",eois:"Pending applications",orders:"Search orders…"}[t]||"Search…";render();
}
function render(){({roster:renderRoster,kos:renderKos,strikes:renderStrikes,stash:renderStash,wars:renderWars,eois:renderEois,orders:renderOrders,settings:renderSettings}[TAB]||renderRoster)();}
function renderRoster(){
  const q=$("search").value.toLowerCase();
  let ms=STATE.members.filter(m=>m.name.toLowerCase().includes(q)||(m.rank||"").toLowerCase().includes(q));
  $("count").textContent=STATE.members.length+" member"+(STATE.members.length===1?"":"s");
  if(!ms.length){$("rosterView").innerHTML='<div class="empty">No members match.</div>';return;}
  const groups={};ms.forEach(m=>{const k=m.rank||"Unranked";(groups[k]=groups[k]||[]).push(m);});
  const order=STATE.rank_order||[];
  const ri=n=>{const i=order.indexOf((n||"").toLowerCase());return i<0?order.length:i;};
  const names=Object.keys(groups).sort((a,b)=>{
    if(a==="Unranked")return 1;if(b==="Unranked")return -1;
    const d=ri(a)-ri(b);if(d)return d;
    return groups[b].length-groups[a].length||a.localeCompare(b);
  });
  $("rosterView").innerHTML=names.map(rk=>`<div class="ranklabel">${h(rk)}<span class="pill">${groups[rk].length}</span></div><ul class="members">${groups[rk].map(memberRow).join("")}</ul>`).join("");
}
function memberRow(m){
  const chips=(m.roles||[]).map(r=>{
    const cls="chip "+(STATE.ranks.some(x=>x.name===r.name)?"rankrole":"")+(r.manageable?"":" locked");
    const x=r.manageable?`<span class="x" title="Remove role" onclick="removeRole('${h(m.id)}','${h(r.id)}','${h(r.name)}')">✕</span>`:`<span class="x" style="opacity:.6;width:11px" title="Above the bot">🔒</span>`;
    return `<span class="${cls}">${h(r.name)}${x}</span>`;
  }).join("");
  const have=new Set((m.roles||[]).map(r=>r.name));
  const addable=STATE.ranks.filter(x=>!have.has(x.name));
  const addsel=addable.length?`<select class="addsel" onchange="addRole('${h(m.id)}',this.value,this);this.value=''"><option value="">+ role</option>${addable.map(x=>`<option value="${h(x.id)}">${h(x.name)}</option>`).join("")}</select>`:"";
  const s=(STATE.strikes||[]).find(x=>x.id===m.id);
  const flag=s?` <span class="sflag${s.over?' over':''}" title="${s.count}/${s.limit} strikes - see the Strikes tab">⚠️ ${s.count}/${s.limit}</span>`:"";
  return `<li class="member" data-id="${h(m.id)}"><div class="av"><img src="${h(m.avatar)}" alt="" loading="lazy"></div>
    <div class="grow"><div class="mname">${h(m.name)}${m.username?` <span class="muted" style="font-weight:400;font-size:12px">@${h(m.username)}</span>`:""}${flag}</div><div class="chips">${chips||'<span class="chip locked">no gang roles</span>'}${addsel}</div></div>
    <button class="danger" onclick="removeMember('${h(m.id)}','${h(m.name)}')">Remove all</button></li>`;
}
function ago(ts){if(!ts)return"";const s=Math.floor(Date.now()/1000)-Number(ts);if(s<0)return"just now";
  if(s<3600)return Math.max(Math.round(s/60),1)+"m ago";if(s<86400)return Math.round(s/3600)+"h ago";
  if(s<2592000)return Math.round(s/86400)+"d ago";return Math.round(s/2592000)+"mo ago";}
function renderKos(){
  const q=$("search").value.toLowerCase();
  const ks=STATE.kos.filter(k=>k.name.toLowerCase().includes(q));
  $("count").textContent=STATE.kos.length+" on KOS";
  const pick=(STATE.members&&STATE.members.length)
    ? '<select id="kosPick" class="i" onchange="if(this.value){$(\'kosId\').value=this.value}">'
      +'<option value="">- or pick a current member -</option>'
      +STATE.members.map(m=>`<option value="${h(m.id)}">${h(m.name)}${m.rank?" · "+h(m.rank):""}</option>`).join("")
      +'</select>'
    : '';
  const add='<div class="card"><h4 style="margin:0 0 10px">💀 Add to KOS</h4>'
    +'<div class="addbar"><input id="kosId" class="i" placeholder="Discord user ID (right-click user → Copy ID)" inputmode="numeric">'
    +pick
    +'<input id="kosReason" class="i" placeholder="Reason (optional)">'
    +'<button onclick="addKos()">➕ Add to KOS</button></div>'
    +'<div class="muted" style="font-size:12px;margin-top:6px">Paste a Discord ID, or pick a current member from the list - they get the KOS role automatically if one is set in Settings.</div></div>';
  if(!ks.length){$("kosView").innerHTML=add+'<div class="empty">Nobody on the KOS list yet. 🕊️</div>';return;}
  $("kosView").innerHTML=add+`<ul class="members">${ks.map(k=>{
    const meta=[k.added_by?"added by "+h(k.added_by):"",k.at?h(k.at):""].filter(Boolean).join(" · ");
    return `<li class="member" data-id="${h(k.id)}">
    ${k.avatar?`<div class="av"><img src="${h(k.avatar)}" alt="" loading="lazy"></div>`:'<div class="av skull">💀</div>'}
    <div class="grow"><div class="mname">${h(k.name)}</div>${k.reason?`<div class="reason">${h(k.reason)}</div>`:'<div class="reason" style="opacity:.6">no reason given</div>'}${meta?`<div class="muted" style="font-size:11px;margin-top:3px">${meta}</div>`:""}</div>
    <button class="danger" onclick="removeKos('${h(k.id)}','${h(k.name)}')">Take off KOS</button></li>`;}).join("")}</ul>`;
}
function renderStrikes(){
  const q=$("search").value.toLowerCase();
  const ms=(STATE.strikes||[]).filter(m=>m.name.toLowerCase().includes(q));
  const total=(STATE.strikes||[]).reduce((a,m)=>a+m.count,0);
  $("count").textContent=(STATE.strikes||[]).length+" struck · "+total+" strike"+(total===1?"":"s");
  if(!ms.length){$("strikesView").innerHTML='<div class="empty">Nobody has strikes. ✨</div>';return;}
  $("strikesView").innerHTML=`<ul class="members" style="gap:10px">`+ms.map(m=>{
    const lim=m.limit||STATE.strikeLimit||3;
    const bar=Array.from({length:Math.max(lim,m.count)},(_,i)=>`<i class="${i<m.count?'on':''}"></i>`).join("");
    const col=m.over?"var(--bad)":"var(--warn)";
    const log=(m.strikes||[]).map(s=>`<div class="row"><b>${h(s.reason||"-")}</b><span>${s.by?"by "+h(s.by):""}</span><span>${s.ts?ago(s.ts):""}</span>${s.id!=null?`<span class="sid">#${h(String(s.id))}</span><span class="srm" title="Remove this strike" onclick="removeStrike('${h(String(s.id))}','${h(m.name)}')">✕</span>`:""}</div>`).join("");
    return `<li class="member" style="align-items:flex-start">
      ${m.avatar?`<div class="av"><img src="${h(m.avatar)}" alt="" loading="lazy"></div>`:'<div class="av skull" style="background:linear-gradient(135deg,#5a3a0f,#5c3b12)">⚠️</div>'}
      <div class="grow"><div class="mname">${h(m.name)}${m.over?` <span class="sflag over" title="at or over the limit">limit reached</span>`:""}</div>
        <div class="sbar">${bar}</div>
        <div class="slog">${log}</div></div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
        <div class="scount" style="color:${col}">${m.count}<span style="font-size:13px;color:var(--mute)">/${lim}</span></div>
        <button class="danger" onclick="clearStrikes('${h(m.id)}','${h(m.name)}')">Clear all</button></div></li>`;
  }).join("")+`</ul>`;
}
async function removeStrike(id,name){
  if(!await confirmBox({icon:"⚠️",title:"Remove strike?",message:"Remove strike #"+id+" from "+name+".",confirmText:"Remove",danger:true}))return;
  try{await post("/api/strike_remove",{guild:STATE.guild,strike_id:id});toast("Removed strike #"+id,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function clearStrikes(uid,name){
  if(!await confirmBox({icon:"🧹",title:"Clear all strikes?",message:"Remove every strike from "+name+". This can't be undone.",confirmText:"Clear all",danger:true}))return;
  try{const r=await post("/api/strike_clear",{guild:STATE.guild,user_id:uid});toast("Cleared "+(r.cleared||0)+" strike"+((r.cleared||0)===1?"":"s")+" from "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function post(path,body){return api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});}
async function removeRole(uid,rid,name){
  try{await post("/api/role",{guild:STATE.guild,user_id:uid,role_id:rid,action:"remove"});toast("Removed role "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function addRole(uid,rid,sel){
  if(!rid)return;const name=sel.options[sel.selectedIndex].text;
  try{await post("/api/role",{guild:STATE.guild,user_id:uid,role_id:rid,action:"add"});toast("Added "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function removeMember(uid,name){
  if(!await confirmBox({icon:"🚪",title:"Remove from roster?",message:"Remove "+name+" from the roster and take ALL their gang roles off in Discord?",confirmText:"Remove",danger:true}))return;
  try{const r=await post("/api/remove",{guild:STATE.guild,user_id:uid});
    const f=(r.failed_roles||[]).length?" · couldn't remove: "+r.failed_roles.join(", "):"";
    toast("Removed "+name+(r.removed_roles?.length?" ("+r.removed_roles.join(", ")+")":"")+f,f?"err":"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function addKos(){
  const id=(($("kosId")||{}).value||"").trim();
  if(!/^[0-9]{5,25}$/.test(id))return toast("Enter a valid Discord user ID","err");
  const reason=(($("kosReason")||{}).value||"").trim();
  try{const r=await post("/api/kos_add",{guild:STATE.guild,user_id:id,reason:reason});
    STATE.kos=r.members;renderKos();
    toast((r.name||"Member")+" added to KOS"+(r.role_missing?" (no KOS role set in Settings)":""),"ok");}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Couldn't add to KOS","err");}
}
async function removeKos(uid,name){
  if(!await confirmBox({icon:"🎯",title:"Remove from KOS?",message:"Take "+name+" off the KOS list and remove the KOS role?",confirmText:"Remove",danger:true}))return;
  try{await post("/api/kos_remove",{guild:STATE.guild,user_id:uid});toast("Took "+name+" off KOS","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}

const STASH_CATS=[
  ["weapons","🔫","Weapons",["pistol","rifle","smg","deagle","draco","switch","glock","shotgun","revolver","sniper","knife","weapon","firearm","uzi","ak47","ak-47","mac10","mac-11","mp5","scar","carbine","machete","gun"]],
  ["ammo","🔸","Ammo",["ammo","round","magazine"," mag","clip","bullet"]],
  ["armor","🛡️","Armor",["armor","armour","vest","plate","kevlar"]],
  ["drugs","🧪","Drugs",["weed","coke","cocaine","meth","crack","lean","xanax","acid","shroom","heroin","oxy","ketamine","mdma","molly","joint","blunt","baggie","drug"]],
  ["cash","💵","Cash",["cash","money","dollar","bank","bond"]],
  ["materials","⚙️","Materials",["metal","scrap","chem","acetone","powder","material","supply","fuel","lockpick","tool","plastic","glass","rubber","aluminium","aluminum","copper","steel","iron","part","agent","packaging"]],
  ["other","📦","Other",[]]
];
function stashCat(name){const n=" "+String(name||"").toLowerCase()+" ";for(const c of STASH_CATS){if(c[3].some(k=>n.includes(k)))return c[0];}return "other";}
function stashCatMeta(key){return STASH_CATS.find(c=>c[0]===key)||STASH_CATS[STASH_CATS.length-1];}
function stashSort(s){if(s==="name")return (a,b)=>a.item.localeCompare(b.item);if(s==="weight")return (a,b)=>((b.qty||0)*(b.weight||0))-((a.qty||0)*(a.weight||0));return (a,b)=>(b.qty||0)-(a.qty||0);}
function renderStash(){
  const q=$("search").value.toLowerCase();
  const all=STATE.stash||[];
  const items=all.filter(i=>i.item.toLowerCase().includes(q));
  const totQ=all.reduce((a,i)=>a+(i.qty||0),0);
  const totW=all.reduce((a,i)=>a+(i.qty||0)*(i.weight||0),0);
  const catSet=new Set(all.map(i=>stashCat(i.item)));
  $("count").textContent=all.length+" item"+(all.length===1?"":"s")+" · "+totQ+" total"+(totW>0?" · "+(Math.round(totW*10)/10)+" kg":"");
  const tiles=[["📦",all.length,"Items"],["🔢",totQ,"Units"],["⚖",Math.round(totW*10)/10,"Kg total"],["🗂",catSet.size,"Categories"]];
  const stats=`<div class="stats" style="margin:0 0 14px">`+tiles.map((t,i)=>`<div class="stat" style="animation-delay:${i*45}ms"><span class="ic">${t[0]}</span><div class="n">${t[1]}</div><div class="l">${t[2]}</div></div>`).join("")+`</div>`;
  const add=`<div class="addbar"><input class="i" id="si" placeholder="Item name" onkeydown="if(event.key==='Enter')stashAdd()"><input class="q" id="sq" type="number" placeholder="Qty" value="1"><input class="q" id="sw" type="number" step="0.1" placeholder="kg ea"><button class="brand-btn" onclick="stashAdd()">Add</button></div>`;
  const drop=`<div class="drop" id="drop" onclick="$('file').click()"><div>📸 <b>Drop a stash screenshot</b> or click to upload</div><small class="muted">Reads item names + counts with OCR (weight bar ignored) - you confirm before anything is added</small><input id="file" type="file" accept="image/*" hidden></div><div id="cands" class="card" style="display:none;padding:14px;margin-bottom:14px"></div>`;
  let body="";
  if(!items.length){
    body=`<div class="empty">`+(all.length?`No items match “${h($("search").value)}”.`:`Stash is empty - add an item above, or drop a screenshot to scan it in.`)+`</div>`;
  }else{
    const sort=window.__ssort||"qty";
    const groups={};items.forEach(i=>{const k=stashCat(i.item);(groups[k]=groups[k]||[]).push(i);});
    body=`<div class="stsort">Sort <select onchange="window.__ssort=this.value;renderStash()"><option value="qty"${sort==='qty'?' selected':''}>Most units</option><option value="name"${sort==='name'?' selected':''}>Name A-Z</option><option value="weight"${sort==='weight'?' selected':''}>Heaviest</option></select></div>`;
    for(const c of STASH_CATS){const g=groups[c[0]];if(!g||!g.length)continue;g.sort(stashSort(sort));
      const subQ=g.reduce((a,i)=>a+(i.qty||0),0);
      body+=`<div class="scat"><span class="ic">${c[1]}</span>${c[2]}<span class="pill">${g.length} item${g.length===1?"":"s"} · ${subQ} units</span></div><ul class="members" style="gap:8px;margin-bottom:6px">`+g.map(stashRow).join("")+`</ul>`;
    }
  }
  $("stashView").innerHTML=stats+add+drop+body;wireDrop();
}
function stashRow(i){
  const cm=stashCatMeta(stashCat(i.item));
  const tot=i.weight>0?` · <b>${Math.round(i.qty*i.weight*10)/10}</b> kg`:"";
  return `<li class="sitem">${i.image?`<img class="iconimg" src="${i.image}" alt="">`:`<div class="iconbox">${i.icon||cm[1]}</div>`}<div class="grow"><div class="mname">${h(i.item)}</div>
    <div class="chips"><span class="stcat-tag">${cm[1]} ${cm[2]}</span><span class="wchip">⚖ <input class="wi" type="number" step="0.1" value="${i.weight||0}" title="Weight each (kg)" onchange="stashWeight('${h(i.item)}',this.value)"> kg ea${tot}</span></div></div>
    <div class="q"><button onclick="stashDelta('${h(i.item)}',-1)">−</button><span class="qn">${i.qty}</span><button onclick="stashDelta('${h(i.item)}',1)">+</button></div>
    <button class="danger" onclick="stashRemove('${h(i.item)}')">✕</button></li>`;
}
async function stashWeight(item,w){try{await post("/api/stash",{guild:STATE.guild,item,qty:0,mode:"add",weight:w});await reload();}catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
function wireDrop(){const d=$("drop"),f=$("file");if(!d)return;f.onchange=()=>{if(f.files[0])scanFile(f.files[0]);};d.ondragover=e=>{e.preventDefault();d.classList.add("over");};d.ondragleave=()=>d.classList.remove("over");d.ondrop=e=>{e.preventDefault();d.classList.remove("over");if(e.dataTransfer.files[0])scanFile(e.dataTransfer.files[0]);};}
async function scanFile(file){toast("Scanning screenshot…");const fd=new FormData();fd.append("image",file);
  try{const r=await fetch("/api/stash_scan?guild="+encodeURIComponent(STATE.guild),{method:"POST",body:fd});const d=await r.json();
    if(!r.ok)throw new Error(d.error||"Scan failed");
    if(!d.candidates||!d.candidates.length){toast(d.note||"Couldn't read any items from that image.","err");return;}
    renderCandidates(d.candidates);}catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Scan failed","err");}}
function renderCandidates(c){const el=$("cands");el.style.display="";window.__cimg=c.map(x=>x.image||"");
  el.innerHTML=`<div style="font-weight:700;margin-bottom:10px">Found ${c.length} - tick what to add, fix names/qty/weight, then confirm</div>`
    +c.map((x,i)=>`<div class="cand"><input type="checkbox" checked id="c${i}">${x.image?`<img class="thumb" src="${x.image}">`:`<div class="temoji">${x.icon||"📦"}</div>`}<input class="ci" id="ci${i}" value="${h(x.item)}" title="Item"><input class="cq" id="cq${i}" type="number" value="${x.qty}" title="Qty"><input class="cq" id="cw${i}" type="number" step="0.1" placeholder="kg" title="Weight each"></div>`).join("")
    +`<div style="margin-top:12px;display:flex;gap:8px"><button class="brand-btn" onclick="addCandidates(${c.length})">Add to stash</button><button onclick="$('cands').style.display='none'">Cancel</button></div>`;}
async function addCandidates(n){let added=0;
  for(let i=0;i<n;i++){const cb=$("c"+i);if(!cb||!cb.checked)continue;const item=$("ci"+i).value.trim(),qty=$("cq"+i).value;if(!item)continue;
    const weight=($("cw"+i)||{}).value||"";const image=(window.__cimg||[])[i]||"";
    try{await post("/api/stash",{guild:STATE.guild,item,qty,weight,image,mode:"add"});added++;}catch(e){}}
  const el=$("cands");if(el)el.style.display="none";toast("Added "+added+" item"+(added===1?"":"s")+" to the stash","ok");await reload();}
async function stashAdd(){const item=($("si")||{}).value?.trim(),qty=($("sq")||{}).value||1,weight=($("sw")||{}).value||"";
  if(!item){toast("Enter an item name","err");return;}
  try{await post("/api/stash",{guild:STATE.guild,item,qty,weight,mode:"add"});toast("Added "+item,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
async function stashDelta(item,d){try{await post("/api/stash",{guild:STATE.guild,item,qty:d,mode:"add"});await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
async function stashRemove(item){try{await post("/api/stash_remove",{guild:STATE.guild,item});toast("Removed "+item,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}}
function renderWars(){
  const q=$("search").value.toLowerCase();
  const all=STATE.wars||[];
  const ws=all.filter(w=>(w.enemy||"").toLowerCase().includes(q));
  const liveN=all.filter(w=>w.active).length;
  $("count").textContent=all.length+" war"+(all.length===1?"":"s")+(liveN?" · "+liveN+" live":"");
  const start=`<div class="card"><h4 style="margin:0 0 10px">⚔️ Start a war</h4>`
    +`<div class="addbar"><input id="warEnemy" class="i" placeholder="Enemy gang or crew" maxlength="60" onkeydown="if(event.key==='Enter')startWar()">`
    +`<input id="warFirstTo" class="q" type="number" min="1" max="100" placeholder="First to">`
    +`<button class="brand-btn" onclick="startWar()">⚔️ Start</button></div>`
    +`<div class="muted" style="font-size:12px;margin-top:6px">Posts a live scoreboard to your KOS log channel (set one in Settings). Log rounds here or from the scoreboard - they stay in sync.</div></div>`;
  if(!ws.length){$("warsView").innerHTML=start+`<div class="empty">${q?"No wars match.":"No wars yet - start one above. ⚔️"}</div>`;return;}
  $("warsView").innerHTML=start+`<ul class="members" style="gap:10px">`+ws.map(w=>{
    const col=w.wins>w.losses?"var(--ok)":w.losses>w.wins?"var(--bad)":"var(--mute)";
    const rounds=(w.recent||[]).map(r=>`<i class="${r?'w':'l'}"></i>`).join("");
    const meta=[
      w.start?"started "+ago(w.start):"",
      (w.rounds||0)+" round"+(w.rounds===1?"":"s"),
      (w.rounds>0?w.winrate+"% win rate":""),
      w.first_to?`first to ${w.first_to} (${Math.max(w.wins,w.losses)}/${w.first_to})`:"",
      w.last_ts?`last round ${ago(w.last_ts)}${w.last_by?" by "+h(w.last_by):""}`:"",
    ].filter(Boolean).map(x=>`<span>${x}</span>`).join("");
    const ctrls=w.active?`<div class="warbtns">
        <button class="wb win" onclick="warRound('${h(w.enemy)}','win')">🏆 Won</button>
        <button class="wb loss" onclick="warRound('${h(w.enemy)}','loss')">💀 Lost</button>
        <button class="wb undo" onclick="warRound('${h(w.enemy)}','undo')">↩ Undo</button>
        <button class="wb end" onclick="endWar('${h(w.enemy)}')">🏳 End</button></div>`:"";
    return `<li class="warcard" style="align-items:flex-start;flex-wrap:wrap">
      <div class="grow"><div class="mname">vs ${h(w.enemy)}</div>
        <div class="wmeta">${meta}</div>
        ${w.rounds>0?`<div class="wrate"><span style="width:${w.winrate}%"></span></div>`:""}
        ${rounds?`<div class="wrounds" title="recent rounds (oldest → newest)">${rounds}</div>`:""}
        ${ctrls}</div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:7px">
        <span class="badge ${w.active?'live':'done'}">${w.active?'Live':'Done'}</span>
        <div class="score" style="color:${col}">${w.wins} - ${w.losses}</div></div></li>`;}).join("")+`</ul>`;
}
function renderEois(){const es=STATE.eois||[];$("count").textContent=es.length+" pending";
  if(!es.length){$("eoisView").innerHTML=`<div class="empty">No pending applications. 🎉</div>`;return;}
  $("eoisView").innerHTML=`<ul class="members" style="gap:9px">`+es.map(e=>`<li class="member">${e.avatar?`<div class="av"><img src="${h(e.avatar)}" alt=""></div>`:`<div class="av skull" style="background:linear-gradient(135deg,#1a2033,#243154)">📨</div>`}<div class="grow"><div class="mname">${h(e.ign||e.applicant||"Applicant")}</div><div class="muted" style="font-size:12px">${h(e.role||"-")}${e.applicant&&e.applicant!==e.ign?" · "+h(e.applicant):""}</div></div><div style="display:flex;gap:7px;align-items:center">${e.ends?`<span class="badge done" data-ts="${e.ends}"></span>`:""}<span class="badge" style="background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.4)">✅ ${e.yes}</span><span class="badge" style="background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.4)">❌ ${e.no}</span><button style="padding:6px 11px;background:linear-gradient(135deg,#16a34a,#15803d);border:none;color:#fff;font-weight:700;border-radius:11px;cursor:pointer" onclick="acceptEoi('${h(e.id)}','${h(e.ign||e.applicant||"this")}')">Accept</button><button class="danger" style="padding:6px 11px" onclick="closeEoi('${h(e.id)}','${h(e.ign||e.applicant||"this")}')">Close</button></div></li>`).join("")+`</ul>`;
  document.querySelectorAll('#eoisView [data-ts]').forEach(el=>{const t=+el.dataset.ts,sec=t-Math.floor(Date.now()/1000);el.textContent=sec<=0?"closing":"closes "+(sec>86400?Math.round(sec/86400)+"d":sec>3600?Math.round(sec/3600)+"h":Math.max(Math.round(sec/60),1)+"m");});
}
async function cancelWar(enemy){
  if(!await confirmBox({icon:"🏳️",title:"Cancel war?",message:"The war against "+enemy+" stops immediately and is marked done.",confirmText:"Cancel war",danger:true}))return;
  try{const r=await post("/api/war_cancel",{guild:STATE.guild,enemy:enemy});toast("War cancelled","ok");STATE.wars=r.wars;render();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function startWar(){
  const enemy=(($("warEnemy")||{}).value||"").trim();
  if(!enemy)return toast("Enter who you're at war with","err");
  const ft=(($("warFirstTo")||{}).value||"").trim();
  try{const r=await post("/api/war_start",{guild:STATE.guild,enemy:enemy,first_to:ft||null});
    STATE.wars=r.wars;renderWars();
    toast("War started vs "+enemy+(r.posted?" · scoreboard posted":" · set a KOS log channel in Settings for a live scoreboard"),"ok");}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Couldn't start the war","err");}
}
async function warRound(enemy,result){
  try{const r=await post("/api/war_round",{guild:STATE.guild,enemy:enemy,result:result});STATE.wars=r.wars;renderWars();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function endWar(enemy){
  if(!await confirmBox({icon:"🏳️",title:"End war?",message:"End the war against "+enemy+" and lock in the final score.",confirmText:"End war"}))return;
  try{const r=await post("/api/war_end",{guild:STATE.guild,enemy:enemy});toast("War ended","ok");STATE.wars=r.wars;render();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function closeEoi(id,name){
  if(!await confirmBox({icon:"🗳️",title:"Close voting?",message:"Close voting for "+name+"'s application now? Votes are tallied and the post locks.",confirmText:"Close voting"}))return;
  try{await post("/api/eoi_close",{guild:STATE.guild,msg_id:id});toast("Closed "+name+"'s EOI","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function acceptEoi(id,name){
  if(!await confirmBox({icon:"✅",title:"Force-accept application?",message:"Force-accept "+name+"? They'll be welcomed and given the member role regardless of the votes.",confirmText:"Accept"}))return;
  try{await post("/api/eoi_accept",{guild:STATE.guild,msg_id:id});toast("Accepted "+name,"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
function orderPrioPill(p){const m={urgent:["🔴","Urgent"],high:["🟠","High"],normal:["🔵","Normal"],low:["🟢","Low"]};const x=m[p]||m.normal;return `<span class="opill ${h(p)}">${x[0]} ${x[1]}</span>`;}
function renderOrders(){
  const q=$("search").value.toLowerCase();
  const all=STATE.orders||[];
  const os=all.filter(o=>((o.customer||"")+" "+(o.type||"")+" "+(o.guns||"")+" #"+o.id).toLowerCase().includes(q));
  const openN=all.filter(o=>o.status==="open").length, paidN=all.filter(o=>o.status==="open"&&o.paid).length;
  $("count").textContent=openN+" open · "+paidN+" paid";
  const types=STATE.ticketTypes||[];
  const typeOpts=types.length?types.map(t=>`<option value="${h(t.name)}">${h(t.emoji+" "+t.name)}</option>`).join(""):'<option value="">No ticket types - add one with /ticket type_add</option>';
  const custOpts='<option value="">- no customer -</option>'+(STATE.members||[]).map(m=>`<option value="${h(m.id)}">${h(m.name)}</option>`).join("");
  const form=`<div class="ocard oform" id="oform" style="display:none">
    <div class="ofrow"><select id="of_type">${typeOpts}</select><select id="of_cust">${custOpts}</select></div>
    <div class="ofrow"><input id="of_guns" placeholder="Items / guns (e.g. 2x AK, 1x pistol)"><input id="of_qty" placeholder="Quantity (e.g. 3)"></div>
    <textarea id="of_notes" rows="2" placeholder="Notes - delivery spot, timing, budget…"></textarea>
    <div class="ofrow" style="justify-content:flex-end;gap:8px"><button class="ghost" onclick="toggleOrderForm(false)">Cancel</button><button class="brand-btn" onclick="createOrder()" ${types.length?"":"disabled"}>Create order &amp; open ticket</button></div></div>`;
  const typeRows=(STATE.ticketTypes||[]).map(t=>`<div class="trow"><span>${h(t.emoji||"🎫")} ${h(t.name)}</span><span class="srm" title="Remove this order type" onclick="removeTicketType('${h(t.name)}')">✕</span></div>`).join("")||'<div class="muted" style="font-size:13px">No order types yet - add one below.</div>';
  const typesPanel=`<div class="ocard oform" id="otypes" style="display:none">
    <div style="font-weight:700">Order types <span class="muted" style="font-weight:400;font-size:12px">- what people can open from the panel</span></div>
    <div class="tlist">${typeRows}</div>
    <div class="ofrow"><input id="tt_emoji" placeholder="🎫" maxlength="8" style="max-width:72px;text-align:center"><input id="tt_name" placeholder="New type name - e.g. Weapons Vendor or P90s Order"><button class="brand-btn" onclick="addTicketType()">Add type</button></div>
    <div class="muted" style="font-size:11.5px">After changes, run <b>/ticket panel</b> in Discord to repost the open menu.</div></div>`;
  const closedN=all.filter(o=>o.status!=="open").length;
  const clearBtn=closedN?`<button class="ghost" onclick="clearClosedOrders(${closedN})">🧹 Clear ${closedN} closed</button>`:"";
  const header=`<div class="obar"><button class="brand-btn" onclick="toggleOrderForm()">＋ New order</button><button class="ghost" onclick="toggleTypes()">⚙ Order types</button>${clearBtn}</div>`;
  // overall summary of what's being ordered (all open orders)
  const open=all.filter(o=>o.status==="open");
  const units=open.reduce((s,o)=>s+(parseInt(o.quantity,10)||0),0);
  const paidO=open.filter(o=>o.paid).length;
  let overview="";
  if(open.length){
    const tiles=`<div class="osum-tiles"><div><b>${open.length}</b><span>open orders</span></div><div><b>${units}</b><span>units ordered</span></div><div><b>${paidO}</b><span>paid</span></div><div><b>${open.length-paidO}</b><span>unpaid</span></div></div>`;
    const trows=open.slice().sort((a,b)=>(a.paid-b.paid)||((b.created_ts||0)-(a.created_ts||0))).map(o=>`<tr><td>${h(o.customer)}${o.username?` <span class="muted">@${h(o.username)}</span>`:""}</td><td>${h(o.emoji)} ${h(o.type)}</td><td><b>${h(o.guns||"-")}</b></td><td>${h(o.quantity||"-")}</td><td>${o.paid?'<span class="obadge paid">🟢 Paid</span>':'<span class="obadge unpaid">🔴 Unpaid</span>'}</td></tr>`).join("");
    overview=`<div class="ocard osum"><div class="osum-h">📊 Order summary <span class="muted" style="font-weight:400;font-size:12px">- who ordered what &amp; who's paid</span></div>${tiles}<div class="otwrap"><table class="otable"><thead><tr><th>Customer</th><th>Type</th><th>Ordered</th><th>Qty</th><th>Paid</th></tr></thead><tbody>${trows}</tbody></table></div></div>`;
  }
  if(!os.length){$("ordersView").innerHTML=header+form+typesPanel+overview+'<div class="empty">No matching orders. 📭</div>';return;}
  // group by category (ticket type) - an overall list of what the orders are
  const PR={urgent:0,high:1,normal:2,low:3};
  const groups={};
  os.forEach(o=>{(groups[o.type]=groups[o.type]||[]).push(o);});
  const sections=Object.keys(groups).sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase())).map(ty=>{
    const list=groups[ty].slice().sort((a,b)=>((a.status!=="open")-(b.status!=="open"))||((PR[a.priority]??2)-(PR[b.priority]??2))||((b.created_ts||0)-(a.created_ts||0)));
    const emoji=list[0].emoji||"🎫";
    const op=list.filter(o=>o.status==="open").length;
    const paidG=list.filter(o=>o.status==="open"&&o.paid).length;
    const head=`<div class="ranklabel">${h(emoji)} ${h(ty)}<span class="pill">${op} open · ${paidG} paid · ${list.length} total</span></div>`;
    return head+`<div class="olist">${list.map(orderCard).join("")}</div>`;
  }).join("");
  $("ordersView").innerHTML=header+form+typesPanel+overview+sections;
}
function orderCard(o){
  const paid=o.paid?'<span class="obadge paid">🟢 Paid</span>':'<span class="obadge unpaid">🔴 Unpaid</span>';
  const st=o.status!=="open"?'<span class="obadge closed">🔒 Closed</span>':"";
  const items=[o.guns?`<b>${h(o.guns)}</b>`:"",o.quantity?`× ${h(o.quantity)}`:""].filter(Boolean).join(" ");
  const who=`👤 ${h(o.customer)}${o.username?` <span class="muted">@${h(o.username)}</span>`:""}`;
  return `<div class="ocard${o.status!=="open"?" dim":""}">
    <div class="ohead"><div class="otitle">${h(o.emoji)} ${h(o.type)} <span class="oid">#${h(String(o.id))}</span></div>
      <div class="obadges">${paid}${st}${orderPrioPill(o.priority)}</div></div>
    <div class="ometa"><span>${who}</span>${items?`<span>🔫 ${items}</span>`:""}${o.created_ts?`<span>🕒 ${ago(o.created_ts)}</span>`:""}</div>
    ${o.notes?`<div class="reason">${h(o.notes)}</div>`:""}
    <div class="oactions">${o.status==="open"?`<button class="${o.paid?"ghost":"brand-btn"}" onclick="markOrderPaid('${h(String(o.id))}',${o.paid?"false":"true"})">${o.paid?"Mark unpaid":"Mark paid"}</button><button class="danger" onclick="closeOrder('${h(String(o.id))}')">Force close</button>`:""}</div></div>`;
}
async function clearClosedOrders(n){
  if(!await confirmBox({icon:"🧹",title:"Clear closed orders?",message:"Remove all "+n+" closed order(s) from the list. The Discord channels are already gone - this just clears the history here.",confirmText:"Clear"}))return;
  try{const r=await post("/api/orders_clear_closed",{guild:STATE.guild});toast("Cleared "+(r.removed||0)+" closed order(s)","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
function toggleOrderForm(on){const f=$("oform");if(!f)return;const open=on===undefined?f.style.display==="none":on;f.style.display=open?"":"none";}
async function createOrder(){
  const type=($("of_type")||{}).value||"";if(!type)return toast("Pick a ticket type","err");
  try{const r=await post("/api/order_create",{guild:STATE.guild,type,user_id:($("of_cust")||{}).value||"",guns:($("of_guns")||{}).value||"",quantity:($("of_qty")||{}).value||"",notes:($("of_notes")||{}).value||""});
    toast("Order #"+r.id+" created - ticket opened","ok");await reload();setTab("orders");}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function markOrderPaid(id,paid){
  try{await post("/api/order_paid",{guild:STATE.guild,order_id:id,paid});toast("Order #"+id+(paid?" marked paid":" marked unpaid"),"ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function closeOrder(id){
  if(!await confirmBox({icon:"🔒",title:"Force-close order?",message:"Force-close order #"+id+"? The ticket channel will be closed and a transcript saved.",confirmText:"Force close",danger:true}))return;
  try{await post("/api/order_close",{guild:STATE.guild,order_id:id});toast("Order #"+id+" closed","ok");await reload();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
function toggleTypes(){const f=$("otypes");if(f)f.style.display=f.style.display==="none"?"":"none";}
async function addTicketType(){
  const name=(($("tt_name")||{}).value||"").trim();if(!name)return toast("Type a name","err");
  try{const r=await post("/api/ticket_type_create",{guild:STATE.guild,name,emoji:($("tt_emoji")||{}).value||""});
    STATE.ticketTypes=r.types||[];toast("Saved order type","ok");render();const f=$("otypes");if(f)f.style.display="";}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function removeTicketType(name){
  if(!await confirmBox({icon:"🗑️",title:"Remove order type?",message:'Remove the order type "'+name+'"?',confirmText:"Remove",danger:true}))return;
  try{const r=await post("/api/ticket_type_remove",{guild:STATE.guild,name});
    STATE.ticketTypes=r.types||[];toast("Removed order type","ok");render();const f=$("otypes");if(f)f.style.display="";}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
/* ---- Settings tab ---- */
async function renderSettings(){
  const el=$("settingsView");
  if(!STATE.settings){
    el.innerHTML='<div class="card" style="text-align:center;color:#9a9aa6">Loading settings…</div>';
    try{const r=await api("/api/settings?guild="+encodeURIComponent(STATE.guild));STATE.settings=r.settings;}
    catch(e){if(e.status===401||e.status===403)return boot();el.innerHTML='<div class="card" style="color:#f2b8b8">'+h(e.message||"Couldn't load settings")+'</div>';return;}
  }
  const s=STATE.settings,pr=s.progress||{done:0,total:0};
  const pct=pr.total?Math.round(pr.done/pr.total*100):0;
  function opts(kind,val){
    var list=kind==="channel"?s.channels:s.roles,pre='#';if(kind==="role")pre='@';
    var o='<option value="">- not set -</option>';
    list.forEach(function(x){var lk=x.locked?" 🔒":"";o+='<option value="'+h(x.id)+'"'+(x.id===val?" selected":"")+'>'+pre+h(x.name)+lk+'</option>';});
    return o;
  }
  function row(it){
    return `<div class="setrow"><label>${it.emoji} ${h(it.label)}</label><select onchange="setSetting('${it.key}',this.value)">${opts(it.kind,it.value)}</select></div>`;
  }
  const roles=s.items.filter(i=>i.kind==="role"),mainc=s.items.filter(i=>i.kind==="channel"&&!i.is_log),logs=s.items.filter(i=>i.is_log);
  const webOn=s.web_enabled!==false;
  const wa=s.web_access||{allowed:[],candidates:[]};
  const chips=(wa.allowed||[]).length
    ? (wa.allowed||[]).map(function(m){return `<span class="wmchip">@${h(m.name)}<button class="x" title="Remove" onclick="removeWebMember('${h(m.id)}')">✕</button></span>`;}).join("")
    : '<div class="muted" style="font-size:13px">No extra members yet - only the server owner has access.</div>';
  const copts='<option value="">Select a member…</option>'+(wa.candidates||[]).map(function(c){return '<option value="'+h(c.id)+'">@'+h(c.name)+'</option>';}).join("");
  el.innerHTML=subCardHTML()+
    '<div class="card">'
      +'<div class="setuptop"><div><h3 style="margin:0">⚙️ Setup</h3>'
      +'<div class="muted" style="font-size:13px">'+pr.done+' of '+pr.total+' configured - pick a channel or role for each.</div></div>'
      +'</div>'
      +'<div class="pbar"><div class="pfill" style="width:'+pct+'%"></div></div>'
    +'</div>'
    +'<div class="card"><div class="setrow toggle"><label>🌐 Website panel</label>'
      +'<button class="tgl '+(webOn?'on':'')+'" onclick="toggleWeb('+(!webOn)+')">'+(webOn?'On':'Off')+'</button></div>'
      +'<div class="muted" style="font-size:12px;margin-top:4px">Turn the whole web panel off for this server. You can switch it back on here any time.</div></div>'
    +'<div class="card"><h4>👥 Website access</h4>'
      +'<div class="muted" style="font-size:12px;margin-bottom:8px">The server owner always has access. Add any members who should be able to use this panel.</div>'
      +'<div class="wmlist">'+chips+'</div>'
      +'<div class="addbar" style="margin-top:12px">'
        +'<input id="wmFilter" class="i" placeholder="Search members…" oninput="filterWM()">'
        +'<select id="wmSel" class="i">'+copts+'</select>'
        +'<button onclick="addWebMember()">➕ Add</button></div>'
      +(( (wa.candidates||[]).length===0 )?'<div class="muted" style="font-size:12px;margin-top:6px">No members to list yet - enable the Server Members Intent to populate this, or add by selecting once they appear.</div>':'')+'</div>'
    +'<div class="card"><h4>👑 Roles</h4>'+roles.map(row).join("")+'</div>'
    +'<div class="card"><div class="setuptop"><h4 style="margin:0">📁 Channels</h4>'
      +'<button class="ghost" onclick="testWelcome(this)">✉️ Test welcome</button></div>'
      +'<div class="muted" style="font-size:12px;margin:4px 0 8px">Posts the welcome card to your Welcome channel right now so you can check it - no need to wait for someone to join.</div>'
      +mainc.map(row).join("")+'</div>'
    +'<div class="card"><div class="setuptop"><h4 style="margin:0">📝 Log channels</h4>'
      +'<button class="ghost" onclick="logsSetup()">⚡ Auto-create all</button></div>'
      +'<div class="muted" style="font-size:12px;margin:6px 0">Creates a private <b>📋 Gang Manager Logs</b> category with a channel for every log type and wires them up. (Bot needs Manage Channels.)</div>'
      +logs.map(row).join("")+'</div>';
}
async function setSetting(key,value){
  try{const r=await post("/api/setting_set",{guild:STATE.guild,key,value});STATE.settings=r.settings;toast("Saved","ok");renderSettings();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");STATE.settings=null;renderSettings();}
}
async function testWelcome(btn){
  if(btn)btn.disabled=true;
  try{const r=await post("/api/welcome_test",{guild:STATE.guild});toast("Welcome posted to #"+(r.channel||"welcome")+" ✓","ok");}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Couldn't send test welcome","err");}
  finally{if(btn)btn.disabled=false;}
}
async function toggleWeb(on){
  try{const r=await post("/api/web_toggle",{guild:STATE.guild,enabled:on});toast("Website panel "+(r.enabled?"on":"off"),"ok");STATE.settings=null;renderSettings();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
function filterWM(){
  var q=(($("wmFilter")||{}).value||"").toLowerCase();
  var cands=((STATE.settings||{}).web_access||{}).candidates||[];
  var sel=$("wmSel");if(!sel)return;
  var list=q?cands.filter(function(c){return c.name.toLowerCase().indexOf(q)>-1;}):cands;
  sel.innerHTML='<option value="">Select a member…</option>'+list.slice(0,500).map(function(c){return '<option value="'+h(c.id)+'">@'+h(c.name)+'</option>';}).join("");
}
async function addWebMember(){
  var id=(($("wmSel")||{}).value||"");if(!id)return toast("Pick a member","err");
  try{const r=await post("/api/web_member_add",{guild:STATE.guild,user_id:id});STATE.settings=r.settings;toast("Access granted","ok");renderSettings();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
async function removeWebMember(id){
  try{const r=await post("/api/web_member_remove",{guild:STATE.guild,user_id:id});STATE.settings=r.settings;toast("Access removed","ok");renderSettings();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
function subCardHTML(){
  var st=STATE.settings||{},s=st.subscription||{},g=encodeURIComponent(STATE.guild);
  var price=((STATE.pub||{}).plan||{}).price||"$7.99 / month";
  var until=s.expires_at?new Date(s.expires_at).toLocaleDateString(undefined,{day:"numeric",month:"short",year:"numeric"}):"";
  var pill,line,acts="";
  if(s.mode==="free"){pill=`<span class="spill ok">Free mode</span>`;line="Billing is switched off - everything is unlocked.";}
  else if(s.mode==="lifetime"){pill=`<span class="spill ok">Lifetime</span>`;line="Lifetime access - this server never expires.";}
  else if(s.mode==="paid"){
    pill=`<span class="spill ok">Active</span>`;line=`Paid until <b>${h(until)}</b>${s.has_billing?" · renews automatically":""}.`;
    acts=s.has_billing?`<a href="/billing/portal?guild=${g}"><button>💳 Manage billing</button></a>`:`<a href="/billing/checkout?guild=${g}"><button>💳 Subscribe</button></a>`;
  }else{
    pill=`<span class="spill bad">${s.status==="expired"?"Expired":"Not subscribed"}</span>`;line="The bot's features are paused in this server until it's subscribed.";
    acts=`<a href="/billing/checkout?guild=${g}"><button class="brand-btn">💳 Subscribe - ${h(price)}</button></a>`;
  }
  return `<div class="card subcard"><div class="subhead"><div><h3 style="margin:0">💳 Subscription</h3><div class="muted" style="font-size:13px;margin-top:3px">${line}</div></div>${pill}</div>${acts?`<div class="subacts">${acts}</div>`:""}</div>`;
}
async function subAdmin(action){
  if(action==="expire"&&!await confirmBox({icon:"⚠️",title:"Expire subscription?",message:"Expire this server's subscription now? Paid features pause until it's renewed.",confirmText:"Expire",danger:true}))return;
  try{const r=await post("/api/sub_admin",{guild:STATE.guild,action:action});toast(r.message||"Done","ok");
    STATE.subs[STATE.guild]=r.subscription;STATE.settings=null;renderSubBanner();renderSettings();}
  catch(e){if(e.status===401)return boot();toast(e.message||"Failed","err");}
}
async function logsSetup(){
  if(!await confirmBox({icon:"🗂️",title:"Create log channels?",message:"Create a Logs category with a channel for every log type?",confirmText:"Create"}))return;
  try{const r=await post("/api/logs_setup",{guild:STATE.guild});toast("Created "+r.created+" log channel(s)"+(r.kept?" (kept "+r.kept+")":""),"ok");STATE.settings=r.settings;renderSettings();}
  catch(e){if(e.status===401||e.status===403)return boot();toast(e.message||"Failed","err");}
}
boot();
</script>
</body>
</html>
"""


# export for the entrypoint (start_web_server) via star import
__all__ = [n for n in list(globals()) if not n.startswith("__")]
