from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db.models import GuildConfig, LFGMember, LFGParty, Player

if TYPE_CHECKING:
    from ..bot import AkimBot


# ---------------- i18n strings ----------------

_I18N: dict[str, dict[str, str]] = {
    "ru": {
        "status_open": "Открыто",
        "status_closed": "Закрыто",
        "party_roster": "СПИСОК УЧАСТНИКОВ",
        "party_progress": "📊 **Прогресс группы**",
        "voice_channel": "🔊 Голосовой канал",
        "note": "📝 Заметка",
        "start_time": "📅 Начало",
        "footer_open": "Открыто",
        "footer_closed": "Закрыто",
        "created_by": "создано",
        "saturday": "📅 Суббота",
        "sunday": "📅 Воскресенье",
        "reserve": "🔁 Резерв",
        "main_label": "Основные",
        "full": "ПОЛНЫЙ",
        "day_sat": "Суббота",
        "day_sun": "Воскресенье",
        "day_both": "Оба дня",
        "lang_button": "EN",
        "registered_msg": "✅ Зарегистрирован: **{day}** → {slot}",
        "updated_msg": "✅ Обновлено: **{day}** ({slot})",
        "slot_reserve": "**Резерв** (основные заполнены)",
        "slot_main": "**Основные**",
        "slot_reserve_short": "Резерв",
        "slot_main_short": "Основные",
        "register_title_new": "{remaining} осталось основных мест. Выберите день:",
        "register_title_full": "⚠️ Основной состав заполнен ({current}/{limit}). Вы попадёте в **Резерв**.\n\nВыберите день:",
        "register_title_update": "Текущая регистрация: **{day}** {slot}\n\nВыберите другой день:",
        "select_day_placeholder": "Выберите день(и)...",
        "confirm_button": "Подтвердить",
        "cancel_button": "Отмена",
        "lfg_card_title": "🎮 Поиск группы",
        "lfg_card_desc": (
            "Нажмите кнопку ниже, чтобы создать группу:\n\n"
            "⚔️ **Hero Realm** — 10 игроков\n"
            "🗡️ **Sword Trial** — 5 игроков\n"
            "🎯 **Custom** — произвольный состав"
        ),
    },
    "en": {
        "status_open": "Open",
        "status_closed": "Closed",
        "party_roster": "PARTY ROSTER",
        "party_progress": "📊 **Party Progress**",
        "voice_channel": "🔊 Voice Channel",
        "note": "📝 Note",
        "start_time": "📅 Start time",
        "footer_open": "Open",
        "footer_closed": "Closed",
        "created_by": "created by",
        "saturday": "📅 Saturday",
        "sunday": "📅 Sunday",
        "reserve": "🔁 Reserve",
        "main_label": "Main",
        "full": "FULL",
        "day_sat": "Saturday",
        "day_sun": "Sunday",
        "day_both": "Both Days",
        "lang_button": "RU",
        "registered_msg": "✅ Registered: **{day}** → {slot}",
        "updated_msg": "✅ Updated: **{day}** ({slot})",
        "slot_reserve": "**Reserve** (main is full)",
        "slot_main": "**Main**",
        "slot_reserve_short": "Reserve",
        "slot_main_short": "Main",
        "register_title_new": "{remaining} Main slot(s) remaining. Select your day:",
        "register_title_full": "⚠️ Main roster is full ({current}/{limit}). You will be added to **Reserve**.\n\nSelect your day:",
        "register_title_update": "Current registration: **{day}** {slot}\n\nPick a new day to update:",
        "select_day_placeholder": "Select day(s)...",
        "confirm_button": "Confirm",
        "cancel_button": "Cancel",
        "lfg_card_title": "🎮 Looking For Group",
        "lfg_card_desc": (
            "Click a button below to create a party:\n\n"
            "⚔️ **Hero Realm** — 10 players\n"
            "🗡️ **Sword Trial** — 5 players\n"
            "🎯 **Custom** — custom composition"
        ),
    },
}


def _t(lang: str, key: str) -> str:
    return _I18N.get(lang, _I18N["ru"]).get(key, _I18N["en"].get(key, key))


# ---------------- Auto-dismiss helper ----------------


async def _auto_dismiss(interaction: discord.Interaction, delay: float = 3.0) -> None:
    """Delete the original ephemeral response after `delay` seconds.
    Use only when the original response is a direct send_message or edit_message
    (not a defer+followup from a modal submit, which must use _auto_dismiss_followup)."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.HTTPException):
        pass


async def _auto_dismiss_followup(
    followup: discord.Webhook, msg_id: int, delay: float = 3.0
) -> None:
    """Delete a specific followup message by ID after `delay` seconds.
    Use for modal submit flows where defer()+followup.send() was used,
    to avoid accidentally deleting the component's source message via @original."""
    await asyncio.sleep(delay)
    try:
        await followup.delete_message(msg_id)
    except (discord.NotFound, discord.HTTPException):
        pass


ROLE_META = {
    "tank": {"label": "Tank", "emoji": "🛡️", "style": discord.ButtonStyle.primary},
    "healer": {"label": "Healer", "emoji": "💚", "style": discord.ButtonStyle.success},
    "dps": {"label": "DPS", "emoji": "⚔️", "style": discord.ButtonStyle.danger},
    "member": {"label": "Member", "emoji": "👤", "style": discord.ButtonStyle.secondary},
}
ROLE_ORDER = ["tank", "healer", "dps"]

# Guild-war mode: sort order by role (DPS → Tank → Healer → generic Member)
_GW_ROLE_SORT: dict[str, int] = {"dps": 0, "tank": 1, "healer": 2, "member": 3}

DAY_LABELS = {"sat": "Saturday", "sun": "Sunday", "both": "Both Days"}


def _day_label(day_key: str, lang: str) -> str:
    mapping = {"sat": "day_sat", "sun": "day_sun", "both": "day_both"}
    i18n_key = mapping.get(day_key, day_key)
    return _t(lang, i18n_key)


# ---------------- Guild-war embed ----------------


def _gw_member_sort_key(m: LFGMember, guild: discord.Guild) -> tuple[int, str]:
    member = guild.get_member(m.user_id)
    name = member.display_name.lower() if member else ""
    return (_GW_ROLE_SORT.get(m.role, 9), name)


def _gw_build_day_column(
    members: list[LFGMember],
    day_key: str,
    guild: discord.Guild,
    *,
    show_role: bool = True,
) -> str:
    """Build a newline-joined column of `{idx}. {name} [- {role}]` lines
    filtered to members who voted for `day_key` or "both"."""
    rows = sorted(
        [m for m in members if m.day in (day_key, "both")],
        key=lambda m: _gw_member_sort_key(m, guild),
    )
    if not rows:
        return "*(empty)*"
    lines: list[str] = []
    for i, m in enumerate(rows, 1):
        disc = guild.get_member(m.user_id)
        name = disc.display_name if disc else f"User {m.user_id}"
        role_label = ROLE_META.get(m.role, ROLE_META["member"])["label"]
        if show_role:
            lines.append(f"`{i:>2}.` {name} — **{role_label}**")
        else:
            lines.append(f"`{i:>2}.` {name}")
    return "\n".join(lines)


async def build_gw_embed(
    bot: "AkimBot",
    party: LFGParty,
    guild: discord.Guild,
) -> discord.Embed:
    """Embed for guild-war day-voting mode (main_limit is not None)."""
    lang = getattr(party, "language", "ru") or "ru"
    lock_icon = "🔒" if not party.is_open else "🔓"
    status_text = _t(lang, "status_closed") if not party.is_open else _t(lang, "status_open")
    title = f"{lock_icon} {party.mode} — {status_text}"

    main_members = sorted(
        [m for m in party.members if not m.is_reserve],
        key=lambda m: _gw_member_sort_key(m, guild),
    )
    reserve_members = sorted(
        [m for m in party.members if m.is_reserve],
        key=lambda m: _gw_member_sort_key(m, guild),
    )

    filled = len(main_members)
    limit = party.main_limit or 0
    remaining = max(0, limit - filled)

    pct = int((filled / limit) * 100) if limit else 0
    blocks = int(round(pct / 10))
    bar = "█" * blocks + "░" * (10 - blocks)

    main_label = _t(lang, "main_label")
    reserve_label = _t(lang, "reserve")
    full_label = _t(lang, "full")

    color = discord.Color.dark_gray() if not party.is_open else discord.Color.gold()
    embed = discord.Embed(title=title, color=color)

    desc_parts: list[str] = []
    if party.start_time:
        parts = party.start_time.split(" ", 1)
        if len(parts) == 2:
            desc_parts.append(f"{_t(lang, 'start_time')}: **{parts[0]}** ⏰ **{parts[1]}**")
        else:
            desc_parts.append(f"{_t(lang, 'start_time')}: **{party.start_time}**")

    bar_line = (
        f"`{bar}` **{pct}%** • {filled}/{limit} {main_label}"
        + (f" • {reserve_label} {len(reserve_members)}" if reserve_members else "")
        + (f" • 🟢 {remaining}" if party.is_open and remaining > 0 else "")
        + (f" • **{full_label}**" if party.is_open and remaining == 0 else "")
    )
    desc_parts.append(bar_line)
    embed.description = "\n".join(desc_parts)

    # Day columns — Discord renders two consecutive inline fields side-by-side
    sat_col = _gw_build_day_column(main_members, "sat", guild, show_role=False)
    sun_col = _gw_build_day_column(main_members, "sun", guild, show_role=False)

    embed.add_field(name=_t(lang, "saturday"), value=sat_col[:1020], inline=True)
    embed.add_field(name=_t(lang, "sunday"), value=sun_col[:1020], inline=True)

    if reserve_members:
        res_lines: list[str] = []
        for i, m in enumerate(reserve_members, 1):
            disc = guild.get_member(m.user_id)
            name = disc.display_name if disc else f"User {m.user_id}"
            res_lines.append(f"`{i:>2}.` {name}")
        embed.add_field(
            name=f"{reserve_label} ({len(reserve_members)})",
            value="\n".join(res_lines)[:1020],
            inline=False,
        )

    if party.voice_channel_id is not None:
        vc = guild.get_channel(party.voice_channel_id)
        embed.add_field(
            name=_t(lang, "voice_channel"),
            value=vc.mention if vc else f"<#{party.voice_channel_id}>",
            inline=False,
        )
    if party.note:
        embed.add_field(name=_t(lang, "note"), value=party.note, inline=False)

    creator = guild.get_member(party.creator_id)
    creator_name = creator.display_name if creator else f"User {party.creator_id}"
    ts = (party.closed_at or party.created_at).astimezone(timezone.utc)
    footer_status = _t(lang, "footer_closed") if not party.is_open else _t(lang, "footer_open")
    embed.set_footer(
        text=(
            f"{footer_status} • "
            f"{_t(lang, 'created_by')} {creator_name} • "
            f"{ts.strftime('%d/%m/%Y %H:%M UTC')}"
        )
    )
    return embed


# ---------------- Embed builder ----------------


def _fmt_member_line(idx: int, member: LFGMember, player: Player | None, mention: str) -> str:
    role_tag = f"**`{ROLE_META[member.role]['label'].upper()}`**"
    line_body = f"{role_tag} {mention}"
    return f"`{idx:>2}.` {line_body}"


async def build_lfg_embed(
    bot: "AkimBot",
    party: LFGParty,
    guild: discord.Guild,
) -> discord.Embed:
    if party.main_limit is not None:
        return await build_gw_embed(bot, party, guild)

    lang = getattr(party, "language", "ru") or "ru"
    lock_icon = "🔒" if not party.is_open else "🔓"
    status_text = _t(lang, "status_closed") if not party.is_open else _t(lang, "status_open")
    title = f"{lock_icon} {party.mode} - {status_text}"

    # Load players for members in one query
    user_ids = [m.user_id for m in party.members]
    players_by_id: dict[int, Player] = {}
    if user_ids:
        async with bot.db.session() as s:
            rows = await s.execute(
                select(Player)
                .where(Player.guild_id == party.guild_id, Player.discord_id.in_(user_ids))
            )
            for p in rows.scalars().all():
                players_by_id[p.discord_id] = p

    lines: list[str] = []
    if party.start_time:
        st_parts = party.start_time.split(" ", 1)
        if len(st_parts) == 2:
            lines.append(f"{_t(lang, 'start_time')}: **{st_parts[0]}** ⏰ **{st_parts[1]}**")
        else:
            lines.append(f"{_t(lang, 'start_time')}: **{party.start_time}**")
        lines.append("")
    sep = "═" * 14
    lines.append(f"`{sep} {_t(lang, 'party_roster')} {sep}`")

    sorted_members = sorted(party.members, key=lambda m: m.joined_at)
    for i, m in enumerate(sorted_members, start=1):
        member = guild.get_member(m.user_id)
        mention = member.mention if member else f"<@{m.user_id}>"
        lines.append(_fmt_member_line(i, m, players_by_id.get(m.user_id), mention))

    for i in range(len(sorted_members) + 1, party.total_slots + 1):
        lines.append(f"`{i:>2}.` `EMPTY SLOT`")

    # Progress bar
    filled = len(sorted_members)
    total = party.total_slots
    pct = int((filled / total) * 100) if total else 0
    blocks_filled = int(round(pct / 10))
    bar = "█" * blocks_filled + "░" * (10 - blocks_filled)
    lines.append("")
    lines.append(_t(lang, "party_progress"))
    lines.append(f"`{bar}` **{pct}%** • {filled}/{total}")

    # Role breakdown
    by_role = {r: 0 for r in ROLE_ORDER}
    for m in party.members:
        by_role[m.role] = by_role.get(m.role, 0) + 1
    role_summary = "  ".join(
        f"{ROLE_META[r]['emoji']} {by_role[r]}/{party.role_capacity(r)}"
        for r in ROLE_ORDER
        if party.role_capacity(r) > 0
    )
    if role_summary:
        lines.append(role_summary)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=discord.Color.dark_gray() if not party.is_open else discord.Color.gold(),
    )
    if party.voice_channel_id is not None:
        vc = guild.get_channel(party.voice_channel_id)
        embed.add_field(
            name=_t(lang, "voice_channel"),
            value=(vc.mention if vc else f"<#{party.voice_channel_id}> (deleted?)"),
            inline=False,
        )

    if party.note:
        embed.add_field(name=_t(lang, "note"), value=party.note, inline=False)

    creator = guild.get_member(party.creator_id)
    creator_name = creator.display_name if creator else f"User {party.creator_id}"
    ts = (party.closed_at or party.created_at).astimezone(timezone.utc)
    footer_status = _t(lang, "footer_closed") if not party.is_open else _t(lang, "footer_open")
    footer = (
        f"{footer_status} • "
        f"{_t(lang, 'created_by')} {creator_name} • "
        f"{ts.strftime('%d/%m/%Y %H:%M UTC')}"
    )
    embed.set_footer(text=footer)
    return embed


# ---------------- Dynamic persistent buttons ----------------


class LFGJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:join:(?P<party_id>\d+):(?P<role>dps|healer|tank)",
):
    def __init__(self, party_id: int, role: str, *, disabled: bool = False) -> None:
        self.party_id = party_id
        self.role = role
        meta = ROLE_META[role]
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:join:{party_id}:{role}",
                label=meta["label"],
                emoji=meta["emoji"],
                style=meta["style"],
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGJoinButton":
        return cls(int(match["party_id"]), match["role"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_role_toggle(bot, interaction, self.party_id, self.role)


class LFGEndButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:end:(?P<party_id>\d+)",
):
    def __init__(self, party_id: int, *, disabled: bool = False) -> None:
        self.party_id = party_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:end:{party_id}",
                label="End LFG",
                emoji="🛑",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGEndButton":
        return cls(int(match["party_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_end(bot, interaction, self.party_id)


class LFGLeaveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:leave:(?P<party_id>\d+)",
):
    def __init__(self, party_id: int, *, disabled: bool = False) -> None:
        self.party_id = party_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:leave:{party_id}",
                label="Leave",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGLeaveButton":
        return cls(int(match["party_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_lfg_leave(bot, interaction, self.party_id)


class LFGStartButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:start:(?P<party_id>\d+)",
):
    def __init__(self, party_id: int, *, disabled: bool = False) -> None:
        self.party_id = party_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:start:{party_id}",
                label="Start Match",
                emoji="🎬",
                style=discord.ButtonStyle.success,
                row=1,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGStartButton":
        return cls(int(match["party_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_start(bot, interaction, self.party_id)


def _voice_jump_url(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


# ---------------- Guild-war buttons ----------------


class GuildWarRegisterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:gw:register:(?P<party_id>\d+)",
):
    def __init__(self, party_id: int, *, disabled: bool = False) -> None:
        self.party_id = party_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:gw:register:{party_id}",
                label="Register",
                emoji="⚔️",
                style=discord.ButtonStyle.success,
                row=0,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "GuildWarRegisterButton":
        return cls(int(match["party_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_gw_register_open(bot, interaction, self.party_id)


class GuildWarLeaveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:gw:leave:(?P<party_id>\d+)",
):
    def __init__(self, party_id: int, *, disabled: bool = False) -> None:
        self.party_id = party_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:gw:leave:{party_id}",
                label="Leave",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                row=0,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "GuildWarLeaveButton":
        return cls(int(match["party_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await _handle_gw_leave(bot, interaction, self.party_id)


class GuildWarCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:gw:card:(?P<guild_id>\d+)",
):
    """Persistent button on the Guild War Card — opens a modal to create a GW party."""

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:gw:card:{guild_id}",
                label="Create Guild War",
                emoji="⚔️",
                style=discord.ButtonStyle.danger,
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "GuildWarCardButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        # Permission check: manage_guild or bot owner/super-user
        is_owner = (
            interaction.guild is not None
            and bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        )
        is_manager = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_owner or is_manager):
            await interaction.response.send_message(
                "❌ You need **Manage Server** permission to create a Guild War.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(GuildWarCardModal(bot))


class GuildWarCardModal(discord.ui.Modal, title="Create Guild War"):
    """Modal for creating a guild-war LFG party from the GW card."""

    main_limit_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Main Roster Limit",
        placeholder="e.g. 30",
        default="30",
        min_length=1,
        max_length=4,
        required=True,
    )
    start_time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Start time / Начало (optional)",
        placeholder="DD/MM HH:MM or DDMMHHMM",
        required=False,
        max_length=11,
        style=discord.TextStyle.short,
    )
    note_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="Any notes for this Guild War",
        required=False,
        max_length=256,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: "AkimBot") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        assert interaction.guild is not None

        raw_limit = self.main_limit_input.value.strip()
        note_val = self.note_input.value.strip() or None
        start_time_val = _parse_start_time(self.start_time_input.value)

        if not raw_limit.isdigit() or int(raw_limit) < 1:
            await interaction.response.send_message(
                "❌ Main Roster Limit must be a positive number.", ephemeral=True
            )
            return

        main_limit = int(raw_limit)
        await interaction.response.defer(ephemeral=True)

        lfg_cog = self._bot.cogs.get("LFG")
        if lfg_cog is None:
            await interaction.followup.send("❌ LFG system is not available.", ephemeral=True)
            return

        party = await lfg_cog._spawn_party(  # type: ignore[union-attr]
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode="Guild War",
            dps=0,
            healer=0,
            tank=0,
            note=note_val,
            voice_channel_id=None,
            main_limit=main_limit,
            gw_role_selection=False,
            start_time=start_time_val,
        )

        if party is None:
            await interaction.followup.send(
                "❌ Failed to create Guild War party. Make sure `/setup-lfg` is configured.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Guild War party created! (main limit: **{main_limit}**)",
            ephemeral=True,
        )


# ---------------- Language toggle button (per-party) ----------------


class LFGLangToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:lang:(?P<party_id>\d+)",
):
    """Toggles the party embed language between RU and EN."""

    def __init__(self, party_id: int, lang: str = "ru") -> None:
        self.party_id = party_id
        self._lang = lang
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:lang:{party_id}",
                label=_t(lang, "lang_button"),
                emoji="🌐",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGLangToggleButton":
        party_id = int(match["party_id"])
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        async with bot.db.session() as s:
            party = await s.get(LFGParty, party_id)
            lang = (party.language if party else None) or "ru"
        return cls(party_id, lang)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        async with bot.db.session() as s:
            party = await s.get(LFGParty, self.party_id)
            if party is None:
                await interaction.response.send_message("Party not found.", ephemeral=True)
                return
            party.language = "en" if (party.language or "ru") == "ru" else "ru"
            await s.commit()
        if interaction.guild is not None:
            await _refresh_party_message_direct(bot, self.party_id, interaction.guild)
        await interaction.response.defer()


# ---------------- LFG Card buttons (Hero Realm, Sword Trial, Custom) ----------------


class LFGCardHeroRealmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:card:hr:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:card:hr:{guild_id}",
                label="Hero Realm",
                emoji="⚔️",
                style=discord.ButtonStyle.primary,
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGCardHeroRealmButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.send_modal(LFGCardHeroRealmModal(bot))


class LFGCardSwordTrialButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:card:st:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:card:st:{guild_id}",
                label="Sword Trial",
                emoji="🗡️",
                style=discord.ButtonStyle.success,
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGCardSwordTrialButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.send_modal(LFGCardSwordTrialModal(bot))


class LFGCardCustomButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"lfg:card:cu:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"lfg:card:cu:{guild_id}",
                label="Custom",
                emoji="🎯",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "LFGCardCustomButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "AkimBot" = interaction.client  # type: ignore[assignment]
        await interaction.response.send_modal(LFGCardCustomModal(bot))


# ---------------- LFG Card modals ----------------


class LFGCardHeroRealmModal(discord.ui.Modal, title="Hero Realm — Create Party"):
    dps_input: discord.ui.TextInput = discord.ui.TextInput(
        label="DPS / DPS слоты",
        placeholder="e.g. 6",
        default="6",
        min_length=1,
        max_length=2,
        required=True,
    )
    healer_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Healer / Хилер слоты",
        placeholder="e.g. 2",
        default="2",
        min_length=1,
        max_length=2,
        required=True,
    )
    tank_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Tank / Танк слоты",
        placeholder="e.g. 2",
        default="2",
        min_length=1,
        max_length=2,
        required=True,
    )
    start_time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Start time / Начало (optional)",
        placeholder="DD/MM HH:MM or DDMMHHMM",
        required=False,
        max_length=11,
        style=discord.TextStyle.short,
    )
    note_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Note / Заметка (optional)",
        required=False,
        max_length=256,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: "AkimBot") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        dps, healer, tank, err = _parse_slots(
            self.dps_input.value, self.healer_input.value, self.tank_input.value
        )
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        note_val = self.note_input.value.strip() or None
        start_time_val = _parse_start_time(self.start_time_input.value)
        await interaction.response.defer(ephemeral=True)
        lfg_cog = self._bot.cogs.get("LFG")
        if lfg_cog is None:
            await interaction.followup.send("❌ LFG system is not available.", ephemeral=True)
            return
        party = await lfg_cog._spawn_party(  # type: ignore[union-attr]
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode="Hero Realm",
            dps=dps, healer=healer, tank=tank,
            note=note_val,
            voice_channel_id=None,
            lfg_type="hero_realm",
            start_time=start_time_val,
        )
        if party is None:
            await interaction.followup.send(
                "❌ Failed to create party. Make sure `/setup-lfg` is configured.", ephemeral=True
            )
            return
        msg = await interaction.followup.send(
            f"✅ Hero Realm party created! (<#{party.channel_id}>)", ephemeral=True, wait=True
        )
        asyncio.create_task(_auto_dismiss_followup(interaction.followup, msg.id))


class LFGCardSwordTrialModal(discord.ui.Modal, title="Sword Trial — Create Party"):
    dps_input: discord.ui.TextInput = discord.ui.TextInput(
        label="DPS / DPS слоты",
        placeholder="e.g. 3",
        default="3",
        min_length=1,
        max_length=2,
        required=True,
    )
    healer_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Healer / Хилер слоты",
        placeholder="e.g. 1",
        default="1",
        min_length=1,
        max_length=2,
        required=True,
    )
    tank_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Tank / Танк слоты",
        placeholder="e.g. 1",
        default="1",
        min_length=1,
        max_length=2,
        required=True,
    )
    start_time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Start time / Начало (optional)",
        placeholder="DD/MM HH:MM or DDMMHHMM",
        required=False,
        max_length=11,
        style=discord.TextStyle.short,
    )
    note_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Note / Заметка (optional)",
        required=False,
        max_length=256,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: "AkimBot") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        dps, healer, tank, err = _parse_slots(
            self.dps_input.value, self.healer_input.value, self.tank_input.value
        )
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        note_val = self.note_input.value.strip() or None
        start_time_val = _parse_start_time(self.start_time_input.value)
        await interaction.response.defer(ephemeral=True)
        lfg_cog = self._bot.cogs.get("LFG")
        if lfg_cog is None:
            await interaction.followup.send("❌ LFG system is not available.", ephemeral=True)
            return
        party = await lfg_cog._spawn_party(  # type: ignore[union-attr]
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode="Sword Trial",
            dps=dps, healer=healer, tank=tank,
            note=note_val,
            voice_channel_id=None,
            lfg_type="sword_trial",
            start_time=start_time_val,
        )
        if party is None:
            await interaction.followup.send(
                "❌ Failed to create party. Make sure `/setup-lfg` is configured.", ephemeral=True
            )
            return
        msg = await interaction.followup.send(
            f"✅ Sword Trial party created! (<#{party.channel_id}>)", ephemeral=True, wait=True
        )
        asyncio.create_task(_auto_dismiss_followup(interaction.followup, msg.id))


class LFGCardCustomModal(discord.ui.Modal, title="Custom LFG — Create Party"):
    mode_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Mode name / Название режима",
        placeholder="e.g. Weekly Boss, PvP 3v3...",
        min_length=1,
        max_length=32,
        required=True,
    )
    dps_input: discord.ui.TextInput = discord.ui.TextInput(
        label="DPS слоты",
        placeholder="e.g. 3",
        default="0",
        min_length=1,
        max_length=2,
        required=True,
    )
    healer_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Healer слоты",
        placeholder="e.g. 1",
        default="0",
        min_length=1,
        max_length=2,
        required=True,
    )
    tank_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Tank слоты",
        placeholder="e.g. 1",
        default="0",
        min_length=1,
        max_length=2,
        required=True,
    )
    start_time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Start time / Начало (optional)",
        placeholder="DD/MM HH:MM or DDMMHHMM",
        required=False,
        max_length=11,
        style=discord.TextStyle.short,
    )

    def __init__(self, bot: "AkimBot") -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        dps, healer, tank, err = _parse_slots(
            self.dps_input.value, self.healer_input.value, self.tank_input.value
        )
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        mode_name = self.mode_input.value.strip()
        start_time_val = _parse_start_time(self.start_time_input.value)
        await interaction.response.defer(ephemeral=True)
        lfg_cog = self._bot.cogs.get("LFG")
        if lfg_cog is None:
            await interaction.followup.send("❌ LFG system is not available.", ephemeral=True)
            return
        party = await lfg_cog._spawn_party(  # type: ignore[union-attr]
            guild_id=interaction.guild_id,
            creator_id=interaction.user.id,
            mode=mode_name,
            dps=dps, healer=healer, tank=tank,
            note=None,
            voice_channel_id=None,
            lfg_type="custom",
            start_time=start_time_val,
        )
        if party is None:
            await interaction.followup.send(
                "❌ Failed to create party. Make sure `/setup-lfg` is configured.", ephemeral=True
            )
            return
        msg = await interaction.followup.send(
            f"✅ Party **{mode_name}** created! (<#{party.channel_id}>)", ephemeral=True, wait=True
        )
        asyncio.create_task(_auto_dismiss_followup(interaction.followup, msg.id))


def _parse_start_time(raw: str) -> str | None:
    """Accept 'DDMMHHMM' (8 digits) or 'DD/MM HH:MM' / 'DD-MM HH:MM'. Returns 'DD/MM HH:MM'."""
    s = raw.strip()
    if not s:
        return None
    if re.fullmatch(r"\d{8}", s):
        return f"{s[0:2]}/{s[2:4]} {s[4:6]}:{s[6:8]}"
    m = re.fullmatch(r"(\d{2})[-/](\d{2})\s+(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}/{m.group(2)} {m.group(3)}:{m.group(4)}"
    return s


def _parse_slots(
    dps_raw: str, healer_raw: str, tank_raw: str
) -> tuple[int, int, int, str | None]:
    try:
        dps = int(dps_raw.strip())
        healer = int(healer_raw.strip())
        tank = int(tank_raw.strip())
    except ValueError:
        return 0, 0, 0, "❌ Slot counts must be numbers."
    if dps < 0 or healer < 0 or tank < 0:
        return 0, 0, 0, "❌ Slot counts must be 0 or more."
    if dps + healer + tank < 1:
        return 0, 0, 0, "❌ Party must have at least 1 slot."
    return dps, healer, tank, None


class _GuildWarRegisterView(discord.ui.View):
    """Ephemeral role + day picker shown when user clicks Register."""

    def __init__(
        self,
        bot: "AkimBot",
        party_id: int,
        user_id: int,
        existing: LFGMember | None,
        lang: str = "ru",
    ) -> None:
        super().__init__(timeout=120)
        self._bot = bot
        self.party_id = party_id
        self.user_id = user_id
        self._lang = lang
        self.chosen_role = "member"
        self.chosen_day: str | None = existing.day if existing else None

        day_opts = [
            discord.SelectOption(
                label=_t(lang, "day_sat"), value="sat", emoji="📅",
                default=(existing is not None and existing.day == "sat"),
            ),
            discord.SelectOption(
                label=_t(lang, "day_sun"), value="sun", emoji="📅",
                default=(existing is not None and existing.day == "sun"),
            ),
            discord.SelectOption(
                label=_t(lang, "day_both"), value="both", emoji="🗓️",
                default=(existing is not None and existing.day == "both"),
            ),
        ]
        self._day_select = discord.ui.Select(
            placeholder=_t(lang, "select_day_placeholder"),
            options=day_opts,
            row=0,
        )
        self._day_select.callback = self._on_day
        self.add_item(self._day_select)

        self._confirm_btn = discord.ui.Button(
            label=_t(lang, "confirm_button"),
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=1,
            disabled=(self.chosen_day is None),
        )
        self._confirm_btn.callback = self._on_confirm
        self.add_item(self._confirm_btn)

        cancel_btn = discord.ui.Button(
            label=_t(lang, "cancel_button"),
            style=discord.ButtonStyle.secondary,
            emoji="❌",
            row=1,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    def _maybe_enable_confirm(self) -> None:
        self._confirm_btn.disabled = (self.chosen_day is None)

    async def _on_day(self, interaction: discord.Interaction) -> None:
        self.chosen_day = interaction.data["values"][0]  # type: ignore[index]
        self._maybe_enable_confirm()
        await interaction.response.edit_message(view=self)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        assert self.chosen_role is not None and self.chosen_day is not None
        await _handle_gw_commit(
            self._bot, interaction, self.party_id,
            interaction.user.id, self.chosen_role, self.chosen_day,
        )
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="❌ Cancelled.", view=None)
        self.stop()


def build_lfg_view(party: LFGParty, *, show_end_for: int | None = None) -> discord.ui.View:
    lang = getattr(party, "language", "ru") or "ru"
    view = discord.ui.View(timeout=None)

    if party.main_limit is not None:
        # Guild-war day-voting mode
        view.add_item(GuildWarRegisterButton(party.id, disabled=not party.is_open))
        view.add_item(GuildWarLeaveButton(party.id, disabled=not party.is_open))
        view.add_item(LFGLangToggleButton(party.id, lang))
        if party.voice_channel_id is not None:
            view.add_item(
                discord.ui.Button(
                    label="Join Voice",
                    emoji="🔊",
                    style=discord.ButtonStyle.link,
                    url=_voice_jump_url(party.guild_id, party.voice_channel_id),
                    row=1,
                )
            )
        if party.is_open:
            if party.voice_channel_id is not None:
                view.add_item(LFGStartButton(party.id))
            view.add_item(LFGEndButton(party.id))
        return view

    # Original slot-based mode
    counts = {r: 0 for r in ROLE_ORDER}
    for m in party.members:
        counts[m.role] = counts.get(m.role, 0) + 1

    for role in ROLE_ORDER:
        cap = party.role_capacity(role)
        if cap <= 0:
            continue
        full = counts[role] >= cap
        view.add_item(
            LFGJoinButton(party.id, role, disabled=(not party.is_open) or full)
        )

    # Row 1: control buttons (voice / start / end)
    if party.voice_channel_id is not None:
        view.add_item(
            discord.ui.Button(
                label="Join Voice",
                emoji="🔊",
                style=discord.ButtonStyle.link,
                url=_voice_jump_url(party.guild_id, party.voice_channel_id),
                row=1,
            )
        )

    if party.is_open:
        if party.voice_channel_id is not None:
            view.add_item(LFGStartButton(party.id))
        view.add_item(LFGEndButton(party.id))
        view.add_item(LFGLeaveButton(party.id))
    view.add_item(LFGLangToggleButton(party.id, lang))
    return view


# ---------------- Interaction handlers ----------------


async def _fetch_party(bot: "AkimBot", party_id: int) -> LFGParty | None:
    async with bot.db.session() as s:
        return await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )


async def _refresh_message(bot: "AkimBot", interaction: discord.Interaction, party_id: int) -> None:
    party = await _fetch_party(bot, party_id)
    if party is None or interaction.guild is None:
        return
    embed = await build_lfg_embed(bot, party, interaction.guild)
    view = build_lfg_view(party)
    if interaction.response.is_done():
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=view)  # type: ignore[union-attr]
    else:
        await interaction.response.edit_message(embed=embed, view=view)


async def _refresh_party_message_direct(
    bot: "AkimBot", party_id: int, guild: discord.Guild
) -> None:
    """Edit the party's Discord message directly (without an interaction).
    Used when the triggering interaction is ephemeral and we can't route
    the edit through it. Does a fresh DB fetch so ORM objects are never
    accessed outside an active session."""
    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None or party.message_id is None:
            return
        # Build embed and view while session is still open — avoids
        # DetachedInstanceError on the ORM object after session close.
        embed = await build_lfg_embed(bot, party, guild)
        view = build_lfg_view(party)
        channel_id = party.channel_id
        message_id = party.message_id

    channel = guild.get_channel_or_thread(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=embed, view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def _handle_lfg_leave(
    bot: "AkimBot",
    interaction: discord.Interaction,
    party_id: int,
) -> None:
    """Remove the user from a regular (slot-based) LFG party."""
    user_id = interaction.user.id
    guild = interaction.guild

    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.send_message(
                "This party no longer exists.", ephemeral=True
            )
            return
        if not party.is_open:
            await interaction.response.send_message(
                "This party is already closed.", ephemeral=True
            )
            return
        existing = next((m for m in party.members if m.user_id == user_id), None)
        if existing is None:
            await interaction.response.send_message(
                "You are not in this party.", ephemeral=True
            )
            return
        await s.delete(existing)
        await s.commit()

    await interaction.response.send_message("✅ You have left the party.", ephemeral=True)
    asyncio.create_task(_auto_dismiss(interaction))
    if guild is not None:
        await _refresh_party_message_direct(bot, party_id, guild)


async def _handle_role_toggle(
    bot: "AkimBot",
    interaction: discord.Interaction,
    party_id: int,
    role: str,
) -> None:
    user_id = interaction.user.id

    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.send_message(
                "This party no longer exists.", ephemeral=True
            )
            return
        if not party.is_open:
            await interaction.response.send_message(
                "This party is already closed.", ephemeral=True
            )
            return

        existing = next((m for m in party.members if m.user_id == user_id), None)

        if existing and existing.role == role:
            # Leave (toggle off)
            await s.delete(existing)
            await s.commit()
            await interaction.response.defer()
            await _refresh_message(bot, interaction, party_id)
            return

        if existing and existing.role != role:
            await interaction.response.send_message(
                f"You're already in the **{ROLE_META[existing.role]['label']}** slot. "
                f"Press the {ROLE_META[existing.role]['emoji']} {ROLE_META[existing.role]['label']} "
                f"button to leave, then pick a different one.",
                ephemeral=True,
            )
            return

        # Check capacity
        current_count = sum(1 for m in party.members if m.role == role)
        if current_count >= party.role_capacity(role):
            await interaction.response.send_message(
                f"The **{ROLE_META[role]['label']}** slot is full.", ephemeral=True
            )
            return

        s.add(LFGMember(party_id=party_id, user_id=user_id, role=role))
        await s.commit()

    await interaction.response.defer()
    await _refresh_message(bot, interaction, party_id)


async def _handle_end(bot: "AkimBot", interaction: discord.Interaction, party_id: int) -> None:
    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.send_message(
                "This party no longer exists.", ephemeral=True
            )
            return

        is_creator = party.creator_id == interaction.user.id
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )
        if not (is_creator or is_admin):
            await interaction.response.send_message(
                "Only the party creator (or an admin) can end this party.", ephemeral=True
            )
            return

        if not party.is_open:
            await interaction.response.send_message(
                "This party was already closed.", ephemeral=True
            )
            return

        party.is_open = False
        party.closed_at = datetime.now(timezone.utc)
        await s.commit()

    await interaction.response.defer()
    await _refresh_message(bot, interaction, party_id)


async def _handle_start(bot: "AkimBot", interaction: discord.Interaction, party_id: int) -> None:
    party = await _fetch_party(bot, party_id)
    if party is None:
        await interaction.response.send_message(
            "This party no longer exists.", ephemeral=True
        )
        return

    is_creator = party.creator_id == interaction.user.id
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )
    if not (is_creator or is_admin):
        await interaction.response.send_message(
            "Only the party creator (or an admin) can Start Match.", ephemeral=True
        )
        return

    if party.voice_channel_id is None or interaction.guild is None:
        await interaction.response.send_message(
            "This party has no voice channel configured.", ephemeral=True
        )
        return

    voice = interaction.guild.get_channel(party.voice_channel_id)
    if not isinstance(voice, (discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message(
            "The voice channel no longer exists or isn't a voice channel.",
            ephemeral=True,
        )
        return

    moved: list[str] = []
    already_there: list[str] = []
    not_in_voice: list[str] = []
    failed: list[str] = []

    me = interaction.guild.me
    owner_id = interaction.guild.owner_id

    for m in party.members:
        member = interaction.guild.get_member(m.user_id)
        if member is None:
            failed.append(f"<@{m.user_id}> (no longer in server)")
            continue
        vs = member.voice
        if vs is None or vs.channel is None:
            not_in_voice.append(member.mention)
            continue
        if vs.channel.id == voice.id:
            already_there.append(member.mention)
            continue

        # Pre-flight diagnostics so failure reasons are accurate.
        if member.id == owner_id:
            # Discord forbids bots from moving the server owner. There is
            # nothing the user can fix, so skip silently instead of dumping
            # a useless warning into the report.
            continue
        if me is not None and member.top_role >= me.top_role:
            failed.append(
                f"{member.mention} (bot role is below — move bot role higher)"
            )
            continue
        if me is not None and not voice.permissions_for(me).move_members:
            failed.append(
                f"{member.mention} (bot lacks Move Members on destination channel)"
            )
            continue
        src_perms = vs.channel.permissions_for(me) if me else None
        if src_perms is not None and not src_perms.move_members:
            failed.append(
                f"{member.mention} (bot lacks Move Members on source channel {vs.channel.mention})"
            )
            continue

        try:
            await member.move_to(voice, reason="Akim Bot Start Match")
            moved.append(member.mention)
        except discord.Forbidden as e:
            failed.append(f"{member.mention} (403 Forbidden: {e.text or 'no detail'})")
        except discord.HTTPException as e:
            failed.append(f"{member.mention} ({e.status}: {e.text or 'no detail'})")

    lines = [f"🎬 **Start Match** → {voice.mention}"]
    if moved:
        lines.append(f"✅ Moved: {', '.join(moved)}")
    if already_there:
        lines.append(f"🔊 Already there: {', '.join(already_there)}")
    if not_in_voice:
        lines.append(
            f"📵 Not in voice (please join manually): {', '.join(not_in_voice)}"
        )
    if failed:
        lines.append(f"⚠️ Failed: {', '.join(failed)}")
    if len(lines) == 1:
        lines.append("Party is empty — nobody to move.")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


# ---------------- Guild-war interaction handlers ----------------


async def _handle_gw_register_open(
    bot: "AkimBot",
    interaction: discord.Interaction,
    party_id: int,
) -> None:
    """Show the ephemeral role+day picker."""
    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.send_message(
                "This party no longer exists.", ephemeral=True
            )
            return
        if not party.is_open:
            await interaction.response.send_message(
                "This party is already closed.", ephemeral=True
            )
            return
        existing = next(
            (m for m in party.members if m.user_id == interaction.user.id), None
        )
        main_count = sum(1 for m in party.members if not m.is_reserve)
        limit = party.main_limit or 0
        lang = (getattr(party, "language", None) or "ru")

    if existing:
        day_lbl = _day_label(existing.day or "", lang)
        slot_tag = f"*({_t(lang, 'slot_reserve_short')})*" if existing.is_reserve else f"*({_t(lang, 'slot_main_short')})*"
        status_txt = _t(lang, "register_title_update").format(day=day_lbl, slot=slot_tag)
    else:
        if main_count >= limit:
            status_txt = _t(lang, "register_title_full").format(current=main_count, limit=limit)
        else:
            status_txt = _t(lang, "register_title_new").format(remaining=limit - main_count)

    view = _GuildWarRegisterView(bot, party_id, interaction.user.id, existing, lang)
    await interaction.response.send_message(
        status_txt, view=view, ephemeral=True
    )


async def _handle_gw_leave(
    bot: "AkimBot",
    interaction: discord.Interaction,
    party_id: int,
) -> None:
    """Remove user from party, promote first reserve to main if vacated a main slot."""
    user_id = interaction.user.id
    guild = interaction.guild
    gvg_match_id: int | None = None
    party_guild_id: int = 0
    promoted_user_id: int | None = None

    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.send_message(
                "This party no longer exists.", ephemeral=True
            )
            return
        if not party.is_open:
            await interaction.response.send_message(
                "This party is already closed.", ephemeral=True
            )
            return

        existing = next((m for m in party.members if m.user_id == user_id), None)
        if existing is None:
            await interaction.response.send_message(
                "You are not registered for this party.", ephemeral=True
            )
            return

        gvg_match_id = party.gvg_match_id
        party_guild_id = party.guild_id
        was_main = not existing.is_reserve
        await s.delete(existing)
        await s.flush()

        # Promote the earliest reserve member if vacated a main slot
        if was_main and party.main_limit is not None:
            first_reserve = next(
                (
                    m for m in sorted(party.members, key=lambda m: m.joined_at)
                    if m.user_id != user_id and m.is_reserve
                ),
                None,
            )
            if first_reserve is not None:
                first_reserve.is_reserve = False
                promoted_user_id = first_reserve.user_id
        await s.commit()

    await interaction.response.send_message("✅ You have left the party.", ephemeral=True)
    asyncio.create_task(_auto_dismiss(interaction))
    if guild is not None:
        await _refresh_party_message_direct(bot, party_id, guild)

    # Sync removal (and any promotion) to the linked GVG match
    if gvg_match_id is not None:
        gvg_cog = bot.cogs.get("GvG")
        if gvg_cog is not None:
            try:
                await gvg_cog.remove_gw_member(party_guild_id, gvg_match_id, user_id)  # type: ignore[union-attr]
                if promoted_user_id is not None:
                    await gvg_cog.sync_gw_member(  # type: ignore[union-attr]
                        party_guild_id, gvg_match_id, promoted_user_id, "MAIN"
                    )
            except Exception:
                import logging
                logging.getLogger("akim-bot.lfg").exception(
                    "lfg: GVG sync failed on leave for party %s", party_id
                )


async def _handle_gw_commit(
    bot: "AkimBot",
    interaction: discord.Interaction,
    party_id: int,
    user_id: int,
    role: str,
    day: str,
) -> None:
    """Persist the guild-war registration (insert or update)."""
    guild = interaction.guild
    confirm_content: str = ""
    gvg_match_id: int | None = None
    party_guild_id: int = 0
    new_roster_type: str = "MAIN"

    async with bot.db.session() as s:
        party = await s.scalar(
            select(LFGParty)
            .where(LFGParty.id == party_id)
            .options(selectinload(LFGParty.members))
        )
        if party is None:
            await interaction.response.edit_message(
                content="❌ This party no longer exists.", view=None
            )
            return
        if not party.is_open:
            await interaction.response.edit_message(
                content="❌ This party is already closed.", view=None
            )
            return

        gvg_match_id = party.gvg_match_id
        party_guild_id = party.guild_id
        lang = (getattr(party, "language", None) or "ru")
        existing = next((m for m in party.members if m.user_id == user_id), None)
        day_lbl = _day_label(day, lang)

        if existing:
            existing.role = role
            existing.day = day
            await s.commit()
            new_roster_type = "RESERVE" if existing.is_reserve else "MAIN"
            slot_txt = _t(lang, "slot_reserve_short") if existing.is_reserve else _t(lang, "slot_main_short")
            confirm_content = _t(lang, "updated_msg").format(day=day_lbl, slot=slot_txt)
        else:
            main_count = sum(1 for m in party.members if not m.is_reserve)
            limit = party.main_limit or 0
            is_reserve = main_count >= limit
            s.add(LFGMember(
                party_id=party_id,
                user_id=user_id,
                role=role,
                day=day,
                is_reserve=is_reserve,
            ))
            await s.commit()
            new_roster_type = "RESERVE" if is_reserve else "MAIN"
            slot_txt = _t(lang, "slot_reserve") if is_reserve else _t(lang, "slot_main")
            confirm_content = _t(lang, "registered_msg").format(day=day_lbl, slot=slot_txt)

    await interaction.response.edit_message(content=confirm_content, view=None)
    asyncio.create_task(_auto_dismiss(interaction))

    if guild is not None:
        await _refresh_party_message_direct(bot, party_id, guild)

    # Sync registration to the linked GVG match
    if gvg_match_id is not None:
        gvg_cog = bot.cogs.get("GvG")
        if gvg_cog is not None:
            try:
                await gvg_cog.sync_gw_member(  # type: ignore[union-attr]
                    party_guild_id, gvg_match_id, user_id, new_roster_type
                )
            except Exception:
                import logging
                logging.getLogger("akim-bot.lfg").exception(
                    "lfg: GVG sync failed on commit for party %s", party_id
                )
