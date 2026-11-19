"""
Gang Manager — FiveM gang management bot (entrypoint).

Run with `python gang_bot.py`. Features live in the tenshi_bot package:
  tenshi_bot/core.py — Config, data store, the bot object, shared helpers (logging, branding, V2 cards), EOI + role-menu views and command groups.
  tenshi_bot/roster.py — /roster — live roster from rank roles, ranks, import_roles, add/remove/reset.
  tenshi_bot/kos.py — /kos — KOS list, sign-up → draw → staff approval → announce flow.
  tenshi_bot/settings.py — /settings — channel & role configuration.
  tenshi_bot/panels.py — /role_menu, /radio, /eoi, /eoi_post and /help.
  tenshi_bot/tickets.py — Ticket / order system: panels, tickets, transcripts, priority, auto-close, stats.
  tenshi_bot/gang.py — /gang dashboard & profile, /strike, /war (live scoreboards).
"""

import os
import sys

from tenshi_bot.core import *  # noqa: F401,F403
from tenshi_bot.roster import *  # noqa: F401,F403
from tenshi_bot.kos import *  # noqa: F401,F403
from tenshi_bot.settings import *  # noqa: F401,F403
from tenshi_bot.panels import *  # noqa: F401,F403
from tenshi_bot.tickets import *  # noqa: F401,F403
from tenshi_bot.gang import *  # noqa: F401,F403
from tenshi_bot.billing import *  # noqa: F401,F403
from tenshi_bot.web import *  # noqa: F401,F403
from tenshi_bot.web import _gmlog, _fire  # underscore names aren't picked up by *
from tenshi_bot.emojis import sync_app_emojis
from tenshi_bot.pool import *  # noqa: F401,F403


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

bot.tree.add_command(roster_group)
bot.tree.add_command(kos_group)
bot.tree.add_command(role_menu_group)
bot.tree.add_command(radio_group)
bot.tree.add_command(ticket_group)
bot.tree.add_command(strike_group)
bot.tree.add_command(war_group)
bot.tree.add_command(gang_group)
bot.tree.add_command(settings_group)
bot.tree.add_command(subscription_group)

# Paywall: block paid commands in a server whose subscription has lapsed.
# No-op unless BILLING_ENABLED is set (see billing.py).
async def _main_gate(interaction) -> bool:
    # Maintenance switch first, then the paywall.
    if MAIN_OFF["v"]:
        try:
            await interaction.response.send_message(
                "🛠️ The bot is in maintenance right now — back shortly.", ephemeral=True)
        except Exception:  # noqa: BLE001
            pass
        return False
    return await subscription_gate(interaction)


bot.tree.interaction_check = _main_gate


# The distribution bot carries no slash commands of its own: it hands out the
# pool bots, runs the website and sits in the hub. Hand the full command set to
# the pool (its sub bots serve customers), then clear the main tree so the
# distributor syncs with nothing. The gate above stays put for the pool, which
# copies it; only this bot goes command-less. Set GM_DISTRIBUTOR_COMMANDS=1 to
# keep commands on the main bot (the old behaviour).
if os.getenv("GM_DISTRIBUTOR_COMMANDS", "").strip().lower() not in ("1", "true", "yes", "on"):
    set_pool_commands(bot.tree.get_commands())
    bot.tree.clear_commands(guild=None)
    log.info("Distribution bot: cleared own commands - the pool serves them instead.")


# --------------------------------------------------------------------------- #
# Live status — rotates every 30s through real numbers
# --------------------------------------------------------------------------- #

def _status_activities() -> list[discord.BaseActivity]:
    """The distributor bot's presence rotation, built fresh each tick off live
    fleet numbers — it hands out the pool bots, so its status is about that.
    Set BOT_STATUS to pin a line of your own.
    """
    active = [g for g in all_guilds() if guild_active(g.id)]
    members = sum(g.member_count or 0 for g in active)
    wars = 0
    for g in active:
        store = guild_store(DATA, g.id)
        wars += sum(1 for w in store.get("wars", []) if w.get("active", True))
    bots_online = 0
    try:  # best-effort: the pool may not be up yet
        bots_online = int(pool_overview().get("counts", {}).get("online", 0))
    except Exception:  # noqa: BLE001 — a status blip must never matter
        bots_online = 0

    def line(text: str) -> discord.CustomActivity:
        return discord.CustomActivity(name=text[:128])

    acts: list[discord.BaseActivity] = []
    custom = (os.getenv("BOT_STATUS") or "").strip()
    if custom:
        acts.append(line(custom))
    acts.append(line("📦 distributing Gang Manager bots"))
    if bots_online:
        acts.append(line(f"📡 running {bots_online} bot{'s' if bots_online != 1 else ''} across the fleet"))
    if active:
        acts.append(line(f"🏴 handing bots to {len(active)} gang{'s' if len(active) != 1 else ''}"))
    if members:
        acts.append(line(f"👥 {members:,} member{'s' if members != 1 else ''} on the network"))
    if wars:
        acts.append(line(f"⚔️ {wars} gang war{'s' if wars != 1 else ''} live"))
    acts.append(line("🥷 gangmanager.site"))
    return acts


_status_i = 0


@tasks.loop(seconds=30)
async def status_loop() -> None:
    global _status_i
    if MAIN_OFF["v"]:
        return  # stay invisible while powered off
    try:
        acts = _status_activities()
        if not acts:
            return
        act = acts[_status_i % len(acts)]
        _status_i += 1
        await bot.change_presence(status=discord.Status.online, activity=act)
    except Exception as exc:  # noqa: BLE001 — a status blip must never stop the loop
        log.debug("status update skipped: %s", exc)


@status_loop.before_loop
async def _before_status_loop() -> None:
    await bot.wait_until_ready()


@bot.listen("on_interaction")
async def _trace_interaction(interaction: discord.Interaction) -> None:
    # slim trace (debug level): which component was clicked, by whom
    if interaction.type == discord.InteractionType.component:
        data = interaction.data or {}
        log.debug(
            "component: custom_id=%s user=%s", data.get("custom_id"), interaction.user
        )


def register_persistent_views(client) -> None:
    """Buttons and menus that must keep working after a restart. Registered on
    the main bot and on every pool bot, so they behave the same everywhere."""
    client.add_view(EOI_VIEW)
    client.add_view(EOIPromptView())
    client.add_view(SELF_ROLE_VIEW)
    client.add_view(RADIO_PANEL_VIEW)
    client.add_view(TICKET_PANEL_VIEW)
    client.add_view(ORDER_TICKET_VIEW)
    client.add_view(MUSTER_VIEW)
    client.add_view(STAFF_DRAW_VIEW)
    client.add_view(ROSTER_PROPOSAL_VIEW)
    client.add_view(ROSTER_REFRESH_VIEW)
    client.add_view(RatingView())
    client.add_view(DashboardView())
    client.add_view(WarPanelView())
    client.add_view(HelpView())
    client.add_dynamic_items(VouchDecisionButton)


@bot.event
async def on_ready() -> None:
    # on_ready fires again on every reconnect; the one-time setup (buttons,
    # Stripe heal, bot pool) only needs to run once per process.
    first = not getattr(bot, "_gm_ready_once", False)
    if first:
        bot._gm_ready_once = True
        register_persistent_views(bot)
        await sync_app_emojis(bot)  # upload the GM custom emojis to this app
    if not status_loop.is_running():
        status_loop.start()
    if MAIN_OFF["v"]:
        try:
            await bot.change_presence(status=discord.Status.invisible)
        except Exception:  # noqa: BLE001
            pass
    if not ticket_autoclose_loop.is_running():
        ticket_autoclose_loop.start()
    if not eoi_timer_loop.is_running():
        eoi_timer_loop.start()
    # Recognise whoever owns the bot application as the operator (no env needed).
    await detect_app_owners()
    await start_web_server()
    if first:
        # Stripe is the source of truth for payments: re-apply live subscriptions
        # in the background, so a reset data file never locks out paying servers.
        async def _stripe_heal():
            try:
                await sync_subscriptions_from_stripe()
            except Exception as exc:
                log.error("Stripe sync failed: %s", exc)
        asyncio.create_task(_stripe_heal())
    # A global slash-command sync is one of Discord's most rate-limited calls —
    # do it once per process, not on every reconnect.
    if not getattr(bot, "_gm_synced", False):
        try:
            synced = await bot.tree.sync()
            bot._gm_synced = True
            log.info("Synced %d slash commands.", len(synced))
        except discord.DiscordException as exc:
            log.error("Command sync failed: %s", exc)
    log.info("Logged in as %s (id: %s)", bot.user, bot.user.id)
    log.info(
        "discord.py %s · Components V2 available: %s",
        discord.__version__,
        hasattr(discord.ui, "LayoutView"),
    )
    log.info(
        "In %d guild(s): %s",
        len(bot.guilds),
        ", ".join(f"{g.name} ({g.id})" for g in bot.guilds) or "none",
    )
    # enforce the server allowlist on startup, if one is configured
    if ALLOWED_GUILD_IDS:
        for g in list(bot.guilds):
            if g.id not in ALLOWED_GUILD_IDS:
                log.warning("Leaving unauthorized guild %s (%s)", g.name, g.id)
                try:
                    await g.leave()
                except discord.HTTPException:
                    pass
    # Extra pool bots (Admin → Bots) — a no-op until pool bots have been added.
    if first:
        try:
            await start_pool(
                views=register_persistent_views,
                handlers=[("on_member_join", on_member_join), ("on_member_remove", on_member_remove),
                          ("on_member_update", on_member_update), ("on_member_ban", on_member_ban),
                          ("on_member_unban", on_member_unban)],
                guild_join=on_guild_join,
            )
        except Exception as exc:  # noqa: BLE001 — the pool must never take the main bot down
            log.error("Bot pool failed to start: %s", exc)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    # if an allowlist is set, immediately leave any server not on it
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        log.warning("Leaving unauthorized guild %s (%s)", guild.name, guild.id)
        try:
            await guild.leave()
        except discord.HTTPException:
            pass
        return

    # A subscription is required BEFORE the bot can be added. The website only
    # offers "Add bot" for subscribed servers; this catches copied invite links.
    # Servers owned by the bot owner are allowed in so they can be comped.
    if BILLING_ENABLED and not peek_active(guild.id) and not is_owner(getattr(guild, "owner_id", None)):
        log.info("Leaving %s (%s): no active subscription", guild.name, guild.id)
        site = web_base_url()
        where = f" at {site}/panel" if site else " on the Gang Manager website"
        note = (
            "👋 Thanks for adding **Gang Manager**! This server doesn't have an active "
            f"subscription yet, so I've left. Subscribe for this server{where}, then add me "
            "again from there."
        )
        channel = guild.system_channel
        me = guild.me
        if channel is None or (me is not None and not channel.permissions_for(me).send_messages):
            channel = next(
                (c for c in guild.text_channels if me is not None and c.permissions_for(me).send_messages),
                None,
            )
        if channel is not None:
            try:
                await channel.send(note)
            except discord.HTTPException:
                pass
        try:
            await guild.leave()
        except discord.HTTPException:
            pass
        return

    # Added and staying (paid, owner-comped, or allowlisted) — log it.
    try:
        _fire(_gmlog("server", "🌐 Bot added to a server", fields=[
            {"name": "Server", "value": f"{guild.name} (`{guild.id}`)", "inline": False},
            {"name": "Members", "value": str(getattr(guild, "member_count", None) or "—"), "inline": True},
        ]))
    except Exception:  # noqa: BLE001
        pass


@bot.event
async def on_member_join(member: discord.Member) -> None:
    guild = member.guild
    if not _should_log(guild):
        return  # not our server to log (wrong bot, or an unsubscribed customer)
    store = guild_store(DATA, guild.id)

    # public welcome message
    wid = store.get("welcome_channel_id")
    wchannel = guild.get_channel(wid) if wid else None
    if wchannel is not None:
        try:
            await _send_card(
                wchannel,
                [welcome_embed(guild, member)],
                header=member.mention,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            log.warning(
                "Welcome: no permission to post in #%s (guild %s)",
                getattr(wchannel, "name", wchannel), guild.id,
            )
        except discord.HTTPException as exc:
            log.warning("Welcome: send failed in guild %s: %s", guild.id, exc)

    # join log - target= renders member + account age + exact time; add badges,
    # a new-account flag, and the fleet member count.
    fields = [("Member #", f"#{guild.member_count}", True)]
    badges = _user_badges(member)
    if badges:
        fields.append(("Badges", badges, True))
    meta = {}
    if _is_new_account(member):
        meta["Heads up"] = "⚠️ Account < 7 days old"
    await send_log(
        guild, store, f"📥 {member.mention} **joined**.",
        category="joins", target=member, fields=fields, meta=meta or None,
    )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    guild = member.guild
    if not _should_log(guild):
        return  # not our server to log (wrong bot, or an unsubscribed customer)
    store = guild_store(DATA, guild.id)
    joined = member.joined_at
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    fields = [("Members now", f"#{guild.member_count}", True)]
    top = getattr(member, "top_role", None)
    if top is not None and top.name != "@everyone":
        fields.append(("Top role", top.mention, True))
    if joined is not None:
        fields.append(("Joined", f"<t:{int(joined.timestamp())}:F> · <t:{int(joined.timestamp())}:R>", False))
        fields.append(("Time in server", _since(joined), True))
    fields.append(("Roles held", str(len(roles)), True))
    if roles:
        fields.append(("Roles", " ".join(roles)[:1024], False))
    await send_log(
        guild, store, f"📤 **{member}** left.",
        category="joins", target=member, fields=fields,
    )


# --------------------------------------------------------------------------- #
# Deeper audit log: role, nickname, timeout, ban and unban changes. These feed
# the "members" (Role) log channel, which little else writes to. Attribution
# ("by whom") is best-effort from the server audit log, so a missing View Audit
# Log permission never breaks logging - it just drops the "By" line.
# --------------------------------------------------------------------------- #

def _should_log(guild) -> bool:
    """Whether THIS bot should write member logs for `guild`. Only the bot that
    serves it (no double logs from the main bot + a pool bot sharing a server),
    and either the hub / main server (always - your own server logs even though
    it carries no subscription) or a subscription-active customer server."""
    if not serves_guild(guild):
        return False
    if str(getattr(guild, "id", "")) == admin_guild():
        return True  # the operator's own server always logs
    return guild_active(guild.id)


def _user_badges(user) -> str:
    """Public badge names from a user's flags, as a compact string (or '')."""
    try:
        flags = user.public_flags
        names = [n.replace("_", " ").title() for n, on in flags if on]
        return ", ".join(names)[:1024] if names else ""
    except Exception:  # noqa: BLE001
        return ""


def _is_new_account(user, days: int = 7) -> bool:
    created = getattr(user, "created_at", None)
    if created is None:
        return False
    try:
        return (discord.utils.utcnow() - created).days < days
    except Exception:  # noqa: BLE001
        return False


def _since(dt) -> str:
    """Human 'Xd Yh' between dt and now, best-effort."""
    try:
        secs = int((discord.utils.utcnow() - dt).total_seconds())
    except Exception:  # noqa: BLE001
        return "-"
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def _audit_actor(guild, action, target_id=None, within: int = 20):
    """Best-effort: who performed `action` (optionally on `target_id`) from the
    audit log, and that entry's reason. Returns (user|None, reason|None). Never
    raises - missing perms or rate limits just yield (None, None)."""
    try:
        me = getattr(guild, "me", None)
        perms = getattr(getattr(me, "guild_permissions", None), "view_audit_log", False)
        if not perms:
            return None, None
        async for entry in guild.audit_logs(limit=6, action=action):
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            try:
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            except Exception:  # noqa: BLE001
                age = 0
            if age <= within:
                return entry.user, entry.reason
            return None, None
    except (discord.Forbidden, discord.HTTPException):
        return None, None
    except Exception as exc:  # noqa: BLE001
        log.debug("audit actor lookup failed: %s", exc)
    return None, None


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    guild = after.guild
    if not _should_log(guild):
        return
    store = guild_store(DATA, guild.id)

    # Role changes
    b_roles, a_roles = set(before.roles), set(after.roles)
    added = [r for r in after.roles if r not in b_roles and r.name != "@everyone"]
    removed = [r for r in before.roles if r not in a_roles and r.name != "@everyone"]
    if added or removed:
        actor, _ = await _audit_actor(guild, discord.AuditLogAction.member_role_update, after.id)
        fields = []
        if added:
            fields.append(("➕ Added", " ".join(r.mention for r in added)[:1024], False))
        if removed:
            fields.append(("➖ Removed", " ".join(r.mention for r in removed)[:1024], False))
        live = [r for r in after.roles if r.name != "@everyone"]
        fields.append(("Roles now", str(len(live)), True))
        top = getattr(after, "top_role", None)
        if top is not None and top.name != "@everyone":
            fields.append(("Top role", top.mention, True))
        await send_log(guild, store, f"🎭 Roles updated for {after.mention}.",
                       category="members", actor=actor, target=after, fields=fields)

    # Nickname
    if before.nick != after.nick:
        actor, _ = await _audit_actor(guild, discord.AuditLogAction.member_update, after.id)
        await send_log(guild, store, f"✏️ Nickname changed for {after.mention}.",
                       category="members", actor=actor, target=after, fields=[
                           ("Before", f"`{before.nick}`" if before.nick else "*(none)*", True),
                           ("After", f"`{after.nick}`" if after.nick else "*(none)*", True)])

    # Timeout (communication disabled until)
    bt = getattr(before, "timed_out_until", None)
    at_ = getattr(after, "timed_out_until", None)
    if bt != at_:
        actor, reason = await _audit_actor(guild, discord.AuditLogAction.member_update, after.id)
        if at_ is not None:
            await send_log(guild, store, f"⏳ {after.mention} was **timed out**.",
                           category="members", actor=actor, target=after, reason=reason, fields=[
                               ("Until", f"<t:{int(at_.timestamp())}:F> · <t:{int(at_.timestamp())}:R>", False)])
        else:
            await send_log(guild, store, f"✅ Timeout lifted for {after.mention}.",
                           category="members", actor=actor, target=after)


@bot.event
async def on_member_ban(guild: discord.Guild, user) -> None:
    if not _should_log(guild):
        return
    store = guild_store(DATA, guild.id)
    actor, reason = await _audit_actor(guild, discord.AuditLogAction.ban, getattr(user, "id", None))
    await send_log(guild, store, f"🔨 **{user}** was **banned**.",
                   category="members", actor=actor, target=user, reason=reason)


@bot.event
async def on_member_unban(guild: discord.Guild, user) -> None:
    if not _should_log(guild):
        return
    store = guild_store(DATA, guild.id)
    actor, reason = await _audit_actor(guild, discord.AuditLogAction.unban, getattr(user, "id", None))
    await send_log(guild, store, f"♻️ **{user}** was **unbanned**.",
                   category="members", actor=actor, target=user, reason=reason)


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Add it as an environment variable."
        )
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        # The members intent is requested by default (welcome + join/leave logs),
        # but the "Server Members Intent" toggle is off in the Developer Portal.
        # Don't crash a live bot — restart once WITHOUT the intent so it keeps
        # running. Welcome/join logs stay off until the portal toggle is enabled.
        if os.getenv("MEMBERS_INTENT", "true").lower() in ("0", "false", "no", "off"):
            raise  # already running without it — avoid a restart loop
        log.error(
            "Server Members Intent is OFF in the Developer Portal, so welcome "
            "messages and join/leave logs are disabled. Enable it at Developer "
            "Portal -> your app -> Bot -> Privileged Gateway Intents -> Server "
            "Members Intent, then restart. Running without it for now."
        )
        os.environ["MEMBERS_INTENT"] = "false"
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
