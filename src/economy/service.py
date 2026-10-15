"""Coin economy service layer.

The contract:
  - Every wallet mutation goes through WalletService.adjust() — that's the
    only place a CoinTransaction row gets written. Direct UPDATE on
    economy_wallets is forbidden so the ledger stays a complete history.
  - Negative balances are rejected (raises InsufficientBalanceError).
  - Reward grants (join / daily / minigame) sit behind RewardEngine, which
    enforces idempotency via the dedicated *Claim tables and only debits/
    credits via WalletService.

Listeners in cogs/economy.py call these methods. Minigame integrations in
cogs/quiz.py and cogs/sohoc.py call grant_minigame_rewards() after they
finish a round.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db.models import (
    CoinTransaction,
    DailyInteractionReward,
    GuildEconomyConfig,
    IgnoredRewardChannel,
    MiniGameRewardClaim,
    MiniGameRewardConfig,
    UserWallet,
)

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.economy")


class InsufficientBalanceError(Exception):
    """Raised when an adjust() would push balance below zero."""


# ---- Transaction reason constants (kept as strings to match DB col) ----

REASON_JOIN = "join"
REASON_DAILY_TEXT = "daily_text"
REASON_DAILY_VOICE = "daily_voice"
REASON_CHECKIN = "checkin"
REASON_BOOST = "boost"
REASON_APPROVAL = "approval"
REASON_PET_BUY = "pet_buy"
REASON_PET_FEED = "pet_feed"
REASON_PET_WATER = "pet_water"
REASON_PET_REFUND = "pet_refund"
REASON_PET_PASSIVE = "pet_passive"   # hourly stipend by stage
REASON_PET_MISSION = "pet_mission"   # daily / weekly mission claim
REASON_PET_BUFF = "pet_buff"         # minigame win boost
REASON_PET_RENAME = "pet_rename"     # Pet House rename fee
REASON_MINIGAME_PREFIX = "minigame_"  # e.g. minigame_quiz_win, minigame_sohoc_participate
REASON_ADMIN_ADD = "admin_add"
REASON_ADMIN_REMOVE = "admin_remove"
REASON_ADMIN_SET = "admin_set"
REASON_SYNC_INIT = "sync_init"


@dataclass
class MiniGameResult:
    """Payload passed to RewardEngine.grant_minigame_rewards.

    top1/top2/top3 are placement buckets — a user listed in top1 gets
    cfg.top1_reward, etc. Ties share a bucket (two users tied for first
    are both in top1 and both receive top1_reward). A user in multiple
    buckets only gets paid for the highest one. participants is for
    everyone who took part but missed the top-3.

    session_id is used purely as the idempotency key (together with
    game_type, user_id, outcome) so re-dispatching a finished game
    cannot double-pay.
    """

    guild_id: int
    game_type: str
    session_id: int
    top1: list[int] = field(default_factory=list)
    top2: list[int] = field(default_factory=list)
    top3: list[int] = field(default_factory=list)
    participants: list[int] = field(default_factory=list)


@dataclass
class GrantedReward:
    user_id: int
    outcome: str
    amount: int


class WalletService:
    """All wallet mutations live here. Direct UPDATE on the table is
    treated as a bug — go through adjust()."""

    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot

    async def get_or_create_wallet(
        self, guild_id: int, user_id: int
    ) -> UserWallet:
        async with self.bot.db.session() as s:
            wallet = await s.scalar(
                select(UserWallet).where(
                    UserWallet.guild_id == guild_id,
                    UserWallet.user_id == user_id,
                )
            )
            if wallet is None:
                wallet = UserWallet(
                    guild_id=guild_id, user_id=user_id, balance=0
                )
                s.add(wallet)
                try:
                    await s.commit()
                except IntegrityError:
                    # Concurrent insert raced us; just re-fetch.
                    await s.rollback()
                    wallet = await s.scalar(
                        select(UserWallet).where(
                            UserWallet.guild_id == guild_id,
                            UserWallet.user_id == user_id,
                        )
                    )
                else:
                    await s.refresh(wallet)
            assert wallet is not None
            return wallet

    async def adjust(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        reason: str,
        *,
        description: str | None = None,
        actor_id: int | None = None,
        metadata: dict | None = None,
    ) -> UserWallet:
        """Apply delta to wallet balance and record a CoinTransaction.

        delta == 0 is a no-op (returns the wallet without writing a tx).
        delta < 0 when the new balance would be negative raises
        InsufficientBalanceError.
        """
        if delta == 0:
            return await self.get_or_create_wallet(guild_id, user_id)
        async with self.bot.db.session() as s:
            wallet = await s.scalar(
                select(UserWallet).where(
                    UserWallet.guild_id == guild_id,
                    UserWallet.user_id == user_id,
                )
            )
            if wallet is None:
                wallet = UserWallet(
                    guild_id=guild_id, user_id=user_id, balance=0
                )
                s.add(wallet)
                await s.flush()
            new_balance = wallet.balance + delta
            if new_balance < 0:
                raise InsufficientBalanceError(
                    f"Insufficient balance: have {wallet.balance}, "
                    f"need {abs(delta)}"
                )
            wallet.balance = new_balance
            if delta > 0:
                wallet.total_earned += delta
            else:
                wallet.total_spent += -delta
            s.add(
                CoinTransaction(
                    guild_id=guild_id,
                    user_id=user_id,
                    delta=delta,
                    reason=reason,
                    description=description,
                    actor_id=actor_id,
                    metadata_json=json.dumps(metadata) if metadata else None,
                    balance_after=new_balance,
                )
            )
            await s.commit()
            await s.refresh(wallet)
        # Notify listeners (e.g. the Rich List leaderboard refresher) that a
        # wallet in this guild moved. Fired outside the DB session block
        # so handlers don't see a half-committed state.
        try:
            self.bot.dispatch("wallet_changed", guild_id)
        except Exception:
            log.exception("WalletService: wallet_changed dispatch failed")
        return wallet

    async def apply_pet_minigame_buff(
        self,
        guild_id: int,
        user_id: int,
        base_amount: int,
        game_type: str,
        *,
        session_id: int | None = None,
        outcome: str = "win",
    ) -> int:
        """Grant a stage-based pet buff on top of a minigame reward.
        Returns the bonus coin granted (0 when there's no active pet,
        the percent rounds down, or base_amount <= 0). Logged as a
        separate REASON_PET_BUFF transaction so the ledger shows where
        the extra coin came from. Lazy imports to keep economy free of
        compile-time pet deps."""
        if base_amount <= 0:
            return 0
        from ..db.models import UserPet
        from ..pets.care import STAGE_REWARD_BUFF_PCT
        async with self.bot.db.session() as s:
            pet = await s.scalar(
                select(UserPet).where(
                    UserPet.guild_id == guild_id,
                    UserPet.user_id == user_id,
                    UserPet.is_active.is_(True),
                )
            )
        if pet is None:
            return 0
        pct = STAGE_REWARD_BUFF_PCT.get(pet.stage, 0)
        if pct <= 0:
            return 0
        bonus = base_amount * pct // 100
        if bonus <= 0:
            return 0
        await self.adjust(
            guild_id, user_id, bonus, REASON_PET_BUFF,
            description=(
                f"Pet buff +{pct}% on {game_type} {outcome}"
                + (f" (session #{session_id})" if session_id else "")
            ),
            metadata={
                "game_type": game_type,
                "session_id": session_id,
                "outcome": outcome,
                "stage": pet.stage,
                "species_id": pet.species_id,
                "base": base_amount,
                "pct": pct,
            },
        )
        return bonus

    async def set_balance(
        self,
        guild_id: int,
        user_id: int,
        new_balance: int,
        *,
        actor_id: int | None = None,
        description: str | None = None,
    ) -> UserWallet:
        """Used by /economy set. Computes the implied delta and routes
        through adjust() so the transaction log still captures the change."""
        if new_balance < 0:
            raise InsufficientBalanceError("Balance cannot be set below zero.")
        current = await self.get_or_create_wallet(guild_id, user_id)
        delta = new_balance - current.balance
        if delta == 0:
            return current
        return await self.adjust(
            guild_id,
            user_id,
            delta,
            REASON_ADMIN_SET,
            description=description or f"Set balance to {new_balance}",
            actor_id=actor_id,
        )


# =============================================================================
# Reward engine
# =============================================================================


class RewardEngine:
    """Wraps the per-flavor reward paths (join / daily text / daily voice /
    minigame). Each method is idempotent on its own claim table so a
    redelivered event can't pay out twice."""

    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        self.wallet = WalletService(bot)

    async def get_or_create_config(self, guild_id: int) -> GuildEconomyConfig:
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildEconomyConfig, guild_id)
            if cfg is None:
                cfg = GuildEconomyConfig(guild_id=guild_id)
                s.add(cfg)
                try:
                    await s.commit()
                except IntegrityError:
                    await s.rollback()
                    cfg = await s.get(GuildEconomyConfig, guild_id)
                else:
                    await s.refresh(cfg)
            assert cfg is not None
            return cfg

    async def is_channel_ignored(self, guild_id: int, channel_id: int) -> bool:
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(IgnoredRewardChannel).where(
                    IgnoredRewardChannel.guild_id == guild_id,
                    IgnoredRewardChannel.channel_id == channel_id,
                )
            )
        return row is not None

    # ---------- join ----------

    async def grant_approval_reward(self, guild_id: int, user_id: int) -> int:
        """/baodanh approval reward. Granted exactly once per (guild, user) —
        a re-join + re-approval cycle does NOT pay a second time.

        Returns amount granted (0 if disabled, amount is 0, or already paid).
        """
        cfg = await self.get_or_create_config(guild_id)
        if not cfg.approval_reward_enabled or cfg.approval_reward_amount <= 0:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(CoinTransaction).where(
                    CoinTransaction.guild_id == guild_id,
                    CoinTransaction.user_id == user_id,
                    CoinTransaction.reason == REASON_APPROVAL,
                ).limit(1)
            )
        if existing is not None:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        await self.wallet.adjust(
            guild_id, user_id, cfg.approval_reward_amount,
            REASON_APPROVAL,
            description="Registration approved reward",
        )
        return cfg.approval_reward_amount

    async def grant_boost_reward(self, guild_id: int, user_id: int) -> int:
        """Server-boost reward. Granted exactly once per (guild, user) — if
        the user cancels and re-boosts later, no second payout.

        Returns amount granted (0 if disabled, amount is 0, or already paid).
        """
        cfg = await self.get_or_create_config(guild_id)
        if not cfg.boost_reward_enabled or cfg.boost_reward_amount <= 0:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(CoinTransaction).where(
                    CoinTransaction.guild_id == guild_id,
                    CoinTransaction.user_id == user_id,
                    CoinTransaction.reason == REASON_BOOST,
                ).limit(1)
            )
        if existing is not None:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        await self.wallet.adjust(
            guild_id, user_id, cfg.boost_reward_amount,
            REASON_BOOST,
            description="Server boost reward",
        )
        return cfg.boost_reward_amount

    # ---------- join ----------

    async def grant_join_reward(self, guild_id: int, user_id: int) -> int:
        """Returns amount granted (0 if disabled or already a member)."""
        cfg = await self.get_or_create_config(guild_id)
        if not cfg.join_reward_enabled or cfg.join_reward_amount <= 0:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        # Only grant once: if the wallet already had a join tx, skip.
        async with self.bot.db.session() as s:
            existing = await s.scalar(
                select(CoinTransaction).where(
                    CoinTransaction.guild_id == guild_id,
                    CoinTransaction.user_id == user_id,
                    CoinTransaction.reason == REASON_JOIN,
                ).limit(1)
            )
        if existing is not None:
            await self.wallet.get_or_create_wallet(guild_id, user_id)
            return 0
        await self.wallet.adjust(
            guild_id,
            user_id,
            cfg.join_reward_amount,
            REASON_JOIN,
            description="Server join welcome reward",
        )
        return cfg.join_reward_amount

    # ---------- daily text ----------

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def try_claim_daily_text(
        self, guild_id: int, user_id: int, channel_id: int
    ) -> int:
        """Claim today's per-channel text reward. Returns granted amount
        (0 if disabled, already claimed today, or channel ignored)."""
        cfg = await self.get_or_create_config(guild_id)
        if not cfg.daily_text_enabled or cfg.daily_text_reward <= 0:
            return 0
        if await self.is_channel_ignored(guild_id, channel_id):
            return 0
        return await self._try_claim_daily(
            guild_id, user_id, channel_id, "text", cfg.daily_text_reward,
            REASON_DAILY_TEXT, "Daily message reward",
        )

    async def try_claim_daily_voice(
        self, guild_id: int, user_id: int, channel_id: int, duration_seconds: int
    ) -> int:
        cfg = await self.get_or_create_config(guild_id)
        if not cfg.daily_voice_enabled or cfg.daily_voice_reward <= 0:
            return 0
        if duration_seconds < cfg.voice_min_duration_seconds:
            return 0
        if await self.is_channel_ignored(guild_id, channel_id):
            return 0
        return await self._try_claim_daily(
            guild_id, user_id, channel_id, "voice", cfg.daily_voice_reward,
            REASON_DAILY_VOICE, f"Daily voice reward ({duration_seconds}s)",
        )

    async def _try_claim_daily(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        reward_type: str,
        amount: int,
        reason: str,
        description: str,
    ) -> int:
        today = self._today_utc()
        async with self.bot.db.session() as s:
            claim = DailyInteractionReward(
                guild_id=guild_id,
                user_id=user_id,
                channel_id=channel_id,
                reward_type=reward_type,
                claim_date=today,
                amount=amount,
            )
            s.add(claim)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return 0  # already claimed today
        await self.wallet.adjust(
            guild_id,
            user_id,
            amount,
            reason,
            description=description,
            metadata={"channel_id": channel_id, "date": today},
        )
        return amount

    # ---------- minigame ----------

    async def get_minigame_config(
        self, guild_id: int, game_type: str
    ) -> MiniGameRewardConfig | None:
        async with self.bot.db.session() as s:
            return await s.scalar(
                select(MiniGameRewardConfig).where(
                    MiniGameRewardConfig.guild_id == guild_id,
                    MiniGameRewardConfig.game_type == game_type,
                )
            )

    async def grant_minigame_rewards(
        self, result: MiniGameResult
    ) -> list[GrantedReward]:
        """Single entry-point for minigame payouts. Idempotent per
        (game_type, session_id, user_id, outcome)."""
        cfg = await self.get_minigame_config(result.guild_id, result.game_type)
        if cfg is None or not cfg.enabled:
            return []

        # Build a list of (user_id, outcome, amount) tuples in priority
        # order. A user listed in multiple buckets gets only the highest
        # tier — top1 > top2 > top3 > participate.
        seen: set[int] = set()
        planned: list[tuple[int, str, int]] = []

        for bucket, outcome, amount in (
            (result.top1, "top1", cfg.top1_reward),
            (result.top2, "top2", cfg.top2_reward),
            (result.top3, "top3", cfg.top3_reward),
            (result.participants, "participate", cfg.participate_reward),
        ):
            if amount <= 0:
                continue
            for uid in bucket:
                if uid in seen:
                    continue
                seen.add(uid)
                planned.append((uid, outcome, amount))

        granted: list[GrantedReward] = []
        for uid, outcome, amount in planned:
            async with self.bot.db.session() as s:
                claim = MiniGameRewardClaim(
                    guild_id=result.guild_id,
                    game_type=result.game_type,
                    session_id=result.session_id,
                    user_id=uid,
                    outcome=outcome,
                    amount=amount,
                )
                s.add(claim)
                try:
                    await s.commit()
                except IntegrityError:
                    await s.rollback()
                    continue  # already paid for this outcome in this session
            await self.wallet.adjust(
                result.guild_id,
                uid,
                amount,
                f"{REASON_MINIGAME_PREFIX}{result.game_type}_{outcome}",
                description=(
                    f"{result.game_type} {outcome} reward "
                    f"(session #{result.session_id})"
                ),
                metadata={
                    "game_type": result.game_type,
                    "session_id": result.session_id,
                    "outcome": outcome,
                },
            )
            # Pet stage buff — silent bonus on top of the base reward
            # when the player has an active pet. Logged as a separate
            # CoinTransaction (REASON_PET_BUFF) so the /economy ledger
            # explains the extra coin without needing UI changes in
            # every minigame.
            buff = await self.wallet.apply_pet_minigame_buff(
                result.guild_id, uid, amount,
                result.game_type,
                session_id=result.session_id, outcome=outcome,
            )
            granted.append(
                GrantedReward(
                    user_id=uid, outcome=outcome, amount=amount + buff,
                )
            )
        return granted


# =============================================================================
# Permission helper
# =============================================================================


def is_economy_owner(member) -> bool:
    """Strict owner-only check. Replaced the older can_manage_economy
    (which also accepted Manage Server + custom manager roles) per the
    new policy: economy is set up exclusively by the server owner so
    coin grants, ledger edits, and leaderboard placement can't be
    influenced by anyone the owner appointed for unrelated reasons.

    The cfg-based manager_role_ids column is now inert — kept on the
    schema so the additive-only DB policy holds and old rows aren't
    lost, but no command path reads it any more."""
    if member is None or not hasattr(member, "guild"):
        return False
    return member.guild.owner_id == member.id


def can_manage_economy(member, cfg: GuildEconomyConfig | None) -> bool:
    """Deprecated alias kept for any out-of-tree callers. Forwards to
    is_economy_owner — Manage Server + manager_role_ids no longer
    grant economy admin."""
    del cfg  # no longer consulted
    return is_economy_owner(member)


def collect_text_users(values: Iterable) -> list[int]:
    """Coerce Discord user-like objects into their ids."""
    out: list[int] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, int):
            out.append(v)
        elif hasattr(v, "id"):
            out.append(v.id)
    return out
