"""/roster — live roster from rank roles, ranks, import_roles, add/remove/reset."""

from .core import *  # noqa: F401,F403


# ---- /roster ---------------------------------------------------------------- #

def _roster_role(guild: discord.Guild, store: dict[str, Any]) -> discord.Role | None:
    """The configured roster role for this guild, or None if unset/deleted."""
    rid = store.get("roster_role_id")
    return guild.get_role(rid) if rid else None


async def _ensure_members(guild: discord.Guild) -> None:
    """Make sure the member list is loaded so ``role.members`` is accurate before
    building a roster. A no-op when the guild is already cached or the members
    intent is off — never raises."""
    try:
        if not guild.chunked:
            await guild.chunk()
    except Exception:  # noqa: BLE001
        pass


def _apply_roster_roles(
    guild: discord.Guild, store: dict[str, Any], roles: "list[discord.Role]"
) -> list[str]:
    """Set the roster's ranks to exactly the picked roles — the clean way to choose
    what shows up. Keeps any custom rank name already mapped to a role, else uses
    the role's own name. Highest role first, capped at 25 (Discord's field limit)."""
    old_names = {r["role_id"]: r["name"] for r in store.get("ranks", [])}
    # drop @everyone / managed (bot, booster, integration) roles — never rostered
    clean = [r for r in roles if not r.is_default() and not r.managed]
    picked = sorted(clean, key=lambda r: r.position, reverse=True)[:25]
    store["ranks"] = [
        {"name": (old_names.get(role.id) or role.name).strip() or role.name,
         "role_id": role.id}
        for role in picked
    ]
    return [r["name"] for r in store["ranks"]]


def _detect_roster_roles(guild: discord.Guild) -> "list[discord.Role]":
    """Best-guess the roster's rank roles with zero setup: the server's hoisted
    roles (the ones shown as their own group in the member list — almost always
    gang ranks), highest first. Falls back to any role that has real (non-bot)
    members if nothing is hoisted. Skips @everyone and bot/booster/integration
    roles; capped at 25 (Discord's embed-field limit)."""
    usable = [r for r in guild.roles if not r.is_default() and not r.managed]
    hoisted = [r for r in usable if r.hoist]
    pool = hoisted or [r for r in usable if any(not m.bot for m in r.members)]
    return sorted(pool, key=lambda r: r.position, reverse=True)[:25]


def _live_roster_embed(
    guild: discord.Guild, store: dict[str, Any]
) -> discord.Embed:
    """Build the roster straight from the rank roles and who currently holds them."""
    embed = discord.Embed(
        title="🎯  Gang Roster",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    ranks = store.get("ranks", [])
    counted: set[int] = set()
    rank_role_ids: set[int] = set()

    # resolve rank → role, drop deleted roles, order by role position (top first)
    resolved = []
    for r in ranks:
        role = guild.get_role(r["role_id"])
        if role is None:
            continue
        resolved.append((r["name"], role))
        rank_role_ids.add(role.id)
    resolved.sort(key=lambda nr: nr[1].position, reverse=True)

    for name, role in resolved:
        members = role.members
        for m in members:
            counted.add(m.id)
        if members:
            shown = members[:40]
            value = " ".join(m.mention for m in shown)
            if len(members) > len(shown):
                value += f"\n*+{len(members) - len(shown)} more*"
        else:
            value = "*nobody*"
        embed.add_field(
            name=f"{name} · {len(members)}", value=value[:1024], inline=False
        )

    # anyone with the default roster role but none of the rank roles
    base = _roster_role(guild, store)
    if base is not None:
        extras = [
            m
            for m in base.members
            if m.id not in counted
            and not any(rr.id in rank_role_ids for rr in m.roles)
        ]
        for m in extras:
            counted.add(m.id)
        if extras:
            shown = extras[:40]
            value = " ".join(m.mention for m in shown)
            if len(extras) > len(shown):
                value += f"\n*+{len(extras) - len(shown)} more*"
            embed.add_field(
                name=f"Unranked · {len(extras)}", value=value[:1024], inline=False
            )

    if not embed.fields:
        embed.description = (
            "No ranks mapped yet.\nMap a rank to a server role with "
            "`/roster rank_set <name> <role>` and everyone in that role shows up here."
        )
    _brand(embed, guild, f"👥 {len(counted)} total · tap 🔄 to refresh")
    return embed


class RosterRefreshView(GatedView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="roster:refresh",
    )
    async def refresh(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        store = guild_store(DATA, interaction.guild_id)
        await _edit_card(
            interaction.message,
            [_live_roster_embed(interaction.guild, store)],
            RosterRefreshView,
            interaction=interaction,
        )


ROSTER_REFRESH_VIEW = RosterRefreshView()


class RosterRolesPicker(GatedView):
    """A native Discord role picker — leadership ticks which server roles make up
    the roster, each becoming a rank ordered by its place in the server. Opens
    pre-filled with the current selection, so it doubles as the editor. Auto-detect
    fills it from the server's rank roles (zero setup), Clear empties it, and the
    picker itself unticks to remove."""

    def __init__(self, guild: discord.Guild, store: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        current = []
        for r in store.get("ranks", []):
            role = guild.get_role(r["role_id"])
            if role is not None:
                current.append(role)
        select = discord.ui.RoleSelect(
            placeholder="Pick the roles that make up your roster…",
            min_values=0,
            max_values=25,
            default_values=current[:25],
            custom_id="roster:roles_pick",
            row=0,
        )
        select.callback = self._on_pick
        self._select = select
        self.add_item(select)

    @staticmethod
    def _body(names: "list[str]", lead: str = "") -> str:
        if names:
            listing = "\n".join(f"`{i + 1:>2}` {n}" for i, n in enumerate(names))
            return (
                f"{lead}**Roster roles — {len(names)} rank(s), top to bottom:**\n{listing}\n\n"
                "Untick any above to remove it, or tick more. Run `/roster view` to see "
                "everyone grouped by these."
            )
        return (
            f"{lead}No roles on the roster right now. Tick some above, or tap "
            "**✨ Auto-detect** to fill it from your server's rank roles."
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        store = guild_store(DATA, interaction.guild_id)
        names = _apply_roster_roles(interaction.guild, store, self._select.values)
        save_data(DATA)
        await interaction.response.edit_message(content=self._body(names)[:2000], view=self)
        await send_log(
            interaction.guild,
            store,
            f"🎯 {interaction.user.mention} set **{len(names)}** roster role(s) with the picker.",
            category="roster",
            actor=interaction.user,
        )

    @discord.ui.button(label="Auto-detect", style=discord.ButtonStyle.primary,
                       emoji="✨", custom_id="roster:roles_auto", row=1)
    async def _auto(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store = guild_store(DATA, interaction.guild_id)
        names = _apply_roster_roles(interaction.guild, store, _detect_roster_roles(interaction.guild))
        save_data(DATA)
        if names:
            lead = (f"✨ Detected **{len(names)}** rank role(s) from your server — "
                    "untick any that aren't gang ranks.\n\n")
        else:
            lead = ("Couldn't spot obvious rank roles (none are set to display separately). "
                    "Tick them above manually.\n\n")
        await interaction.response.edit_message(
            content=self._body(names, lead)[:2000],
            view=RosterRolesPicker(interaction.guild, store),
        )
        if names:
            await send_log(
                interaction.guild,
                store,
                f"🎯 {interaction.user.mention} auto-detected **{len(names)}** roster role(s).",
                category="roster",
                actor=interaction.user,
            )

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.secondary,
                       emoji="🗑️", custom_id="roster:roles_clear", row=1)
    async def _clear(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store = guild_store(DATA, interaction.guild_id)
        store["ranks"] = []
        save_data(DATA)
        await interaction.response.edit_message(
            content=self._body([])[:2000],
            view=RosterRolesPicker(interaction.guild, store),
        )


_ID_RE = re.compile(r"(\d{17,20})")


async def _collect_members(
    guild: discord.Guild,
    first: discord.Member,
    more_text: str | None,
    limit: int = 30,
) -> list[discord.Member]:
    """A picked member plus any @mentions/IDs pasted in `more_text`, up to `limit`."""
    out: list[discord.Member] = [first]
    seen = {first.id}
    for raw in _ID_RE.findall(more_text or ""):
        mid = int(raw)
        if mid in seen:
            continue
        seen.add(mid)
        member = guild.get_member(mid)
        if member is None:
            try:
                member = await guild.fetch_member(mid)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is not None:
            out.append(member)
        if len(out) >= limit:
            break
    return out


def _rank_role(
    guild: discord.Guild, store: dict[str, Any], rank_name: str
) -> discord.Role | None:
    """The server role mapped to a rank name, or None."""
    for r in store.get("ranks", []):
        if r["name"].lower() == rank_name.lower():
            return guild.get_role(r["role_id"])
    return None


async def rank_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest the guild's configured rank names while typing the rank option."""
    store = guild_store(DATA, interaction.guild_id)
    cur = current.lower()
    return [
        app_commands.Choice(name=r["name"], value=r["name"])
        for r in store.get("ranks", [])
        if cur in r["name"].lower()
    ][:25]


@roster_group.command(
    name="rank_set",
    description="Map a rank name to a server role (leadership)",
)
@app_commands.describe(
    name="Rank name (e.g. OG, Prospect, Driver)",
    role="The server role this rank gives",
)
async def roster_rank_set(
    interaction: discord.Interaction, name: str, role: discord.Role
) -> None:
    if await _deny(interaction, "set ranks"):
        return
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    name_clean = name.strip()
    for r in ranks:
        if r["name"].lower() == name_clean.lower():
            r["name"] = name_clean
            r["role_id"] = role.id
            break
    else:
        if len(ranks) >= 25:
            await interaction.response.send_message(
                "You can have at most 25 ranks.", ephemeral=True
            )
            return
        ranks.append({"name": name_clean, "role_id": role.id})
    save_data(DATA)
    await interaction.response.send_message(
        f"Rank **{name_clean}** → {role.mention}. Pick it when adding to the roster. ✅",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        store,
        f"🎖️ Rank **{name_clean}** mapped to {role.mention}.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="rank_remove", description="Remove a configured rank (leadership)"
)
@app_commands.describe(name="The rank name to remove")
@app_commands.autocomplete(name=rank_autocomplete)
async def roster_rank_remove(interaction: discord.Interaction, name: str) -> None:
    if await _deny(interaction, "remove ranks"):
        return
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["ranks"])
    store["ranks"] = [r for r in store["ranks"] if r["name"].lower() != name.lower()]
    if len(store["ranks"]) == before:
        await interaction.response.send_message(
            f"No rank called **{name}**.", ephemeral=True
        )
        return
    save_data(DATA)
    await interaction.response.send_message(
        f"Removed rank **{name}**. 🗑️", ephemeral=True
    )
    await send_log(
        interaction.guild,
        store,
        f"🎖️ Rank **{name}** removed.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="rank_list", description="Show the configured ranks and their roles"
)
async def roster_rank_list(interaction: discord.Interaction) -> None:
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    embed = discord.Embed(title="🎖️ Ranks", color=EMBED_COLOR)
    if not ranks:
        embed.description = "No ranks yet. Add some with `/roster rank_set`."
    else:
        embed.description = "\n".join(
            f"**{r['name']}** → <@&{r['role_id']}>" for r in ranks
        )
    await _send_v2(interaction, embed, ephemeral=True)


@roster_group.command(
    name="import_roles",
    description="Add every server role as a rank in one go (leadership)",
)
async def roster_import_roles(interaction: discord.Interaction) -> None:
    if await _deny(interaction, "import roles"):
        return
    store = guild_store(DATA, interaction.guild_id)
    ranks = store["ranks"]
    existing_ids = {r["role_id"] for r in ranks}
    existing_names = {r["name"].lower() for r in ranks}

    # highest roles first; skip @everyone and bot/integration/booster roles
    candidates = [
        role
        for role in sorted(
            interaction.guild.roles, key=lambda r: r.position, reverse=True
        )
        if not role.is_default() and not role.managed
    ]

    added, skipped = [], 0
    capped = False
    for role in candidates:
        if len(ranks) >= 25:
            capped = True
            break
        if role.id in existing_ids:
            continue
        name = role.name.strip()
        if name.lower() in existing_names:
            skipped += 1
            continue
        ranks.append({"name": name, "role_id": role.id})
        existing_ids.add(role.id)
        existing_names.add(name.lower())
        added.append(role.mention)
    save_data(DATA)

    lines = []
    if added:
        lines.append(
            f"✅ Imported **{len(added)}** role(s) as ranks: {', '.join(added)}"
        )
    else:
        lines.append("Nothing new to import — every role is already a rank.")
    if capped:
        lines.append(
            "⚠️ Hit the 25-rank cap (Discord embeds max out at 25 groups). "
            "Trim with `/roster rank_remove` if you need different ones."
        )
    lines.append("Now run `/roster view` to see everyone grouped by rank.")
    await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)
    if added:
        await send_log(
            interaction.guild,
            store,
            f"🎖️ {interaction.user.mention} imported **{len(added)}** role(s) as ranks.",
            category="roster",
            actor=interaction.user,
        )


@roster_group.command(
    name="roles",
    description="Pick which server roles make up the roster (leadership)",
)
async def roster_roles(interaction: discord.Interaction) -> None:
    if await _deny(interaction, "set roster roles"):
        return
    store = guild_store(DATA, interaction.guild_id)
    auto = ""
    if not store.get("ranks"):
        detected = _detect_roster_roles(interaction.guild)
        if detected:
            _apply_roster_roles(interaction.guild, store, detected)
            save_data(DATA)
            auto = (f"✨ Auto-detected **{len(store['ranks'])}** rank role(s) from your server — "
                    "untick any that aren't gang ranks below.\n\n")
    view = RosterRolesPicker(interaction.guild, store)
    await interaction.response.send_message(
        auto +
        "**Roster roles**\n"
        "Tick every role that should appear on the roster — each becomes a rank, "
        "ordered by its place in your server. Untick to remove. @everyone and bot roles "
        "are ignored. Changes save instantly, and **✨ Auto-detect** refills from your "
        "server any time.",
        view=view,
        ephemeral=True,
    )


@roster_group.command(
    name="view",
    description="Show the roster, built live from your rank roles",
)
async def roster_view(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    store = guild_store(DATA, interaction.guild_id)
    await _ensure_members(interaction.guild)
    embed = _live_roster_embed(interaction.guild, store)
    await _send_card(interaction, [embed], RosterRefreshView)


@roster_group.command(
    name="add",
    description="Add up to 30 members to the roster and give them a rank's role",
)
@app_commands.describe(
    member="Member to add (@mention them)",
    more="Paste extra @mentions or IDs here to add many at once (up to 30 total)",
    rank="Rank to assign — pick from your configured ranks (optional)",
)
@app_commands.autocomplete(rank=rank_autocomplete)
async def roster_add(
    interaction: discord.Interaction,
    member: discord.Member,
    more: str | None = None,
    rank: str | None = None,
) -> None:
    await interaction.response.defer()
    store = guild_store(DATA, interaction.guild_id)
    rank_role = _rank_role(interaction.guild, store, rank) if rank else None
    base_role = _roster_role(interaction.guild, store)
    # A picked rank's role wins; otherwise fall back to the default roster role.
    role = rank_role or base_role
    targets = await _collect_members(interaction.guild, member, more, limit=30)

    added, skipped, role_fail = [], [], []
    for m in targets:
        if any(e.get("id") == m.id for e in store["roster"]):
            skipped.append(m.mention)
            continue
        if role is not None:
            try:
                await m.add_roles(role, reason=f"Roster add by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException):
                role_fail.append(m.mention)
        store["roster"].append(
            {
                "id": m.id,
                "name": m.display_name,
                "note": rank,
                "added_by": str(interaction.user),
                "at": now_stamp(),
            }
        )
        added.append(m.mention)
    save_data(DATA)

    as_rank = f" as **{rank}**" if rank else ""
    lines = []
    if added:
        lines.append(f"✅ Added **{len(added)}** to the roster{as_rank}: {', '.join(added)}")
    if skipped:
        lines.append(f"ℹ️ Already on the roster: {', '.join(skipped)}.")
    if rank and rank_role is None:
        lines.append(
            f"⚠️ No role is mapped to rank **{rank}** — set one with `/roster rank_set`."
        )
    elif role is None:
        lines.append(
            "⚠️ No role to give — set a rank with `/roster rank_set`, or set the "
            "**Roster default role** with `/settings role`."
        )
    elif role_fail:
        lines.append(
            f"⚠️ Couldn't give the {role.mention} role to {', '.join(role_fail)} "
            "— check I have **Manage Roles** and my role sits above it."
        )
    await interaction.followup.send("\n".join(lines)[:2000] or "Nothing to add.")

    if added:
        await send_log(
            interaction.guild,
            store,
            f"📋 Added **{len(added)}** to the roster{as_rank}.",
            category="roster",
            actor=interaction.user,
            fields=[
                ("Members", ", ".join(added), False),
                ("Rank", rank or "—", True),
                ("Roster size", str(len(store["roster"])), True),
            ],
        )


@roster_group.command(
    name="remove",
    description="Remove a member from the roster and take the role back",
)
@app_commands.describe(member="The member to remove (@mention them)")
async def roster_remove(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    store = guild_store(DATA, interaction.guild_id)
    before = len(store["roster"])
    store["roster"] = [m for m in store["roster"] if m.get("id") != member.id]
    if len(store["roster"]) == before:
        await interaction.response.send_message(
            f"{member.mention} isn't on the roster.", ephemeral=True
        )
        return

    role = _roster_role(interaction.guild, store)
    note = ""
    if role is not None and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Roster remove by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            note = f"\n⚠️ Couldn't take the {role.mention} role off them."

    save_data(DATA)
    await interaction.response.send_message(
        f"Removed {member.mention} from the roster. 🗑️{note}"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} removed {member.mention} from the roster.",
        category="roster",
        actor=interaction.user,
    )


@roster_group.command(
    name="reset",
    description="Wipe the roster list (keeps everyone's role) (leadership)",
)
async def roster_reset(interaction: discord.Interaction) -> None:
    if await _deny(interaction, "reset the roster"):
        return
    store = guild_store(DATA, interaction.guild_id)
    count = len(store["roster"])
    store["roster"] = []
    save_data(DATA)
    await interaction.response.send_message(
        f"Roster wiped — {count} members cleared. Roles left untouched. 🧹"
    )
    await send_log(
        interaction.guild,
        store,
        f"{interaction.user.mention} wiped the roster ({count} cleared).",
        category="roster",
        actor=interaction.user,
    )


# export everything (incl. _helpers) to the modules that build on this one
__all__ = [n for n in list(globals()) if not n.startswith("__")]
