"""Bot pool — extra Gang Manager bots that each serve one customer server.

Your main bot is untouched. The pool is a list of extra bots (all owned by
you) whose tokens you paste into Admin → Bots. Each one runs in this same
process with exactly the same slash commands, buttons and events as the main
bot, and reads/writes the same data — so every pool bot is linked to the main
bot through the shared store and the website.

When the pool is switched ON, a subscribed server that hasn't got a bot yet is
handed its own free pool bot on "Add bot" instead of the shared main bot.
With the switch OFF (the default) nothing changes: the main bot is handed out
exactly as before. Pool bots you've added still come online so you can check
them, and any server already given a pool bot keeps it.
"""

import asyncio
import base64
import datetime
import hashlib
import os
import pathlib
import re

from .core import *  # noqa: F401,F403
from .billing import is_owner

try:  # tokens are encrypted at rest (cryptography is in requirements.txt)
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # noqa: BLE001 — without it the pool just refuses to store tokens
    Fernet = None  # type: ignore[assignment]

    class InvalidToken(Exception):  # type: ignore[no-redef]
        pass


MAX_POOL = 100        # hard cap on pool bots in one process
RESERVE_HOURS = 24    # an "Add bot" hand-out that never joins is reclaimed after this
LAUNCH_GAP = 1.5      # seconds between pool-bot logins at startup

_ICON = pathlib.Path(__file__).with_name("gm-icon.png")
_DESCRIPTION = (
    "Run your gang like a machine — roster, KOS, wars, strikes, tickets and "
    "recruitment, with a full web control panel. Add it & subscribe at https://gangmanager.site"
)
# Bios we set in an earlier version — safe to refresh to the current one. A bio
# that isn't blank and isn't one of these is treated as custom and left alone.
_OLD_DESCRIPTIONS = frozenset({
    "Gang Manager — roster, KOS, wars, tickets and recruitment for your FiveM gang, "
    "with a full web control panel.",
})


def _desc_for(current) -> "str | None":
    """The bio to write for a pool bot, or None to leave it unchanged. Fills a
    blank bio and refreshes one we set before; never overwrites a custom bio."""
    cur = (current or "").strip()
    if cur == _DESCRIPTION or (cur != "" and cur not in _OLD_DESCRIPTIONS):
        return None
    return _DESCRIPTION
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{18,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{20,}$")

# Discord application flag bits. Apps in fewer than 100 servers may switch the
# "limited" privileged-intent flags on themselves through the API.
_FLAG_PRESENCE_LIMITED = 1 << 13
_FLAG_MEMBERS = 1 << 14
_FLAG_MEMBERS_LIMITED = 1 << 15
_FLAG_CONTENT_LIMITED = 1 << 19

_RUNNING: dict = {}     # bot id -> {"client", "task", "members", "status"}
_SETUP: dict = {"views": None, "handlers": [], "guild_join": None}
_SYNCED: set = set()    # pool bots whose slash commands were synced this process
_STARTED = False


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def pool_state() -> dict:
    """The saved pool registry: {"enabled": bool, "bots": {bot_id: record}}."""
    p = DATA.get("pool")
    if not isinstance(p, dict):
        p = DATA["pool"] = {}
    p.setdefault("enabled", False)
    if not isinstance(p.get("bots"), dict):
        p["bots"] = {}
    return p


def pool_enabled() -> bool:
    return bool(pool_state().get("enabled"))


def set_pool_enabled(on: bool) -> bool:
    pool_state()["enabled"] = bool(on)
    save_data(DATA)
    log.info("Bot pool hand-outs %s.", "ON" if on else "OFF")
    return bool(on)


def _rec(bot_id):
    return pool_state()["bots"].get(str(bot_id))


def _client(bot_id):
    try:
        return (_RUNNING.get(int(bot_id)) or {}).get("client")
    except (TypeError, ValueError):
        return None


def _is_online(bot_id) -> bool:
    w = _client(bot_id)
    try:
        return w is not None and w.user is not None and w.is_ready() and not w.is_closed()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Token encryption (never stored or returned in plain text)
# --------------------------------------------------------------------------- #

def _fernet():
    if Fernet is None:
        return None
    secret = os.getenv("POOL_SECRET") or TOKEN or ""
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(b"gang-manager-pool:" + secret.encode()).digest())
    return Fernet(key)


def crypto_status() -> dict:
    if Fernet is None:
        return {"ok": False, "why": "The 'cryptography' package isn't installed yet — redeploy so requirements.txt installs it."}
    if _fernet() is None:
        return {"ok": False, "why": "No encryption key — set POOL_SECRET (or DISCORD_TOKEN) in Railway."}
    return {"ok": True, "source": "POOL_SECRET" if os.getenv("POOL_SECRET") else "DISCORD_TOKEN"}


def _encrypt(token: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError(crypto_status().get("why") or "Token encryption unavailable.")
    return f.encrypt(token.encode()).decode()


def _decrypt(blob):
    f = _fernet()
    if f is None or not blob:
        return None
    try:
        return f.decrypt(str(blob).encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Handing pool bots out
# --------------------------------------------------------------------------- #

def _free_ids(gid: str) -> list:
    bots = pool_state()["bots"]
    out = [
        b for b, r in bots.items()
        if not r.get("assigned_guild")
        and r.get("status") not in ("bad_token", "locked")
        and r.get("public", True) is not False
        and _is_online(b)
    ]
    # prefer the bot this server had last time, then the oldest
    out.sort(key=lambda b: (bots[b].get("last_guild") != gid, bots[b].get("added_at") or ""))
    return out


def reclaim_stale() -> int:
    """Free hand-outs whose bot never joined within RESERVE_HOURS. Only bots
    that are online (so they can confirm they're not in the server) qualify."""
    now = datetime.datetime.now(datetime.timezone.utc)
    n = 0
    for b, r in pool_state()["bots"].items():
        g = r.get("assigned_guild")
        if not g or not _is_online(b):
            continue
        if _client(b).get_guild(int(g)) is not None:
            continue
        at = _parse_iso(r.get("assigned_at"))
        if at is not None and (now - at).total_seconds() < RESERVE_HOURS * 3600:
            continue
        r["last_guild"], r["assigned_guild"], r["assigned_at"] = g, None, None
        r.pop("joined_at", None)
        n += 1
    if n:
        save_data(DATA)
        log.info("Bot pool: reclaimed %d unused hand-out(s).", n)
    return n


def assign_bot_for_guild(guild_id):
    """The pool bot to add to this server: the one it already has, else a free
    online one. None means "hand out the main bot" (pool off / none free)."""
    try:
        gid = str(int(guild_id))
    except (TypeError, ValueError):
        return None
    bots = pool_state()["bots"]
    for b, r in bots.items():
        if str(r.get("assigned_guild") or "") != gid:
            continue
        healthy = r.get("status") not in ("bad_token", "locked")
        if healthy and (_is_online(b) or r.get("joined_at")):
            return int(b)
        # broken/offline reservation that never joined — hand out another one
        r["assigned_guild"] = r["assigned_at"] = None
        save_data(DATA)
        break
    if not pool_enabled():
        return None
    free = _free_ids(gid)
    if not free and reclaim_stale():
        free = _free_ids(gid)
    if not free:
        log.warning("Bot pool: no free pool bots for server %s — handing out the main bot.", gid)
        return None
    b = free[0]
    bots[b]["assigned_guild"] = gid
    bots[b]["assigned_at"] = _iso_now()
    bots[b].pop("joined_at", None)
    save_data(DATA)
    log.info("Bot pool: reserved pool bot %s for server %s.", b, gid)
    return int(b)


# --------------------------------------------------------------------------- #
# Adding tokens: verify ownership, auto-configure, then bring online
# --------------------------------------------------------------------------- #

def _split_tokens(raw: str) -> list:
    parts = []
    for p in re.split(r"[\s,;]+", raw or ""):
        t = p.strip().strip("'\"`")
        if t and t.lower() != "bot":
            parts.append(t)
    return list(dict.fromkeys(parts))


async def _inspect(token: str) -> dict:
    """Log in over HTTP only (no gateway) to read who owns this bot, check its
    settings, and fill in branding + the members intent where Discord lets a
    bot do that for itself. Raises ValueError with a readable reason."""
    probe = discord.Client(intents=discord.Intents.none())
    try:
        try:
            await probe.login(token)
        except discord.LoginFailure:
            raise ValueError("Discord rejected that token — copy a fresh one (Bot → Reset Token).")
        except discord.HTTPException as exc:
            raise ValueError(f"Discord error while checking it: {exc}")
        user = probe.user
        if user is None or not getattr(user, "bot", False):
            raise ValueError("That isn't a bot token.")
        app = getattr(probe, "application", None)
        if app is None:
            try:
                app = await probe.application_info()
            except discord.HTTPException:
                app = None

        owner_ids: set = set()
        team_id = None
        if app is not None:
            team = getattr(app, "team", None)
            if team is not None:
                team_id = getattr(team, "id", None)
                if getattr(team, "owner_id", None):
                    owner_ids.add(int(team.owner_id))
                for m in getattr(team, "members", None) or []:
                    try:
                        owner_ids.add(int(m.id))
                    except (TypeError, ValueError, AttributeError):
                        pass
            owner = getattr(app, "owner", None)
            if owner is not None and team is None:
                owner_ids.add(int(owner.id))

        info = {
            "id": int(user.id),
            "name": str(user.name),
            "owner_ids": owner_ids,
            "team_id": team_id,
            "public": bool(getattr(app, "bot_public", True)) if app is not None else True,
            "code_grant": bool(getattr(app, "bot_require_code_grant", False)) if app is not None else False,
            "members_intent": None,
            "branded": [],
        }

        icon = _ICON.read_bytes() if _ICON.exists() else None
        # Branding — only fill what's blank, never overwrite a custom look.
        if icon and getattr(user, "avatar", None) is None:
            try:
                await user.edit(avatar=icon)
                info["branded"].append("avatar")
            except (discord.HTTPException, TypeError, ValueError):
                pass

        if app is not None and hasattr(app, "edit"):
            changes: dict = {}
            if icon and getattr(app, "icon", None) is None:
                changes["icon"] = icon
            _nd = _desc_for(getattr(app, "description", ""))
            if _nd is not None:
                changes["description"] = _nd
            flags_val = int(getattr(getattr(app, "flags", None), "value", 0) or 0)
            has_members = bool(flags_val & (_FLAG_MEMBERS | _FLAG_MEMBERS_LIMITED))
            info["members_intent"] = has_members
            if not has_members:
                try:
                    changes["flags"] = discord.ApplicationFlags(
                        gateway_presence_limited=bool(flags_val & _FLAG_PRESENCE_LIMITED),
                        gateway_guild_members_limited=True,
                        gateway_message_content_limited=bool(flags_val & _FLAG_CONTENT_LIMITED),
                    )
                except Exception:  # noqa: BLE001
                    pass
            if changes:
                try:
                    await app.edit(**changes)
                    info["branded"].extend(k for k in changes if k != "flags")
                    if "flags" in changes:
                        info["members_intent"] = True
                except (discord.HTTPException, TypeError, ValueError) as exc:
                    log.info("Pool bot %s: couldn't auto-configure everything (%s)", user.id, exc)
                    if "flags" in changes and len(changes) > 1:
                        changes.pop("flags")
                        try:
                            await app.edit(**changes)
                            info["branded"].extend(changes)
                        except (discord.HTTPException, TypeError, ValueError):
                            pass
        return info
    finally:
        try:
            await probe.close()
        except Exception:  # noqa: BLE001
            pass


async def add_tokens(raw: str) -> dict:
    """Add one or many bot tokens (one per line, or separated by spaces/commas).
    Each is checked to be a bot you own, auto-configured, stored encrypted and
    brought online. Tokens are never logged or echoed back."""
    status = crypto_status()
    if not status["ok"]:
        return {"ok": False, "error": status["why"], "results": []}
    tokens = _split_tokens(raw)
    if not tokens:
        return {"ok": False, "error": "Paste at least one bot token.", "results": []}
    tokens = tokens[:MAX_POOL]

    main_team = None
    try:
        main_app = await bot.application_info()
        main_team = getattr(getattr(main_app, "team", None), "id", None)
    except Exception:  # noqa: BLE001
        pass
    main_id = getattr(getattr(bot, "user", None), "id", None)

    sem = asyncio.Semaphore(4)
    results: list = [None] * len(tokens)

    async def one(i: int, tok: str) -> None:
        n = i + 1
        async with sem:
            if not _TOKEN_RE.match(tok):
                results[i] = {"ok": False, "n": n, "error": "Doesn't look like a bot token."}
                return
            try:
                info = await _inspect(tok)
            except ValueError as exc:
                results[i] = {"ok": False, "n": n, "error": str(exc)}
                return
            except Exception as exc:  # noqa: BLE001
                results[i] = {"ok": False, "n": n, "error": f"Couldn't check it ({exc.__class__.__name__})."}
                return
            bid = info["id"]
            if main_id and bid == main_id:
                results[i] = {"ok": False, "n": n, "error": "That's your main bot's token — it isn't a pool bot."}
                return
            owned = any(is_owner(x) for x in info["owner_ids"]) or (
                main_team is not None and info["team_id"] == main_team
            )
            if not owned:
                results[i] = {"ok": False, "n": n, "error": "That bot isn't owned by you or your team."}
                return
            existed = _rec(bid) is not None
            if not existed and len(pool_state()["bots"]) >= MAX_POOL:
                results[i] = {"ok": False, "n": n, "error": f"The pool is full ({MAX_POOL} bots)."}
                return
            rec = pool_state()["bots"].setdefault(str(bid), {"added_at": _iso_now(), "assigned_guild": None})
            rec.update({
                "token": _encrypt(tok),
                "name": info["name"],
                "public": info["public"],
                "code_grant": info["code_grant"],
            })
            if info["members_intent"] is not None:
                rec["members_intent"] = bool(info["members_intent"])
            rec.pop("status", None)
            save_data(DATA)
            await _launch(bid, restart=True)
            results[i] = {
                "ok": True, "n": n, "id": str(bid), "name": info["name"], "updated": existed,
                "public": info["public"], "code_grant": info["code_grant"],
                "members_intent": info["members_intent"], "branded": info["branded"],
            }

    await asyncio.gather(*(one(i, t) for i, t in enumerate(tokens)))
    added = sum(1 for r in results if r and r.get("ok"))
    log.info("Bot pool: %d of %d token(s) added.", added, len(tokens))
    return {"ok": True, "added": added, "results": results}


# --------------------------------------------------------------------------- #
# Running pool bots
# --------------------------------------------------------------------------- #

async def _post_note(guild, text: str) -> None:
    me = getattr(guild, "me", None)
    try:
        ch = getattr(guild, "system_channel", None)
        if ch is None or (me is not None and not ch.permissions_for(me).send_messages):
            ch = next(
                (c for c in guild.text_channels if me is not None and c.permissions_for(me).send_messages),
                None,
            )
        if ch is not None:
            await ch.send(text)
    except Exception:  # noqa: BLE001
        pass


def _wire(w) -> None:
    """Pool-bot-only events: buttons, command sync, staying in its one server."""

    async def on_ready() -> None:
        if w.user is None:
            return
        bid = w.user.id
        st = _RUNNING.get(bid)
        if st is not None:
            st["status"] = "online"
        rec = _rec(bid)
        if rec is not None:
            rec["name"] = str(w.user.name)
            try:
                rec["avatar"] = str(w.user.display_avatar.url)
            except Exception:  # noqa: BLE001
                pass
        if not getattr(w, "_gm_ready_once", False):
            w._gm_ready_once = True
            if _SETUP["views"] is not None:
                try:
                    _SETUP["views"](w)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Pool bot %s: couldn't register buttons: %s", bid, exc)
            if bid not in _SYNCED:
                try:
                    await w.tree.sync()
                    _SYNCED.add(bid)
                except discord.HTTPException as exc:
                    log.warning("Pool bot %s: slash command sync failed: %s", bid, exc)
            try:  # refresh the settings snapshot shown in Admin → Bots
                app = await w.application_info()
                if rec is not None:
                    rec["public"] = bool(getattr(app, "bot_public", True))
                    rec["code_grant"] = bool(getattr(app, "bot_require_code_grant", False))
                _nd = _desc_for(getattr(app, "description", ""))
                if _nd is not None:  # keep the store link in the bio (never a custom one)
                    await app.edit(description=_nd)
            except Exception:  # noqa: BLE001
                pass
        try:
            await w.change_presence(status=discord.Status.online, activity=discord.CustomActivity(name="🥷 /help"))
        except Exception:  # noqa: BLE001
            pass
        # A pool bot only ever lives in the one server it was handed out for.
        assigned = str((rec or {}).get("assigned_guild") or "")
        for g in list(w.guilds):
            if str(g.id) != assigned:
                log.info("Pool bot %s leaving %s (%s): not its assigned server", bid, g.name, g.id)
                try:
                    await g.leave()
                except discord.HTTPException:
                    pass
            elif rec is not None:
                rec.setdefault("joined_at", _iso_now())
                rec["guild_name"] = g.name
        save_data(DATA)
        log.info("Pool bot %s (%s) online — serving %s", bid, w.user.name, assigned or "nobody yet")

    async def on_guild_join(guild) -> None:
        if w.user is None:
            return
        bid = w.user.id
        rec = _rec(bid)
        if rec is None or str(rec.get("assigned_guild") or "") != str(guild.id):
            log.info("Pool bot %s added to %s (%s) without a hand-out — leaving", bid, guild.name, guild.id)
            await _post_note(
                guild,
                "👋 This Gang Manager bot is reserved for another server, so I've left. To get Gang "
                "Manager here, subscribe on the website and use **Add bot** there — you'll be given "
                "your own bot.",
            )
            try:
                await guild.leave()
            except discord.HTTPException:
                pass
            return
        rec["joined_at"] = _iso_now()
        rec["guild_name"] = guild.name
        save_data(DATA)
        log.info("Pool bot %s joined its server %s (%s)", bid, guild.name, guild.id)
        if _SETUP["guild_join"] is not None:
            await _SETUP["guild_join"](guild)  # same allowlist + subscription check as the main bot

    async def on_guild_remove(guild) -> None:
        if w.user is None:
            return
        rec = _rec(w.user.id)
        if rec is not None and str(rec.get("assigned_guild") or "") == str(guild.id):
            rec["last_guild"] = str(guild.id)
            rec["assigned_guild"] = rec["assigned_at"] = None
            rec.pop("joined_at", None)
            save_data(DATA)
            log.info("Pool bot %s is free again (removed from %s)", w.user.id, guild.id)

    w.add_listener(on_ready, "on_ready")
    w.add_listener(on_guild_join, "on_guild_join")
    w.add_listener(on_guild_remove, "on_guild_remove")


def _make_worker(members: bool):
    """A bot client that mirrors the main bot: same slash commands, same
    paywall check, same member events, same persistent buttons."""
    intents = discord.Intents.default()
    intents.members = bool(members)
    w = commands.Bot(command_prefix="!", intents=intents, help_command=None)
    for cmd in bot.tree.get_commands():
        try:
            w.tree.add_command(cmd)
        except Exception as exc:  # noqa: BLE001
            log.debug("pool: skipped command %s: %s", getattr(cmd, "name", cmd), exc)
    for attr in ("interaction_check", "on_error"):
        if attr in vars(bot.tree):
            setattr(w.tree, attr, getattr(bot.tree, attr))
    for name, fn in _SETUP["handlers"]:
        w.add_listener(fn, name)
    _wire(w)
    return w


async def _run_worker(bid: int, token: str) -> None:
    st = _RUNNING.get(bid)
    if st is None:
        return
    members = (_rec(bid) or {}).get("members_intent", True) is not False
    tries = 0
    while True:
        if _RUNNING.get(bid) is not st:
            return  # stopped or replaced
        w = _make_worker(members)
        st.update(client=w, members=members, status="starting")
        WORKERS.append(w)
        retry, delay = False, 0
        try:
            await w.start(token)
            st["status"] = "stopped"
        except discord.PrivilegedIntentsRequired:
            log.warning(
                "Pool bot %s: its Server Members Intent is off — running without it. Turn it on in its "
                "Developer Portal page, then press Restart in Admin → Bots.", bid,
            )
            members, retry = False, True
            r = _rec(bid)
            if r is not None:
                r["members_intent"] = False
                save_data(DATA)
        except discord.LoginFailure:
            log.error("Pool bot %s: its token was rejected — re-add it in Admin → Bots.", bid)
            st["status"] = "bad_token"
            r = _rec(bid)
            if r is not None:
                r["status"] = "bad_token"
                save_data(DATA)
        except asyncio.CancelledError:
            st["status"] = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            tries += 1
            st["status"] = "error"
            log.warning("Pool bot %s stopped unexpectedly (%s) — retry %d/5", bid, exc, tries)
            retry, delay = tries < 5, min(60, 5 * tries)
        finally:
            if w in WORKERS:
                WORKERS.remove(w)
            try:
                if not w.is_closed():
                    await w.close()
            except Exception:  # noqa: BLE001
                pass
        if not retry or _RUNNING.get(bid) is not st:
            return
        if delay:
            await asyncio.sleep(delay)


async def _launch(bid: int, restart: bool = False) -> bool:
    bid = int(bid)
    rec = _rec(bid)
    if rec is None:
        return False
    token = _decrypt(rec.get("token"))
    if not token:
        rec["status"] = "locked"
        save_data(DATA)
        log.error("Pool bot %s: can't decrypt its token (key changed?) — re-add it in Admin → Bots.", bid)
        return False
    cur = _RUNNING.get(bid)
    if cur and cur.get("task") is not None and not cur["task"].done():
        if not restart:
            return True
        await stop_worker(bid)
    st = {"status": "starting", "members": rec.get("members_intent", True) is not False}
    _RUNNING[bid] = st
    st["task"] = asyncio.create_task(_run_worker(bid, token), name=f"pool-bot-{bid}")
    return True


async def stop_worker(bid) -> None:
    st = _RUNNING.pop(int(bid), None) or {}
    w, task = st.get("client"), st.get("task")
    if w is not None:
        try:
            if not w.is_closed():
                await w.close()
        except Exception:  # noqa: BLE001
            pass
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if w is not None and w in WORKERS:
        WORKERS.remove(w)


async def start_pool(*, views=None, handlers=(), guild_join=None) -> None:
    """Called once from the main bot's on_ready: remember how to wire pool bots
    and bring every saved pool bot online (staggered)."""
    global _STARTED
    _SETUP.update(views=views, handlers=list(handlers), guild_join=guild_join)
    if _STARTED:
        return
    _STARTED = True
    ids = list(pool_state()["bots"].keys())
    if not ids:
        log.info("Bot pool: no pool bots added yet (Admin → Bots).")
        return
    if _fernet() is None:
        log.error("Bot pool: %d pool bot(s) saved but %s", len(ids), crypto_status().get("why"))
        return

    async def _go() -> None:
        for bid in ids:
            try:
                await _launch(int(bid))
            except Exception as exc:  # noqa: BLE001
                log.warning("Bot pool: couldn't start %s: %s", bid, exc)
            await asyncio.sleep(LAUNCH_GAP)

    asyncio.create_task(_go(), name="pool-launcher")
    log.info("Bot pool: starting %d pool bot(s); hand-outs are %s.", len(ids), "ON" if pool_enabled() else "OFF")


# --------------------------------------------------------------------------- #
# Admin actions
# --------------------------------------------------------------------------- #

async def free_bot(bid) -> dict:
    """Take a pool bot off its server (it leaves) and make it free again."""
    rec = _rec(bid)
    if rec is None:
        raise KeyError(bid)
    g = rec.get("assigned_guild")
    rec["assigned_guild"] = rec["assigned_at"] = None
    rec["last_guild"] = None
    rec.pop("joined_at", None)
    rec.pop("guild_name", None)
    save_data(DATA)
    left = False
    if g:
        w = _client(bid)
        guild = w.get_guild(int(g)) if w is not None else None
        if guild is not None:
            try:
                await guild.leave()
                left = True
            except discord.HTTPException:
                pass
    log.info("Bot pool: pool bot %s freed from %s (left=%s)", bid, g, left)
    return {"ok": True, "left": left, "guild": g}


async def remove_bot(bid, force: bool = False) -> dict:
    """Delete a pool bot from the pool (its token is forgotten)."""
    rec = _rec(bid)
    if rec is None:
        raise KeyError(bid)
    if rec.get("assigned_guild") and not force:
        raise ValueError("This bot is serving a server — free it first, or remove it anyway.")
    if rec.get("assigned_guild"):
        await free_bot(bid)
    await stop_worker(bid)
    pool_state()["bots"].pop(str(bid), None)
    _SYNCED.discard(int(bid))
    save_data(DATA)
    log.info("Bot pool: pool bot %s removed.", bid)
    return {"ok": True}


async def restart_bot(bid) -> dict:
    """Reconnect a pool bot, retrying the members intent (e.g. after enabling it)."""
    rec = _rec(bid)
    if rec is None:
        raise KeyError(bid)
    rec["members_intent"] = True
    rec.pop("status", None)
    save_data(DATA)
    ok = await _launch(int(bid), restart=True)
    return {"ok": ok}


def pool_overview() -> dict:
    """Everything Admin → Bots shows. Never includes tokens."""
    rows = []
    for b, r in pool_state()["bots"].items():
        w = _client(b)
        online = _is_online(b)
        st = _RUNNING.get(int(b)) or {}
        g = r.get("assigned_guild")
        guild = w.get_guild(int(g)) if (online and g) else None
        avatar = r.get("avatar")
        if online:
            try:
                avatar = str(w.user.display_avatar.url)
            except Exception:  # noqa: BLE001
                pass
        rows.append({
            "id": str(b),
            "name": r.get("name") or str(b),
            "avatar": avatar,
            "online": online,
            "status": "online" if online else (r.get("status") or st.get("status") or "offline"),
            "members_intent": bool(st.get("members")) if online else (r.get("members_intent", True) is not False),
            "public": r.get("public", True) is not False,
            "code_grant": bool(r.get("code_grant")),
            "assigned_guild": g or None,
            "assigned_name": (guild.name if guild is not None else r.get("guild_name") or ""),
            "in_guild": guild is not None,
            "assigned_at": r.get("assigned_at"),
            "joined_at": r.get("joined_at"),
            "added_at": r.get("added_at"),
        })
    rows.sort(key=lambda x: x["added_at"] or "")
    counts = {
        "total": len(rows),
        "online": sum(1 for x in rows if x["online"]),
        "serving": sum(1 for x in rows if x["in_guild"]),
        "reserved": sum(1 for x in rows if x["assigned_guild"] and not x["in_guild"]),
        "free": sum(1 for x in rows if x["online"] and not x["assigned_guild"]),
    }
    return {"enabled": pool_enabled(), "crypto": crypto_status(), "bots": rows, "counts": counts, "max": MAX_POOL}


__all__ = [
    "MAX_POOL", "pool_state", "pool_enabled", "set_pool_enabled", "crypto_status",
    "assign_bot_for_guild", "reclaim_stale", "add_tokens", "start_pool", "stop_worker",
    "free_bot", "remove_bot", "restart_bot", "pool_overview",
]
