from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Weapon(Base):
    __tablename__ = "weapons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Discord emoji string ("<:name:id>") or unicode emoji. Optional.
    icon: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # main | sub | both — controls which dropdown the weapon appears in
    slot: Mapped[str] = mapped_column(String(8), default="both", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        UniqueConstraint("guild_id", "discord_id", name="uq_player_guild_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    ingame_name: Mapped[str] = mapped_column(String(64), nullable=False)
    ingame_uid: Mapped[str] = mapped_column(String(32), nullable=False)
    luc_chien: Mapped[int] = mapped_column(Integer, nullable=False)
    hang_dau_truong: Mapped[str | None] = mapped_column(String(64), nullable=True)

    main_weapon_id: Mapped[int | None] = mapped_column(
        ForeignKey("weapons.id", ondelete="SET NULL"), nullable=True
    )
    sub_weapon_id: Mapped[int | None] = mapped_column(
        ForeignKey("weapons.id", ondelete="SET NULL"), nullable=True
    )
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True
    )

    # User self-reported answer to "Are you a member of Guild <name>?"
    is_guild_member: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 'approved' | 'pending' | 'denied'. Default approved keeps the previous
    # auto-approve behavior for users who don't have the Unverified role.
    approval_status: Mapped[str] = mapped_column(
        String(16), default="approved", nullable=False
    )
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Pointer to the pending-approval message so /baodanh pending and the
    # buttons can find / update the card after the fact.
    pending_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pending_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Fields synced from external sources (WWM Workshop bot, etc.). luc_chien
    # double-duties as Build Power; level/elegance are display-only stats.
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elegance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    main_weapon: Mapped[Weapon | None] = relationship(foreign_keys=[main_weapon_id], lazy="joined")
    sub_weapon: Mapped[Weapon | None] = relationship(foreign_keys=[sub_weapon_id], lazy="joined")
    server: Mapped["Server | None"] = relationship(foreign_keys=[server_id], lazy="joined")


class GuildConfig(Base):
    __tablename__ = "guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # CSV of role IDs allowed to use /lfg. NULL/empty = everyone allowed.
    lfg_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lfg_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # /baodanh approval flow config
    unverified_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approval_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approver_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # When TRUE (default, matching legacy behavior), /baodanh save posts a
    # pending card to approval_channel_id and waits for a human to click
    # approve. When FALSE, save is auto-approved on the spot and the
    # role in baodanh_auto_role_id is granted immediately (no pending
    # card, no admin ping). Nullable so pre-existing servers keep the
    # legacy require-approval flow.
    baodanh_require_approval: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    baodanh_auto_role_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Auto-rename member nickname to "<In-game Name> - <ServerIcon>ServerName"
    # after /player-reg save (and on admin approve). NULL/TRUE = enabled
    # (legacy behavior); FALSE = never touch the nickname.
    auto_rename_enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    # Displayed in the "Are you a member of Guild <X>?" question on /baodanh.
    guild_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Persistent "press to register" card — channel where the card lives
    # and the message id of the card itself so /setup-baodanh-card can
    # reposition or refresh without leaving orphans. Clicking the
    # card's button opens the same UidModal as /baodanh moi.
    baodanh_card_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baodanh_card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Persistent Guild War Card — channel + message of the "Create Guild War" card.
    gw_card_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gw_card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Persistent /player help card posted via /setup-player-card.
    player_card_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    player_card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Optional "Link UID" slash-command mention rendered inside the
    # registration card. Discord's `</name:id>` mention requires the
    # *application command id* (not the bot/app id) — the bot_id field
    # is kept for owner reference / labelling only. Owner sets these
    # via /setup-baodanh-link; if command_id is NULL the link line is
    # omitted from the embed.
    baodanh_link_bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baodanh_link_command_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    baodanh_link_command_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Discord user ID of the bot whose embeds /sync should parse (e.g. the
    # WWM Workshop bot). NULL = /sync disabled.
    sync_source_bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Default channel for /quiz start. NULL = user must pass channel: each time.
    quiz_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Channel to post quiz winner announcements (Vinh danh). NULL = skip.
    quiz_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Role auto-granted when a user accumulates quiz_wins_for_role wins.
    quiz_winner_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quiz_wins_for_role: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # /sohoc (number-guessing) config
    sohoc_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sohoc_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sohoc_creator_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sohoc_winner_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Legacy — kept for additive-only migration policy; no longer read.
    sohoc_default_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Wall-clock time of day (HH:MM, 24h) when the join-phase ends. The
    # interpretation timezone is sohoc_tz_offset below. Past → rolls to
    # tomorrow. NULL → must pass on /sohoc start.
    sohoc_end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Integer UTC offset in hours (-12..14) used when interpreting
    # sohoc_end_time. NULL → falls back to +7 (Vietnam) for backwards compat.
    sohoc_tz_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When True, the bot spawns a fresh game session in sohoc_channel_id
    # right after the current one finishes (either via manual Roll or
    # the auto-roll grace timer). Owner runs `/sohoc start` once; from
    # then on the loop is self-sustaining. The new game inherits the
    # previous game's creator so /sohoc admin buttons keep working.
    sohoc_autostart: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    sohoc_default_language: Mapped[str | None] = mapped_column(
        String(4), nullable=True,
    )

    # /quaythuong (Give Away) — channel where game cards are posted.
    # NULL → /quaythuong start refuses until owner runs setup.
    giveaway_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # /caro (Caro 12x12) config
    caro_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caro_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caro_winner_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caro_wins_for_role: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # /oantuti (rock-paper-scissors 1v1) config — icons + creator roles +
    # default game-post channel.
    oantuti_icon_bua: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oantuti_icon_bao: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oantuti_icon_keo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oantuti_creator_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oantuti_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Vinh danh — bot posts a winner card here after each free-play match.
    oantuti_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # /tournament config — bracket channel + match arena + vinh danh.
    tournament_bracket_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tournament_match_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tournament_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # /vuatiengviet (Vietnamese word-scramble) config
    vtv_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vtv_announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vtv_creator_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)
    vtv_winner_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vtv_wins_for_role: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Persistent LFG Card (Hero Realm / Sword Trial / Custom buttons)
    lfg_card_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lfg_card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Per-type LFG post channels. If NULL, falls back to lfg_channel_id.
    lfg_hero_realm_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lfg_sword_trial_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lfg_custom_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # NOTE: /gvg config was moved to a dedicated GvgGuildSettings table
    # as part of the SPEC rewrite; the old gvg_* columns on this row are
    # deprecated but left in the DB via ALTER-DROP for a fresh start.

    def sohoc_creator_ids(self) -> list[int]:
        return self._split_csv(self.sohoc_creator_role_ids)

    def oantuti_creator_ids(self) -> list[int]:
        return self._split_csv(self.oantuti_creator_role_ids)

    def vtv_creator_ids(self) -> list[int]:
        return self._split_csv(self.vtv_creator_role_ids)

    @staticmethod
    def _split_csv(csv: str | None) -> list[int]:
        if not csv:
            return []
        out: list[int] = []
        for chunk in csv.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.append(int(chunk))
        return out

    def allowed_role_ids(self) -> list[int]:
        return self._split_csv(self.lfg_role_ids)

    def approver_ids(self) -> list[int]:
        return self._split_csv(self.approver_role_ids)


class Server(Base):
    """In-game server / region that a Player belongs to (e.g. EU, NA, JP).

    Per-Discord-guild because different communities run different in-game
    regions. The icon is a unicode or custom emoji string that gets appended
    to the user's nickname (e.g. `AKim - 🇪🇺EU`).
    """

    __tablename__ = "servers"
    __table_args__ = (UniqueConstraint("guild_id", "name", name="uq_server_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class GuildMemberUid(Base):
    """Whitelist of UIDs known to belong to the server's in-game Guild."""

    __tablename__ = "guild_member_uids"
    __table_args__ = (
        UniqueConstraint("guild_id", "uid", name="uq_guild_member_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    uid: Mapped[str] = mapped_column(String(32), nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LFGParty(Base):
    __tablename__ = "lfg_parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    mode: Mapped[str] = mapped_column(String(32), nullable=False)  # display label
    dps_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    healer_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tank_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Guild-war day-voting model: when set, total main roster cap.
    # dps/healer/tank slots are all 0 and unused in this mode.
    main_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When False, users only pick a day (no DPS/Tank/Healer role selection).
    gw_role_selection: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    voice_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Auto-created GvgMatch linked to this GW party (set after spawn)
    gvg_match_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Language of the party embed — toggled by the 🌐 button. Default 'ru'.
    language: Mapped[str] = mapped_column(String(4), default="ru", nullable=False)
    # Optional activity start time entered at creation — displayed below title.
    # Stored as "DD-MM HH:MM" (e.g. "12-10 21:00"), max 11 chars.
    start_time: Mapped[str | None] = mapped_column(String(11), nullable=True)

    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    members: Mapped[list["LFGMember"]] = relationship(
        back_populates="party",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="LFGMember.joined_at",
    )

    @property
    def total_slots(self) -> int:
        if self.main_limit is not None:
            return self.main_limit
        return self.dps_slots + self.healer_slots + self.tank_slots

    def role_capacity(self, role: str) -> int:
        return {
            "dps": self.dps_slots,
            "healer": self.healer_slots,
            "tank": self.tank_slots,
        }.get(role, 0)


class LFGMember(Base):
    __tablename__ = "lfg_members"
    __table_args__ = (
        UniqueConstraint("party_id", "user_id", name="uq_lfg_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    party_id: Mapped[int] = mapped_column(
        ForeignKey("lfg_parties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)  # dps | healer | tank
    day: Mapped[str | None] = mapped_column(String(4), nullable=True)   # sat|sun|both
    is_reserve: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    party: Mapped[LFGParty] = relationship(back_populates="members")


class LFGSchedule(Base):
    """Owner-defined recurring LFG spawn — bot creates a fresh party
    at run_at (respecting repeat_kind) using this row's mode/slot
    template. The scheduler task on the LFG cog polls next_run_at and
    fires when it lapses."""

    __tablename__ = "lfg_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # Who owns the schedule. Also credited as `creator_id` on every
    # party the schedule spawns so the End/Start buttons belong to
    # a real member — pick a server admin who's around often.
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Party template.
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    dps_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    healer_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tank_slots: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    voice_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Timing.
    # "once"   — fire at next_run_at, then disable
    # "daily"  — fire every day at run_hour:run_minute (weekday ignored)
    # "weekly" — fire on `weekday` at run_hour:run_minute
    repeat_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    weekday: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )  # 0=Mon .. 6=Sun; NULL for daily/once
    run_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    run_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    tz_offset: Mapped[int] = mapped_column(
        Integer, default=7, nullable=False,
    )  # hours from UTC (-12..14)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Persisted so a redeploy doesn't miss the next fire — the
    # scheduler advances this after each spawn and reads it back on
    # boot.
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_party_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


# ============================================================================
# /poll — event participation polls with optional weekly recurrence
# ============================================================================


class Poll(Base):
    """One poll instance = one Discord message + its live vote roster.

    Recurring polls live as a CHAIN of Poll rows: each fresh instance
    is a new row whose ``parent_poll_id`` points back to the original
    creation. The head-of-chain row (parent_poll_id NULL) holds the
    recurrence config (``repeat_kind`` + ``repeat_weekday`` etc.); the
    scheduler reads only heads and spawns child rows. Child rows are
    plain polls with ``repeat_kind='once'`` — they never spawn further.

    Statuses:
      open       → accepting votes, buttons live
      closed     → deadline hit; buttons disabled, result frozen
      cancelled  → owner manually killed it before deadline
    """

    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    close_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="open", nullable=False
    )

    # Recurrence config — only meaningful on head-of-chain rows.
    repeat_kind: Mapped[str] = mapped_column(
        String(16), default="once", nullable=False
    )
    repeat_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repeat_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repeat_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tz_offset: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )
    parent_poll_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    options: Mapped[list["PollOption"]] = relationship(
        back_populates="poll",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PollOption.position",
    )
    votes: Mapped[list["PollVote"]] = relationship(
        back_populates="poll",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PollOption(Base):
    """One selectable choice on a poll."""

    __tablename__ = "poll_options"
    __table_args__ = (
        UniqueConstraint("poll_id", "position", name="uq_poll_option_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[int] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)

    poll: Mapped[Poll] = relationship(back_populates="options")


class PollVote(Base):
    """A user's current pick on a poll. Unique on (poll_id, user_id) —
    when a user picks again, we UPDATE this row (single-choice poll)."""

    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_poll_vote_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[int] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    option_id: Mapped[int] = mapped_column(
        ForeignKey("poll_options.id", ondelete="CASCADE"), nullable=False
    )
    voted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    poll: Mapped[Poll] = relationship(back_populates="votes")


class PollLeaderboard(Base):
    """Per-guild anchor for the auto-updating vote ranking card.

    One board per guild — `/poll rank post` upserts this row; the cog
    re-edits `message_id` in place on every debounced vote/delete tick.
    """

    __tablename__ = "poll_leaderboards"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Cap on how many voters per option-label bucket the card shows.
    display_count: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ============================================================================
# /giveaway — owner-driven multi-winner raffle (separate from /quaythuong)
# ============================================================================


class Giveaway(Base):
    """One /giveaway event. Owner picks a channel + name + winner count;
    members click Join; owner clicks Roll for a sequential per-winner
    animation that excludes previously-picked winners.

    Deliberately a separate table from ``giveaway_games`` (the older
    /quaythuong leader-driven raffle) so the new flow can stay simple
    — no leader_id, no reroll counter, plain multi-winner via
    winners_csv.

    Statuses:
      open       → accepting joins; Roll + Cancel active
      rolling    → animation in flight; buttons disabled
      finished   → winners locked in ``winners_csv``, card frozen
      cancelled  → owner killed it before rolling
    """

    __tablename__ = "giveaways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    winners_count: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="open", nullable=False
    )
    # CSV of winner user_ids after Roll completes — kept as a CSV so we
    # don't need a whole extra table for a bounded 1-20-ish list.
    winners_csv: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    joins: Mapped[list["GiveawayJoin"]] = relationship(
        back_populates="giveaway",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="GiveawayJoin.joined_at",
    )

    def winner_ids(self) -> list[int]:
        """Parse winners_csv back into a list — safe when NULL/empty."""
        if not self.winners_csv:
            return []
        out: list[int] = []
        for chunk in self.winners_csv.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.append(int(chunk))
            except ValueError:
                continue
        return out


class GiveawayJoin(Base):
    """One member's entry into a /giveaway. Unique per (giveaway, user)
    so a double-click doesn't stack the same person into the pool."""

    __tablename__ = "giveaway_joins"
    __table_args__ = (
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_join_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(
        ForeignKey("giveaways.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    giveaway: Mapped[Giveaway] = relationship(back_populates="joins")


# ============================================================================
# Quiz minigame
# ============================================================================


class QuizQuestion(Base):
    """A multiple-choice question stored in the per-guild question bank."""

    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    option_a: Mapped[str] = mapped_column(Text, nullable=False)
    option_b: Mapped[str] = mapped_column(Text, nullable=False)
    option_c: Mapped[str] = mapped_column(Text, nullable=False)
    option_d: Mapped[str] = mapped_column(Text, nullable=False)
    correct: Mapped[str] = mapped_column(String(1), nullable=False)  # A | B | C | D
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def option(self, letter: str) -> str:
        return {
            "A": self.option_a,
            "B": self.option_b,
            "C": self.option_c,
            "D": self.option_d,
        }[letter.upper()]


class QuizSession(Base):
    """One run of the quiz minigame (waiting → running → finished)."""

    __tablename__ = "quiz_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    controller_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    num_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    seconds_per_question: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )  # waiting | running | finished
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    questions: Mapped[list["QuizSessionQuestion"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="QuizSessionQuestion.order_index",
    )


class QuizSessionQuestion(Base):
    """Snapshot of which questions belong to a session, in their drawn order."""

    __tablename__ = "quiz_session_questions"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "order_index", name="uq_quiz_session_order"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_questions.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # The message that hosts this question's buttons during the run.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    session: Mapped[QuizSession] = relationship(back_populates="questions")
    question: Mapped[QuizQuestion] = relationship(lazy="joined")


class QuizAnswer(Base):
    """One user's answer to one question of one session. Unique per triple."""

    __tablename__ = "quiz_answers"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "question_id", "user_id", name="uq_quiz_answer"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_questions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    choice: Mapped[str] = mapped_column(String(1), nullable=False)  # A | B | C | D
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuizWin(Base):
    """One row per (guild, user, session) when the user finished top of the
    leaderboard. Ties on top score → every tied user gets a row.
    Feeds the quiz_wins_for_role auto-grant."""

    __tablename__ = "quiz_wins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), nullable=False
    )
    won_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================================
# Sohoc (number-guessing) minigame
# ============================================================================


class SohocSession(Base):
    """One round of the number-guess game."""

    __tablename__ = "sohoc_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )  # waiting | rollable | finished
    rolled_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Second roll drawn together with rolled_number. Adds a consolation
    # tier — the user who picked this number gets top2_reward instead
    # of the top1 prize. NULL for legacy single-roll rows.
    rolled_number_2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_2_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Language of the card body — flipped by the 🌐 toggle button.
    language: Mapped[str] = mapped_column(
        String(4), default="vi", nullable=False
    )

    entries: Mapped[list["SohocEntry"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SohocEntry.submitted_at",
    )


class SohocEntry(Base):
    """One user's number pick for a Sohoc session — one per (session, user)."""

    __tablename__ = "sohoc_entries"
    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_sohoc_entry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sohoc_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)  # 0..99
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # Coin escrow for the "Bet" button. Debited on bet placement,
    # refunded plus bonus on win, lost on lose. 0 when the user didn't
    # opt into the bet.
    bet_amount: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    session: Mapped[SohocSession] = relationship(back_populates="entries")


class SohocWin(Base):
    """Audit log of wins per user per guild — used for the 5-win role grant."""

    __tablename__ = "sohoc_wins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sohoc_sessions.id", ondelete="CASCADE"), nullable=False
    )
    won_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================================
# Caro (12x12 five-in-a-row) minigame
# ============================================================================


class CaroGame(Base):
    """One game of 12x12 Caro. The board is serialized as a 144-char string,
    row-major, with characters '.', 'X', 'O'. status moves
    waiting → playing → finished."""

    __tablename__ = "caro_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    player_x_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    player_o_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    board: Mapped[str] = mapped_column(String(160), default="." * 144, nullable=False)
    turn: Mapped[str] = mapped_column(String(1), default="X", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )  # waiting | playing | finished

    winner_mark: Mapped[str | None] = mapped_column(String(1), nullable=True)  # X | O | None (draw)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # ";"-separated "r,c" pairs of the winning 5 cells
    win_line: Mapped[str | None] = mapped_column(String(128), nullable=True)
    move_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_move_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaroWin(Base):
    """Audit log of Caro wins for the wins-for-role auto-grant."""

    __tablename__ = "caro_wins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("caro_games.id", ondelete="CASCADE"), nullable=False
    )
    won_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================================
# Giveaway (/quaythuong) — leader-driven roll with an animated reveal
# ============================================================================


class GiveawayGame(Base):
    """One round of the /quaythuong giveaway.

    Owner runs `/quaythuong start` and picks a Game Leader; the bot posts
    a game card in giveaway_channel_id with Join + Start Roll buttons.
    Only leader_id can click Start Roll / Reroll / Finish. Winner is
    rerollable up to _MAX_REROLLS times (=> 4 possible winners total)
    before Finish locks the result and reveals the Gift.

    Statuses:
      waiting      → joining open, leader can Start Roll
      rolling      → animation in flight; buttons disabled
      showed       → winner shown, reroll/finish available
      finished     → winner locked, Gift revealed, buttons off
      cancelled    → creator or leader hit Cancel
    """

    __tablename__ = "giveaway_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    leader_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gift_name: Mapped[str] = mapped_column(String(256), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reroll_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Language of the card body — flipped by the 🌐 toggle button.
    language: Mapped[str] = mapped_column(
        String(4), default="vi", nullable=False
    )

    entries: Mapped[list["GiveawayEntry"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="GiveawayEntry.joined_at",
    )


class GiveawayEntry(Base):
    """One user's join on a giveaway. Unique per (game, user)."""

    __tablename__ = "giveaway_entries"
    __table_args__ = (
        UniqueConstraint("game_id", "user_id", name="uq_giveaway_entry"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("giveaway_games.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    game: Mapped[GiveawayGame] = relationship(back_populates="entries")


# ============================================================================
# Coin economy
# ============================================================================


class GuildEconomyConfig(Base):
    """Per-guild knobs for the coin economy. Created lazily on first
    /economy setup or first reward grant."""

    __tablename__ = "economy_guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    coin_name: Mapped[str] = mapped_column(String(32), default="Coin", nullable=False)
    # Emoji string for display: unicode (🪙) or custom (<:name:id>).
    coin_icon: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Reward amounts (0 effectively disables a reward without the boolean).
    join_reward_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_text_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_voice_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Server-boost (Nitro) reward — granted once per user per guild.
    boost_reward_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # /baodanh approval reward — granted once per user per guild when an
    # admin approves their pending registration card.
    approval_reward_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    join_reward_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    daily_text_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    daily_voice_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    boost_reward_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    approval_reward_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    # Anti-abuse knobs.
    # min_message_length defaults to 0 so the reward fires without
    # requiring the message_content privileged intent. Admins who do
    # enable that intent in the developer portal can set this >0 via
    # /economy setup to enforce a minimum length.
    min_message_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    voice_min_duration_seconds: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )

    # CSV of role IDs allowed to run admin-tier /economy commands in
    # addition to owner / Manage Server. Empty = only those two.
    manager_role_ids: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # /diemdanh (daily check-in) config
    # Legacy: was used for a per-event public announce in checkin_channel.
    # That spammy notification has been removed in favor of the persistent
    # card's daily list + the clicker's ephemeral receipt; column kept per
    # additive-only schema policy and may be repurposed later.
    checkin_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Opt-in: admin must explicitly enable. DMs to every server member
    # every 24h, so default OFF to avoid spam on bot install.
    checkin_reminder_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Integer UTC offset in hours (-12..14). 0 = UTC, 7 = Vietnam.
    # Controls when "today" rolls over for streak/list-clear purposes.
    checkin_tz_offset: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # Exact LOCAL hour (0-23, interpreted via checkin_tz_offset) at
    # which the daily reminder DM fires. Column name kept as
    # `_hour_utc` for additive-schema continuity; the value is local.
    # Sweep computes the effective UTC hour as (this - tz_offset) % 24.
    # NULL = fall back to local-midnight detection.
    checkin_reminder_hour_utc: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    # UI language for all user-facing economy embeds: "ru" (default) or "en".
    language: Mapped[str] = mapped_column(String(2), default="ru", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def manager_ids(self) -> list[int]:
        if not self.manager_role_ids:
            return []
        out: list[int] = []
        for chunk in self.manager_role_ids.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.append(int(chunk))
        return out

    def coin_label(self, amount: int | None = None) -> str:
        prefix = f"{amount:,} " if amount is not None else ""
        icon = f"{self.coin_icon} " if self.coin_icon else ""
        return f"{prefix}{icon}{self.coin_name}".strip()


class UserWallet(Base):
    """Per-(guild, user) balance. balance >= 0 is enforced by service code."""

    __tablename__ = "economy_wallets"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_economy_wallet"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CoinTransaction(Base):
    """Audit log row written for every wallet change. balance_after lets us
    rebuild any wallet from its transactions if a row is ever questioned."""

    __tablename__ = "economy_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)  # +credit / -debit
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DailyInteractionReward(Base):
    """One row per claim. Unique key prevents double-claiming the same
    (channel, type) on the same calendar day."""

    __tablename__ = "economy_daily_claims"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "user_id",
            "channel_id",
            "reward_type",
            "claim_date",
            name="uq_economy_daily_claim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reward_type: Mapped[str] = mapped_column(String(16), nullable=False)  # text | voice
    claim_date: Mapped[str] = mapped_column(
        String(10), nullable=False
    )  # YYYY-MM-DD in UTC
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MiniGameRewardConfig(Base):
    """Per-guild, per-game-type reward amounts. Game cogs read this then
    pass results into grant_minigame_rewards()."""

    __tablename__ = "economy_minigame_configs"
    __table_args__ = (
        UniqueConstraint("guild_id", "game_type", name="uq_economy_minigame_cfg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    game_type: Mapped[str] = mapped_column(String(32), nullable=False)  # quiz | sohoc
    # Legacy reward fields (deprecated, kept per additive-only schema policy).
    # New code reads top1_reward / top2_reward / top3_reward below.
    win_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runner_up_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Coin amounts for the top three placement buckets. Ties share a bucket
    # (e.g. two users tied for top-1 each receive top1_reward).
    top1_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top2_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top3_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Anyone who joined but didn't make the top-3 placements.
    participate_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Amount deducted from each user's wallet when they enter the game.
    # 0 = free to play. Charged eagerly when the entry is recorded.
    entry_fee: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MiniGameRewardClaim(Base):
    """Idempotency record so re-dispatching a finished game can't pay out twice."""

    __tablename__ = "economy_minigame_claims"
    __table_args__ = (
        UniqueConstraint(
            "game_type",
            "session_id",
            "user_id",
            "outcome",
            name="uq_economy_minigame_claim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    game_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # win/participate/runner_up
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IgnoredRewardChannel(Base):
    """Channels excluded from daily message/voice reward accounting."""

    __tablename__ = "economy_ignored_channels"
    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", name="uq_economy_ignored"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GameWinBoard(Base):
    """Per-(guild, game_type) auto-updating board ranking users by lifetime
    wins in that game. Game cogs render it under their own title (e.g. quiz's
    'Scholar' board, sohoc's 'Oracle' board); the table is generic so a new
    game just picks a game_type string without a schema change.
    """

    __tablename__ = "game_win_boards"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_type: Mapped[str] = mapped_column(
        String(16), primary_key=True
    )  # 'quiz' | 'sohoc' | …
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    display_count: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    language: Mapped[str] = mapped_column(String(4), default="ru", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PhuHoLeaderboard(Base):
    """Per-guild auto-updating richest-users leaderboard.

    One row per guild. The bot keeps `message_id` pointing at a single
    embed in `channel_id`; whenever a wallet changes, the embed is
    re-rendered in place (debounced) so the board stays live without
    posting new messages.
    """

    __tablename__ = "economy_phuho_boards"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    display_count: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class VoiceSession(Base):
    """Open-ended voice presence row written on join, closed on leave so
    we can compute time-in-channel for daily voice reward eligibility."""

    __tablename__ = "economy_voice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CheckinCard(Base):
    """Per-guild persistent check-in card. One row per guild; the bot
    keeps `message_id` pointing at a single embed in `channel_id` that
    shows today's date + the list of members who have already checked
    in today. The card carries a 'Check in now' DynamicItem button
    that re-uses the same /diemdanh logic on the server side.
    """

    __tablename__ = "economy_checkin_cards"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyCheckin(Base):
    """Per-(guild, user) daily check-in tracker.

    last_checkin_date is the UTC date (YYYY-MM-DD) of the most recent
    successful /diemdanh. current_streak is the running consecutive-day
    count; it's bumped when last_checkin_date is exactly yesterday and
    reset to 1 otherwise. longest_streak is the lifetime best.
    """

    __tablename__ = "economy_daily_checkins"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_economy_daily_checkin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    last_checkin_date: Mapped[str] = mapped_column(String(10), nullable=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_checkins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CheckinReminderPref(Base):
    """Per-(guild, user) opt-out for daily check-in DM reminders.

    Absent row = default (respects the guild's checkin_reminder_enabled
    setting). Row with dm_enabled=False = user explicitly opted out.
    Row with dm_enabled=True = user explicitly opted in (same as absent
    today, kept so a future "opt-in required" flip preserves consent).
    """

    __tablename__ = "economy_checkin_reminder_prefs"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "user_id", name="uq_checkin_reminder_pref"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    dm_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ============================================================================
# Oantuti (Rock-Paper-Scissors 1v1) minigame
# ============================================================================


class OantutiGame(Base):
    """One 1v1 RPS match between two players. status moves
    invited → running → finished | cancelled.

    bet_amount is the per-player escrow amount; 0 = friendly play. When
    bet > 0 both players are debited at accept-time and the loser's
    debit transfers to the winner at finish (tie → refund both).

    tournament_match_id (nullable) tags games that are part of a
    bracketed tournament so the tournament cog can find them by FK
    when the oantuti_finished event fires.
    """

    __tablename__ = "oantuti_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    player_a_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # challenger
    player_b_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # opponent
    rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    bet_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    a_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    b_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="invited", nullable=False
    )  # invited | running | finished | cancelled
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_tie: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tournament_match_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    # When a Rematch button was used, this points at the new game that
    # was spawned from this one — used so the button can be no-op'd if
    # clicked twice.
    rematch_game_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Language of the card body — flipped by the 🌐 toggle button.
    language: Mapped[str] = mapped_column(
        String(4), default="vi", nullable=False
    )


class OantutiRound(Base):
    """One round inside an OantutiGame. Each round is unique on
    (game_id, round_index)."""

    __tablename__ = "oantuti_rounds"
    __table_args__ = (
        UniqueConstraint("game_id", "round_index", name="uq_oantuti_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("oantuti_games.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    round_index: Mapped[int] = mapped_column(Integer, nullable=False)
    a_choice: Mapped[str | None] = mapped_column(String(8), nullable=True)  # bua|bao|keo
    b_choice: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 'a' | 'b' | 'tie' once both have picked, else None.
    result: Mapped[str | None] = mapped_column(String(4), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ============================================================================
# Tournament (single-elimination bracket on top of oantuti)
# ============================================================================


class Tournament(Base):
    """One tournament run per guild at a time (enforced in cog logic, not
    schema). Lifecycle: registering → bracket_ready → running → finished.
    """

    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Channel where the bracket embed lives + auto-updates.
    bracket_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bracket_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Channel where each per-pair oantuti match is posted.
    match_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Vinh danh — final results announcement.
    announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Registration card (with Join button), so we can disable it on start.
    registration_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    max_participants: Mapped[int] = mapped_column(Integer, nullable=False)
    rounds_per_match: Mapped[int] = mapped_column(Integer, nullable=False)
    top1_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top2_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top3_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Reward the unlucky-bye player(s) — anyone odd-eliminated at any round.
    odd_out_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="registering", nullable=False
    )  # registering | bracket_ready | running | finished | cancelled

    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    runnerup_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    third_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    participants: Mapped[list["TournamentParticipant"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TournamentParticipant.joined_at",
    )
    matches: Mapped[list["TournamentMatch"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TournamentMatch.round_number, TournamentMatch.pair_index",
    )


class TournamentParticipant(Base):
    __tablename__ = "tournament_participants"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "user_id", name="uq_tournament_participant"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set when the player's last match is decided. 1=champion, 2=runner-up,
    # 3=semi-final loser, None=eliminated earlier or odd-out.
    placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    odd_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="participants")


class TournamentMatch(Base):
    """One pairing in the bracket. round_number 1 = R1, then 2, 3, …
    pair_index is the slot inside that round (0-based, left-to-right).
    A bye match (no opponent) carries is_bye=True and a single player_a_id;
    it's marked finished immediately at bracket creation.
    """

    __tablename__ = "tournament_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pair_index: Mapped[int] = mapped_column(Integer, nullable=False)
    player_a_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    player_b_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_bye: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    oantuti_game_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending | in_progress | finished | bye
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tournament: Mapped[Tournament] = relationship(back_populates="matches")


# ============================================================================
# VuaTiengViet (Vietnamese word-scramble) minigame
# ============================================================================


class VietWord(Base):
    """Per-guild dictionary entry. `word` is the answer; `meaning` is
    the explanation shown after the question reveals."""

    __tablename__ = "viet_words"
    __table_args__ = (
        UniqueConstraint("guild_id", "word", name="uq_viet_word"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    word: Mapped[str] = mapped_column(String(128), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VuaTiengVietSession(Base):
    """One run of the game. Lifecycle: waiting → running → finished |
    cancelled. pause_seconds = NULL means the creator advances each
    question manually with a 'Next question' button."""

    __tablename__ = "vtv_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    num_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    seconds_per_question: Mapped[int] = mapped_column(Integer, nullable=False)
    pause_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top1_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top2_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top3_reward: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )  # waiting | running | finished | cancelled
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    questions: Mapped[list["VuaTiengVietSessionQuestion"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="VuaTiengVietSessionQuestion.order_index",
    )


class VuaTiengVietSessionQuestion(Base):
    """Snapshot of which dictionary words belong to this session, in
    draw order. started_at marks when the question went live (answer
    timing uses this); revealed_at marks when reveal happened."""

    __tablename__ = "vtv_session_questions"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "order_index", name="uq_vtv_session_order"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("vtv_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    word_id: Mapped[int] = mapped_column(
        ForeignKey("viet_words.id", ondelete="CASCADE"), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    winner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scrambled_text: Mapped[str | None] = mapped_column(String(256), nullable=True)

    session: Mapped[VuaTiengVietSession] = relationship(back_populates="questions")
    word: Mapped[VietWord] = relationship(lazy="joined")


class VuaTiengVietAnswer(Base):
    """One user's submission for one question. Unique on
    (session_question_id, user_id) — one attempt per user per question.

    elapsed_ms is computed as submitted_at − click_at to enforce the
    "fastest correct wins" tiebreaker. accepted=False means the modal
    was submitted after the deadline; the row is still kept for the
    reveal embed's audit list.
    """

    __tablename__ = "vtv_answers"
    __table_args__ = (
        UniqueConstraint(
            "session_question_id", "user_id", name="uq_vtv_answer"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_question_id: Mapped[int] = mapped_column(
        ForeignKey("vtv_session_questions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    answer_text: Mapped[str] = mapped_column(String(256), nullable=False)
    click_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class VuaTiengVietWin(Base):
    """Audit row for each session's top-1 winner. Feeds the leaderboard."""

    __tablename__ = "vtv_wins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("vtv_sessions.id", ondelete="CASCADE"), nullable=False
    )
    won_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================================
# Pet system — owner/admin uploads species asset packs, users adopt + care
# (Phase 1: schema only; zip upload + image storage land in Phase 2.)
# ============================================================================


class PetGuildConfig(Base):
    """Per-guild pet system knobs. Created lazily on first /pet-admin use."""

    __tablename__ = "pet_guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Role that can run /pet-admin commands alongside the guild owner.
    admin_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # ZIP upload limits (Phase 2 will enforce these).
    max_zip_size_mb: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_files_per_zip: Mapped[int] = mapped_column(Integer, default=250, nullable=False)
    # Behavior toggles
    allow_administrator_pet_admin: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    auto_enable_shop_after_import: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Default shop prices by rarity
    default_common_price: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    default_rare_price: Mapped[int] = mapped_column(Integer, default=2000, nullable=False)
    default_epic_price: Mapped[int] = mapped_column(Integer, default=7500, nullable=False)
    default_legendary_price: Mapped[int] = mapped_column(Integer, default=20000, nullable=False)
    # Pet House — persistent embed where members open their active pet
    pet_house_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pet_house_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Custom display name in the Pet House welcome line. NULL → fall
    # back to the live guild.name.
    pet_house_server_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Public channel where /pet share posts a snapshot of the user's pet
    pet_share_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Coin cost per care action (0 disables the deduction).
    pet_feed_cost: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    pet_water_cost: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    # Coin charged by the Pet House Rename button to discourage
    # spam-renaming. 0 = free.
    pet_rename_cost: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    # Mission reset clock. tz_offset is hours from UTC (-12..14); the
    # reset boundary lands at reset_hour:00 in that local timezone.
    # Defaults to 07 / 00 = midnight VN, matching the previous
    # hardcoded behaviour so existing guilds keep their old cadence.
    pet_mission_tz_offset: Mapped[int] = mapped_column(
        Integer, default=7, nullable=False,
    )
    pet_mission_reset_hour: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PetSpecies(Base):
    """A pet species defined per-guild. species_id is the snake_case
    identifier (e.g. 'moon_cat'); display_name is the Vietnamese label."""

    __tablename__ = "pet_species"
    __table_args__ = (
        UniqueConstraint("guild_id", "species_id", name="uq_pet_species"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    species_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rarity: Mapped[str] = mapped_column(
        String(16), default="common", nullable=False
    )  # common | rare | epic | legendary
    price: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    concept: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_shop_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Bridge to the akim-desk-pet asset pool. NULL = species has no
    # desktop representation yet; desktop falls back to its built-in
    # fallback pet. Admins set this via /pet-admin link-desktop-asset.
    desktop_asset_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    imported_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PetAsset(Base):
    """One image asset belonging to a pet species + stage. Populated by
    Phase 2's zip importer; the Phase 1 schema is in place but no rows
    are written yet."""

    __tablename__ = "pet_assets"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "species_id", "stage", "asset_type", "state_key",
            name="uq_pet_asset",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    species_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # baby|teen|adult|mystic
    pack_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 01_base_moods_effects etc.
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state_key: Mapped[str] = mapped_column(String(64), nullable=False)
    function_key: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    original_path: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    public_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PetAssetImportJob(Base):
    """Audit trail for every zip import attempt. Created in Phase 2's
    preview step, updated as the workflow progresses through confirm,
    import, success/failure. Stored as JSON strings (Text) so no extra
    deps; SQLAlchemy JSON type isn't portable to SQLite the same way."""

    __tablename__ = "pet_asset_import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    species_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # create | update | replace | preview
    status: Mapped[str] = mapped_column(
        String(32), default="PENDING", nullable=False
    )  # PENDING | VALIDATING | READY_TO_CONFIRM | IMPORTING | SUCCESS | FAILED | CANCELLED
    zip_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    zip_size: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_stages_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_assets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    errors_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_assets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_assets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_assets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserPet(Base):
    """One adopted pet per (guild, user, species). User can own multiple
    pets but only one is_active at a time (UI shows the active one)."""

    __tablename__ = "user_pets"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "user_id", "species_id", name="uq_user_pet"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    species_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hunger: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    thirst: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    mood: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    energy: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    cleanliness: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    bond: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    health: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), default="baby", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Cooldown timestamps — checked by _try_apply_action to enforce
    # per-action rate limits. NULL = never performed yet.
    last_play_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_clean_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_rest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_train_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Passive coin generation — per-stage rate granted hourly via the
    # existing decay loop. daily_income_credits caps the day's accrual
    # so a perma-active Mystic pet can't print unlimited coin; the date
    # column is the local VN date when the counter was last reset (ISO
    # "YYYY-MM-DD"). NULL date = no income granted yet.
    daily_income_credits: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
    )
    daily_income_date: Mapped[str | None] = mapped_column(
        String(10), nullable=True,
    )
    # Show this pet's compact info on the owner's /info card. Owners
    # toggle via the Pet House "Show in Info" button. Only the
    # active pet is rendered even when this flag is True on multiple.
    show_in_info: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PetMissionProgress(Base):
    """Per-user mission tracker. One row per (guild, user, mission_key,
    period_start). period_start is the local VN date for daily missions
    or the Monday of the local-VN week for weekly missions — that way
    progress for "today" or "this week" is keyed by a value the bot
    can compute from current time without storing period_type. claimed_at
    is set once the user redeems the mission's reward."""

    __tablename__ = "pet_mission_progress"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "user_id", "mission_key", "period_start",
            name="uq_pet_mission_period",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    guild_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
    )
    mission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # ISO date "YYYY-MM-DD" — daily = the day, weekly = Monday of week.
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


# =============================================================================
# Desktop pet integration (akim-desk-pet)
# =============================================================================


class DesktopLinkSession(Base):
    """One link-code attempt between a desktop client and a Discord
    account. Desktop creates a row via POST /api/desktop/link/create,
    Discord bot fills in discord_user_id + guild_id via /link-desktop,
    and desktop polls /api/desktop/link/status to retrieve the JWT.

    Token is only handed out once (see token_delivered_at) — if the
    desktop loses it, the user starts a fresh link flow.
    """

    __tablename__ = "desktop_link_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 6-digit human-shareable code, prefixed AKIM-. Looked up directly
    # in status polls, so we keep it indexed and unique.
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    # Populated when the bot confirms the code via /link-desktop.
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # SPEC says one desktop instance shows pets from one guild — store
    # the guild the user linked from so every API call is scoped without
    # the desktop client having to track it.
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # SHA-256 of the issued JWT — checked when the desktop reconnects so
    # an attacker who learns the code AFTER linking can't grab the token.
    # Cleared after the desktop polls /status once and gets the token.
    access_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )


class PetDesktopProfile(Base):
    """Desktop-only state for an individual UserPet instance — window
    position, scale, toggles. Lives in its own table (not on UserPet)
    so a guild without any desktop users doesn't carry the column
    overhead, and so it cascades on pet delete without polluting the
    pet model with desktop concerns."""

    __tablename__ = "pet_desktop_profiles"

    # PK == FK so each UserPet has at most one profile.
    pet_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("user_pets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Denormalised for cheap "list all profiles for this discord user"
    # queries during link/unlink — UserPet already has user_id but going
    # through it would force a join on every status fetch.
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
    )
    position_x: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    position_y: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    scale: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    selected_skin: Mapped[str] = mapped_column(
        String(32), default="default", nullable=False,
    )
    selected_accessory: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    bubble_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    sound_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    random_walk_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    always_on_top: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    last_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )



# =============================================================================
# /gvg — Guild War roster (SPEC rewrite, 30 Main + unlimited Reserve)
# =============================================================================
#
# Data hierarchy (see SPEC §3):
#
#   Discord Guild
#   ├── GvgGuildSettings  (1:1)
#   ├── GvgPosition       (0..N — global, shared across matches)
#   ├── GvgPlayerProfile  (0..N — per-user default style)
#   ├── GvgAuditLog       (0..N — write history)
#   └── GvgMatch          (0..N)
#          ├── GvgTeam                 (0..N)
#          │     ├── commander_user_id (0..1)
#          │     └── GvgRosterAssignment (0..N — MAIN | RESERVE)
#          └── GvgRosterMessage        (0..N — persistent-board pages)


class GvgGuildSettings(Base):
    __tablename__ = "gvg_guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manager_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    roster_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Optional. When set, the published roster board in roster_channel_id
    # stays read-only (no buttons); a compact "Manager controls" card
    # with the [Assign/Move/Teams/Refresh] buttons is posted here
    # instead. Point it at a channel Discord already gates via View
    # Channel permission for owner + manager role and non-managers can
    # neither see nor click the buttons.
    manager_channel_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )
    allow_commander_roster_management: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
    )
    default_main_limit: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False,
    )
    # Roster board layout selector.
    #   1 — legacy grouped-per-position view: one line per position,
    #       players joined with " │ " on the same line (dense but
    #       loses easy per-player reference).
    #   2 — flat per-player view: one line per assignment, format
    #       `- {position} — {weapon} **{name}** {crown}` (readable
    #       when the team is long or when copy-pasting).
    #   3 — compact horizontal: each team is an inline embed field
    #       (Discord packs up to 3 side-by-side on desktop, 2 on
    #       mobile, 1 in narrow layouts). Loses per-team color bar.
    view_mode: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, server_default="1",
    )
    # Icons drawn at the top-left / bottom-left corner of the avatar
    # on the view_mode=4 grid so team leader + guild commander are
    # recognizable at a glance. Accept the same two shapes as team
    # icons: a Discord custom emoji `<:name:id>` (best — fetched as
    # PNG from the CDN) or a unicode emoji like 👑 (best-effort
    # through the twemoji CDN). NULL falls back to sensible defaults
    # in the renderer (👑 commander, 🎖️ leader).
    grid_commander_icon: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    grid_leader_icon: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    # Empty pixels reserved above the avatar / below the position icon
    # inside every grid cell (view_mode=4). Bigger value = more
    # breathing room; smaller = tighter cell. Bounded [0, 80] in the
    # settings modal so users can't tune the grid into unreadable
    # geometry. Top padding also determines how far the corner badges
    # sit from the cell's top edge.
    grid_top_padding: Mapped[int] = mapped_column(
        Integer, default=18, nullable=False, server_default="18",
    )
    grid_bottom_padding: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False, server_default="10",
    )
    # Optional highlight color for the grid cell background of players
    # who hold a Commander or Team Leader role in the match. NULL = use
    # the default card background (_GRID_CARD_BG). Applied cross-team —
    # a player who is the leader of Team B but assigned to Team A will
    # also have the leader color on their Team A grid cell.
    grid_commander_bg_hex: Mapped[str | None] = mapped_column(
        String(7), nullable=True,
    )
    grid_leader_bg_hex: Mapped[str | None] = mapped_column(
        String(7), nullable=True,
    )
    # Glass / frosted-glass visual effect for view_mode=4 grid cells.
    #   grid_glass_enabled  — master toggle (default OFF for back-compat)
    #   grid_glass_blur     — blur the canvas region behind each card
    #                         before compositing (frosted effect); only
    #                         meaningful when glass is on
    #   grid_glass_opacity  — alpha of the glass card (0 = fully
    #                         transparent, 100 = opaque); default 80
    #   grid_commander_bg_alpha / grid_leader_bg_alpha — how much the
    #                         commander / leader color bleeds through
    #                         (100 = full color, 0 = card color only)
    grid_glass_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false",
    )
    grid_glass_blur: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true",
    )
    grid_glass_opacity: Mapped[int] = mapped_column(
        Integer, default=80, nullable=False, server_default="80",
    )
    grid_commander_bg_alpha: Mapped[int] = mapped_column(
        Integer, default=100, nullable=False, server_default="100",
    )
    grid_leader_bg_alpha: Mapped[int] = mapped_column(
        Integer, default=100, nullable=False, server_default="100",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class GvgPosition(Base):
    """Global position (Tank / DPS / Healer / …). Belongs to a Discord
    guild — reused across every Match/Team of that guild. Soft-deleted
    via is_active=False when historical assignments still reference it."""

    __tablename__ = "gvg_positions"
    __table_args__ = (
        UniqueConstraint("guild_id", "name", name="uq_gvg_position_guild_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class GvgPlayerProfile(Base):
    """Guild-level default style + metadata for one Discord user. Auto-
    created on first assignment."""

    __tablename__ = "gvg_player_profiles"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_gvg_player_profile"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class GvgMatch(Base):
    __tablename__ = "gvg_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    opponent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    main_player_limit: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False,
    )
    # DRAFT | PUBLISHED | LOCKED | ARCHIVED  (SPEC §5)
    status: Mapped[str] = mapped_column(
        String(16), default="DRAFT", nullable=False,
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # When non-null, this match was auto-created from the linked LFG GW party.
    lfg_party_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Guild-scoped "pool" team used to hold LFG-synced members before managers
    # assign them to real teams. Set by auto_create_for_gw; None on manual matches.
    lfg_pool_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    assignments: Mapped[list["GvgRosterAssignment"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list["GvgRosterMessage"]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
    )


class GvgTeam(Base):
    """Guild-scoped team — shared across every match of the guild.
    Only player assignments (GvgRosterAssignment) are per-match; the
    team's identity (name/icon/color/order/commander) is stable across
    the whole guild's GvG lifecycle."""

    __tablename__ = "gvg_teams"
    __table_args__ = (
        UniqueConstraint("guild_id", "name", name="uq_gvg_team_guild_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commander_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    assignments: Mapped[list["GvgRosterAssignment"]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
        order_by="GvgRosterAssignment.sort_order",
    )


class GvgRosterAssignment(Base):
    """A user placed on a Team. rosterType = MAIN or RESERVE.

    UNIQUE(match_id, user_id) so a player appears at most once per match
    across every team + roster type. Moving a player is an UPDATE, not
    a delete-then-insert (SPEC §14, §25)."""

    __tablename__ = "gvg_roster_assignments"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "user_id",
            name="uq_gvg_roster_assignment_match_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("gvg_matches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("gvg_teams.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("gvg_positions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # MAIN | RESERVE
    roster_type: Mapped[str] = mapped_column(
        String(8), default="MAIN", nullable=False,
    )
    custom_icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_color_hex: Mapped[str | None] = mapped_column(String(7), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )

    match: Mapped["GvgMatch"] = relationship(back_populates="assignments")
    team: Mapped["GvgTeam"] = relationship(back_populates="assignments")


class GvgTeamLeader(Base):
    """Per-(match, team) Team Leader — one of the players already
    assigned to that team is nominated the leader. Rendered at the top
    of the team block on the roster board. Unique on (match_id, team_id)
    so a team has at most one leader per match; distinct from
    ``GvgTeam.commander_user_id`` (which is guild-wide) — Team Leader is
    the tactical lead for a single match."""

    __tablename__ = "gvg_team_leaders"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "team_id",
            name="uq_gvg_team_leader_match_team",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("gvg_matches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("gvg_teams.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class GvgAvatarCache(Base):
    """Cached Discord avatar bytes for GvG roster participants.

    Populated by `/gvg avatar sync` (admin batch), and opportunistically
    when a user joins a match via Register/self-serve. Read by the
    image-grid roster renderer (view_mode=4) so a board refresh does
    not have to hit the Discord CDN. Unique per ``(guild_id, user_id)``
    because Discord allows per-server avatars; keeping the storage
    guild-scoped keeps the correct face on each server's board.

    ``avatar_key`` mirrors ``discord.Asset.key`` — the incremental
    sync compares it to the live Member's key so unchanged avatars
    skip the byte fetch entirely."""

    __tablename__ = "gvg_avatar_cache"
    __table_args__ = (
        UniqueConstraint(
            "guild_id", "user_id",
            name="uq_gvg_avatar_cache_guild_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    avatar_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # e.g. "image/png", "image/webp", "image/gif" for animated
    content_type: Mapped[str] = mapped_column(
        String(64), default="image/png", nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )


class GvgRosterMessage(Base):
    """Persistent-board pages. One published match may span many messages
    if the roster overflows a single embed (SPEC §33). Each row stores
    the channel + message id + page index so refresh can edit in place.

    `kind` distinguishes the roster board pages (`"BOARD"`) from the
    optional compact manager-controls card (`"MANAGER"`) that lives in
    a separate channel when `GvgGuildSettings.manager_channel_id` is
    configured."""

    __tablename__ = "gvg_roster_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("gvg_matches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # "BOARD" | "MANAGER"
    kind: Mapped[str] = mapped_column(
        String(16), default="BOARD", nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
    )

    match: Mapped["GvgMatch"] = relationship(back_populates="messages")


class GvgAuditLog(Base):
    """Write-log of every mutation. Displayed in the log channel if
    configured, always queryable via /gvg setup status."""

    __tablename__ = "gvg_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("gvg_matches.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True,
    )
