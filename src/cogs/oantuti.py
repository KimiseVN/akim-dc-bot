"""Oẳn Tù Tì (Rock-Paper-Scissors) 1v1 minigame.

Game flow
---------
1. Challenger runs /oantuti start opponent:@X rounds:N bet:M
2. Bot posts a public challenge embed with Accept / Decline buttons
3. Opponent accepts → if bet > 0, both wallets are debited (escrow).
   On insufficient balance, the game is cancelled and any partial
   debit is refunded.
4. Per-round phase: 3 buttons (Rock / Paper / Scissors). Each player picks
   exactly once per round. The first pick is hidden until the second
   pick lands; then the embed reveals both choices + round winner and
   pauses briefly before advancing to the next round.
5. After N rounds: winner gets 2× bet, loser gets 0; tie → both
   refunded their bet. The 'oantuti_finished' event is dispatched with
   the OantutiGame row so tournament integration can react.

Tournament integration
---------------------
A game whose `tournament_match_id` is non-NULL was started by the
tournament cog. The bet is always 0 in that case (tournament rewards
come from the bracket top-3 pool), and the dispatched event carries
the FK so the tournament cog can mark the corresponding TournamentMatch
as decided.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from ..db.models import (
    GameWinBoard,
    GuildConfig,
    GuildEconomyConfig,
    OantutiGame,
    OantutiRound,
)
from ..economy.service import InsufficientBalanceError, WalletService
from ..i18n import (
    LANG_BUTTON_EMOJI,
    LANG_BUTTON_LABEL,
    LANG_EN,
    LANG_RU,
    LANG_VI,
    next_lang,
    normalize_lang,
)

if TYPE_CHECKING:
    from ..bot import AkimBot

log = logging.getLogger("akim-bot.oantuti")


CHOICES = ("bua", "bao", "keo")
CHOICE_LABEL = {"bua": "Búa", "bao": "Bao", "keo": "Kéo"}
CHOICE_LABEL_EN = {"bua": "Rock", "bao": "Paper", "keo": "Scissors"}
DEFAULT_ICON = {"bua": "🔨", "bao": "📃", "keo": "✂️"}


def _choice_label(choice: str, lang: str) -> str:
    return (
        CHOICE_LABEL_EN[choice] if normalize_lang(lang) == LANG_EN
        else CHOICE_LABEL[choice]
    )


_I18N: dict[str, dict[str, str]] = {
    LANG_VI: {
        # button labels
        "btn_accept":  "Chấp nhận",
        "btn_decline": "Từ chối",
        "btn_rematch": "Chơi lại",
        "btn_restart": "Chơi lại",
        "btn_endgame": "Kết thúc",
        # challenge embed
        "chal_title":  "🥊 Oẳn Tù Tì — Lời thách đấu",
        "chal_bet_line":     "💰 Cược: **{amount}** {coin}",
        "chal_no_bet_line":  "🎉 Chơi vui (không cược)",
        "chal_desc": (
            "🎯 **{rounds}** ván • {bet_text}\n\n"
            "🅰️ <@{a}> ✅ _đã sẵn sàng (người thách đấu)_\n"
            "🅱️ <@{b}> ⏳ — bấm **✅ Chấp nhận** để vào trận, "
            "hoặc **❌ Từ chối** để bỏ qua.\n\n"
            "_(Chỉ người được thách đấu mới cần bấm nút.)_"
        ),
        "footer_icons": "{i_bua} Búa  •  {i_bao} Bao  •  {i_keo} Kéo",
        # round embed
        "round_title":       "🥊 Ván {i}/{n}",
        "round_title_tie":   "  •  🤝 Hòa",
        "round_title_a_win": "  •  🏆 A thắng",
        "round_title_b_win": "  •  🏆 B thắng",
        "round_field_a":     "🅰️ Player A",
        "round_field_vs":    "🆚",
        "round_field_b":     "🅱️ Player B",
        "round_state_pending":  "⏳ đang chọn...",
        "round_state_picked":   "✅ đã chọn",
        "round_bet_field":     "💰 Tiền cược",
        "round_bet_body": (
            "Mỗi người đặt cọc: **{bet}** {coin}\n"
            "Tổng pot: **{pot}** {coin} → người thắng lấy hết"
        ),
        "round_history_field": "📜 Lịch sử các ván",
        "round_history_line":  "`Ván {i}:` {a} vs {b} → {arrow}",
        "round_hist_tie":  "🤝 hòa",
        "round_hist_a":    "🏆 A",
        "round_hist_b":    "🏆 B",
        "round_pick_hint": (
            "Bấm 1 trong 3 lựa chọn dưới đây — đối thủ sẽ không thấy lựa "
            "chọn cho đến khi cả 2 đã chọn xong."
        ),
        "round_reveal_tie":       "🤝 Hai bên ra cùng loại, ván này hòa.",
        "round_reveal_early_win": (
            "🏆 <@{winner}> thắng ván {i}! "
            "Đối phương không thể gỡ lại — trận đấu kết thúc sớm."
        ),
        "round_reveal_win": (
            "🏆 <@{winner}> thắng ván {i}!\n"
            "⏭ Ván kế tiếp sau {pause}s..."
        ),
        "round_footer_bet":    "💰 {bet} {coin}/người",
        "round_footer_no_bet": "🎉 chơi vui",
        "round_footer": "{bet_text}  •  Best of {n}  •  Cần {need} ván để thắng",
        # final embed
        "final_title_tie":  "🤝 Oẳn Tù Tì — Hòa",
        "final_title_win":  "🏆 Oẳn Tù Tì — Kết quả",
        "final_result_tie": (
            "🅰️ <@{a}>  vs  🅱️ <@{b}>\n"
            "Tỉ số: **{sa} - {sb}** — không ai thắng."
        ),
        "final_early_tag": "  •  ⚡ kết thúc sớm",
        "final_result_win": (
            "🅰️ <@{a}>  vs  🅱️ <@{b}>\n"
            "🏆 <@{winner}> thắng!{early}\n"
            "Tỉ số: **{sa} - {sb}**"
        ),
        "prize_breakdown_field": "💰 Chi tiết giải thưởng",
        "prize_breakdown_tie": (
            "• Mỗi người đặt cọc: **{bet}** {coin}\n"
            "• Hòa → hoàn lại **{refund}** {coin}/người\n"
            "• Lãi/lỗ: ±0"
        ),
        "prize_breakdown_win": (
            "• Mỗi người đặt cọc: **{bet}** {coin}\n"
            "• Tổng pot: **{pot}** {coin}\n"
            "• 🏆 <@{winner}> nhận: **+{payout}** {coin} "
            "_(lãi ròng +{net})_\n"
            "• 💀 <@{loser}> mất: **-{bet}** {coin}"
        ),
        "prize_free_field": "🎉 Phần thưởng",
        "prize_free_body": (
            "_Trận chơi vui — không có cược, không có coin di chuyển._"
        ),
    },
    LANG_EN: {
        "btn_accept":  "Accept",
        "btn_decline": "Decline",
        "btn_rematch": "Rematch",
        "btn_restart": "Restart",
        "btn_endgame": "End",
        "chal_title":  "🥊 Rock-Paper-Scissors — Challenge",
        "chal_bet_line":     "💰 Bet: **{amount}** {coin}",
        "chal_no_bet_line":  "🎉 Friendly match (no bet)",
        "chal_desc": (
            "🎯 **{rounds}** rounds • {bet_text}\n\n"
            "🅰️ <@{a}> ✅ _ready (challenger)_\n"
            "🅱️ <@{b}> ⏳ — click **✅ Accept** to play, "
            "or **❌ Decline** to skip.\n\n"
            "_(Only the challenged player needs to click.)_"
        ),
        "footer_icons": "{i_bua} Rock  •  {i_bao} Paper  •  {i_keo} Scissors",
        "round_title":       "🥊 Round {i}/{n}",
        "round_title_tie":   "  •  🤝 Tie",
        "round_title_a_win": "  •  🏆 A wins",
        "round_title_b_win": "  •  🏆 B wins",
        "round_field_a":     "🅰️ Player A",
        "round_field_vs":    "🆚",
        "round_field_b":     "🅱️ Player B",
        "round_state_pending":  "⏳ picking...",
        "round_state_picked":   "✅ picked",
        "round_bet_field":     "💰 Bet",
        "round_bet_body": (
            "Each side stakes: **{bet}** {coin}\n"
            "Total pot: **{pot}** {coin} → winner takes all"
        ),
        "round_history_field": "📜 Round history",
        "round_history_line":  "`Round {i}:` {a} vs {b} → {arrow}",
        "round_hist_tie":  "🤝 tie",
        "round_hist_a":    "🏆 A",
        "round_hist_b":    "🏆 B",
        "round_pick_hint": (
            "Click one of the 3 options below — your opponent won't see "
            "your pick until both have chosen."
        ),
        "round_reveal_tie":       "🤝 Both picked the same — this round is a tie.",
        "round_reveal_early_win": (
            "🏆 <@{winner}> takes round {i}! "
            "The opponent can't come back — match ends early."
        ),
        "round_reveal_win": (
            "🏆 <@{winner}> takes round {i}!\n"
            "⏭ Next round in {pause}s..."
        ),
        "round_footer_bet":    "💰 {bet} {coin}/side",
        "round_footer_no_bet": "🎉 friendly",
        "round_footer": "{bet_text}  •  Best of {n}  •  Need {need} rounds to win",
        "final_title_tie":  "🤝 RPS — Tie",
        "final_title_win":  "🏆 RPS — Result",
        "final_result_tie": (
            "🅰️ <@{a}>  vs  🅱️ <@{b}>\n"
            "Score: **{sa} - {sb}** — no winner."
        ),
        "final_early_tag": "  •  ⚡ ended early",
        "final_result_win": (
            "🅰️ <@{a}>  vs  🅱️ <@{b}>\n"
            "🏆 <@{winner}> wins!{early}\n"
            "Score: **{sa} - {sb}**"
        ),
        "prize_breakdown_field": "💰 Prize breakdown",
        "prize_breakdown_tie": (
            "• Each stake: **{bet}** {coin}\n"
            "• Tie → refunded **{refund}** {coin}/side\n"
            "• Net: ±0"
        ),
        "prize_breakdown_win": (
            "• Each stake: **{bet}** {coin}\n"
            "• Total pot: **{pot}** {coin}\n"
            "• 🏆 <@{winner}> takes: **+{payout}** {coin} "
            "_(net +{net})_\n"
            "• 💀 <@{loser}> loses: **-{bet}** {coin}"
        ),
        "prize_free_field": "🎉 Prize",
        "prize_free_body": (
            "_Friendly match — no bet, no coins moved._"
        ),
    },
}


def _t(lang: str, key: str, **fmt: object) -> str:
    table = _I18N.get(normalize_lang(lang), _I18N[LANG_VI])
    return table[key].format(**fmt) if fmt else table[key]


def _game_lang(game: OantutiGame) -> str:
    return normalize_lang(getattr(game, "language", None))
# bua > keo, bao > bua, keo > bao
_BEATS = {"bua": "keo", "bao": "bua", "keo": "bao"}

_REVEAL_PAUSE_SECONDS = 4


def _decide_round(a: str, b: str) -> str:
    """Return 'a', 'b' or 'tie'."""
    if a == b:
        return "tie"
    return "a" if _BEATS[a] == b else "b"


async def _get_icons(bot: "AkimBot", guild_id: int) -> dict[str, str]:
    """Resolve the per-guild icons for bua/bao/keo, falling back to
    defaults if the admin hasn't configured them yet."""
    async with bot.db.session() as s:
        cfg = await s.get(GuildConfig, guild_id)
    return {
        "bua": (cfg.oantuti_icon_bua if cfg and cfg.oantuti_icon_bua else DEFAULT_ICON["bua"]),
        "bao": (cfg.oantuti_icon_bao if cfg and cfg.oantuti_icon_bao else DEFAULT_ICON["bao"]),
        "keo": (cfg.oantuti_icon_keo if cfg and cfg.oantuti_icon_keo else DEFAULT_ICON["keo"]),
    }


# =============================================================================
# Embed builders
# =============================================================================


def _build_challenge_embed(
    game: OantutiGame, icons: dict[str, str], coin_label: str
) -> discord.Embed:
    lang = _game_lang(game)
    bet_text = (
        _t(lang, "chal_bet_line",
           amount=f"{game.bet_amount:,}", coin=coin_label)
        if game.bet_amount > 0
        else _t(lang, "chal_no_bet_line")
    )
    embed = discord.Embed(
        title=_t(lang, "chal_title"),
        description=_t(
            lang, "chal_desc",
            rounds=game.rounds, bet_text=bet_text,
            a=game.player_a_id, b=game.player_b_id,
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(text=_t(
        lang, "footer_icons",
        i_bua=icons["bua"], i_bao=icons["bao"], i_keo=icons["keo"],
    ))
    return embed


def _wins_needed(rounds: int) -> int:
    """Best-of-N majority: need floor(N/2)+1 wins."""
    return rounds // 2 + 1


def _build_history_lines(
    rounds_list: list["OantutiRound"],
    icons: dict[str, str],
    lang: str = LANG_VI,
) -> list[str]:
    """One line per completed round: 'Round i: 🔨 vs 📃 → 🏆 B'."""
    lines: list[str] = []
    for r in rounds_list:
        if r.result is None or r.a_choice is None or r.b_choice is None:
            continue
        a_icon = icons.get(r.a_choice, "❓")
        b_icon = icons.get(r.b_choice, "❓")
        if r.result == "tie":
            arrow = _t(lang, "round_hist_tie")
        elif r.result == "a":
            arrow = _t(lang, "round_hist_a")
        else:
            arrow = _t(lang, "round_hist_b")
        lines.append(_t(
            lang, "round_history_line",
            i=r.round_index + 1, a=a_icon, b=b_icon, arrow=arrow,
        ))
    return lines


def _build_round_embed(
    game: OantutiGame,
    rnd: OantutiRound,
    icons: dict[str, str],
    *,
    reveal: bool = False,
    coin_label: str = "coin",
    history: list[OantutiRound] | None = None,
    early_end: bool = False,
) -> discord.Embed:
    """Embed for the in-progress phase. When reveal=True the round is
    fully decided and both choices are shown.

    history is the full list of OantutiRound rows for this game (in
    order). It's rendered as a single field under the player names.
    """
    lang = _game_lang(game)
    color = discord.Color.green() if reveal else discord.Color.blurple()
    title = _t(lang, "round_title", i=rnd.round_index + 1, n=game.rounds)
    if reveal:
        if rnd.result == "tie":
            title += _t(lang, "round_title_tie")
        elif rnd.result == "a":
            title += _t(lang, "round_title_a_win")
        else:
            title += _t(lang, "round_title_b_win")
    embed = discord.Embed(title=title, color=color)

    a_state = (
        _t(lang, "round_state_picked") if rnd.a_choice
        else _t(lang, "round_state_pending")
    )
    b_state = (
        _t(lang, "round_state_picked") if rnd.b_choice
        else _t(lang, "round_state_pending")
    )
    if reveal:
        a_state = f"{icons[rnd.a_choice]} **{_choice_label(rnd.a_choice, lang)}**"
        b_state = f"{icons[rnd.b_choice]} **{_choice_label(rnd.b_choice, lang)}**"

    embed.add_field(
        name=_t(lang, "round_field_a"),
        value=f"<@{game.player_a_id}>\n{a_state}",
        inline=True,
    )
    embed.add_field(
        name=_t(lang, "round_field_vs"),
        value=f"**{game.a_score} - {game.b_score}**",
        inline=True,
    )
    embed.add_field(
        name=_t(lang, "round_field_b"),
        value=f"<@{game.player_b_id}>\n{b_state}",
        inline=True,
    )

    if game.bet_amount > 0:
        pot = game.bet_amount * 2
        embed.add_field(
            name=_t(lang, "round_bet_field"),
            value=_t(
                lang, "round_bet_body",
                bet=f"{game.bet_amount:,}", pot=f"{pot:,}", coin=coin_label,
            ),
            inline=False,
        )

    if history:
        hist_lines = _build_history_lines(history, icons, lang=lang)
        if hist_lines:
            embed.add_field(
                name=_t(lang, "round_history_field"),
                value="\n".join(hist_lines)[:1024],
                inline=False,
            )

    if not reveal:
        embed.description = _t(lang, "round_pick_hint")
    else:
        if rnd.result == "tie":
            embed.description = _t(lang, "round_reveal_tie")
        else:
            winner_id = game.player_a_id if rnd.result == "a" else game.player_b_id
            if early_end:
                embed.description = _t(
                    lang, "round_reveal_early_win",
                    winner=winner_id, i=rnd.round_index + 1,
                )
            else:
                embed.description = _t(
                    lang, "round_reveal_win",
                    winner=winner_id, i=rnd.round_index + 1,
                    pause=_REVEAL_PAUSE_SECONDS,
                )
    if game.bet_amount > 0:
        bet_text = _t(
            lang, "round_footer_bet",
            bet=f"{game.bet_amount:,}", coin=coin_label,
        )
    else:
        bet_text = _t(lang, "round_footer_no_bet")
    embed.set_footer(text=_t(
        lang, "round_footer",
        bet_text=bet_text, n=game.rounds, need=_wins_needed(game.rounds),
    ))
    return embed


def _build_final_embed(
    game: OantutiGame,
    icons: dict[str, str],
    *,
    coin_label: str = "coin",
    payout_winner: int = 0,
    refunded: int = 0,
    history: list[OantutiRound] | None = None,
    early_end: bool = False,
) -> discord.Embed:
    lang = _game_lang(game)
    if game.is_tie:
        color = discord.Color.light_gray()
        title = _t(lang, "final_title_tie")
        result_line = _t(
            lang, "final_result_tie",
            a=game.player_a_id, b=game.player_b_id,
            sa=game.a_score, sb=game.b_score,
        )
    else:
        color = discord.Color.gold()
        title = _t(lang, "final_title_win")
        winner = game.winner_id
        early_tag = _t(lang, "final_early_tag") if early_end else ""
        result_line = _t(
            lang, "final_result_win",
            a=game.player_a_id, b=game.player_b_id,
            winner=winner, early=early_tag,
            sa=game.a_score, sb=game.b_score,
        )
    embed = discord.Embed(title=title, description=result_line, color=color)

    if game.bet_amount > 0:
        if game.is_tie:
            breakdown = _t(
                lang, "prize_breakdown_tie",
                bet=f"{game.bet_amount:,}", refund=f"{refunded:,}",
                coin=coin_label,
            )
        else:
            winner = game.winner_id
            loser = (
                game.player_b_id if winner == game.player_a_id
                else game.player_a_id
            )
            net_profit = payout_winner - game.bet_amount
            breakdown = _t(
                lang, "prize_breakdown_win",
                bet=f"{game.bet_amount:,}",
                pot=f"{game.bet_amount * 2:,}",
                winner=winner, payout=f"{payout_winner:,}",
                net=f"{net_profit:,}", loser=loser, coin=coin_label,
            )
        embed.add_field(
            name=_t(lang, "prize_breakdown_field"),
            value=breakdown, inline=False,
        )
    elif not game.is_tie and game.tournament_match_id is None:
        embed.add_field(
            name=_t(lang, "prize_free_field"),
            value=_t(lang, "prize_free_body"),
            inline=False,
        )

    if history:
        hist_lines = _build_history_lines(history, icons, lang=lang)
        if hist_lines:
            embed.add_field(
                name=_t(lang, "round_history_field"),
                value="\n".join(hist_lines)[:1024],
                inline=False,
            )
    embed.set_footer(text=_t(
        lang, "footer_icons",
        i_bua=icons["bua"], i_bao=icons["bao"], i_keo=icons["keo"],
    ))
    return embed


# =============================================================================
# Persistent buttons
# =============================================================================


class AcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:accept:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:accept:{game_id}",
                label=_t(lang, "btn_accept"),
                emoji="✅",
                style=discord.ButtonStyle.success,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "AcceptButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_accept(interaction, self.game_id)


class DeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:decline:(?P<game_id>\d+)",
):
    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:decline:{game_id}",
                label=_t(lang, "btn_decline"),
                emoji="❌",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "DeclineButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_decline(interaction, self.game_id)


class ChoiceButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:pick:(?P<game_id>\d+):(?P<choice>bua|bao|keo)",
):
    def __init__(
        self, game_id: int, choice: str, *,
        label: str, emoji: str | None, disabled: bool = False,
    ) -> None:
        self.game_id = game_id
        self.choice = choice
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:pick:{game_id}:{choice}",
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "ChoiceButton":
        return cls(
            int(match["game_id"]), match["choice"],
            label=CHOICE_LABEL[match["choice"]], emoji=DEFAULT_ICON[match["choice"]],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_pick(interaction, self.game_id, self.choice)


class RematchButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:rematch:(?P<game_id>\d+)",
):
    """Posted on the final embed of a finished /oantuti game. Either of
    the two original players can click to spawn a fresh challenge with
    the SAME rounds + bet. The clicker becomes the new challenger.

    Idempotent: OantutiGame.rematch_game_id is set after the first
    successful click; subsequent clicks are answered with a pointer
    to the existing rematch.
    """

    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:rematch:{game_id}",
                label=_t(lang, "btn_rematch"),
                emoji="🔁",
                style=discord.ButtonStyle.primary,
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "RematchButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_rematch(interaction, self.game_id)


class RestartButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:restart:(?P<game_id>\d+)",
):
    """Mid-game restart. Either player can click to wipe the score +
    rounds and start over from round 1 of the same game record. The
    bet stays escrowed — we deliberately don't refund/re-debit so a
    user can't grind bond-style coin abuse by escrow-refund cycling.
    Disabled in tournament matches (bracket logic owns lifecycle)."""

    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:restart:{game_id}",
                label=_t(lang, "btn_restart"),
                emoji="🔄",
                style=discord.ButtonStyle.secondary,
                disabled=disabled,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "RestartButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_restart(interaction, self.game_id)


class EndGameButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:endgame:(?P<game_id>\d+)",
):
    """Manual end-mid-game. Either player can click to abandon the
    match — bet is refunded to both players. Disabled in tournament
    matches (a forfeit there has to go through the bracket cog)."""

    def __init__(
        self, game_id: int, *,
        disabled: bool = False, lang: str = LANG_VI,
    ) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:endgame:{game_id}",
                label=_t(lang, "btn_endgame"),
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                disabled=disabled,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "EndGameButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_endgame(interaction, self.game_id)


class VodichBoardLangButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:board_lang:(?P<guild_id>\d+)",
):
    """🌐 Toggle the champion leaderboard between RU and EN."""

    def __init__(self, guild_id: int, *, lang: str = LANG_RU) -> None:
        self.guild_id = guild_id
        label = "English" if lang == LANG_RU else "Русский"
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:board_lang:{guild_id}",
                label=label,
                emoji=LANG_BUTTON_EMOJI,
                style=discord.ButtonStyle.secondary,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "VodichBoardLangButton":
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_board_lang_toggle(interaction, self.guild_id)


class LangToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"oantuti:lang:(?P<game_id>\d+)",
):
    """🌐 Flip the card between VI and EN. Row 2 so it doesn't collide
    with the primary buttons (row 0) or the restart/endgame row (row 1)
    on the round view."""

    def __init__(self, game_id: int, *, lang: str = LANG_VI) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"oantuti:lang:{game_id}",
                label=LANG_BUTTON_LABEL[normalize_lang(lang)],
                emoji=LANG_BUTTON_EMOJI,
                style=discord.ButtonStyle.secondary,
                row=2,
            )
        )

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "LangToggleButton":
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Oantuti" = interaction.client.get_cog("Oantuti")  # type: ignore[assignment]
        if cog is None:
            return
        await cog.handle_lang_toggle(interaction, self.game_id)


def _build_challenge_view(
    game_id: int, *, disabled: bool = False, lang: str = LANG_VI,
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(AcceptButton(game_id, disabled=disabled, lang=lang))
    v.add_item(DeclineButton(game_id, disabled=disabled, lang=lang))
    v.add_item(LangToggleButton(game_id, lang=lang))
    return v


def _build_final_view(
    game_id: int, *, disabled: bool = False, lang: str = LANG_VI,
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(RematchButton(game_id, disabled=disabled, lang=lang))
    v.add_item(LangToggleButton(game_id, lang=lang))
    return v


class _MatchIdModal(discord.ui.Modal, title="View match detail"):
    """Owner pastes a game id to drill into. Validation is loose —
    must be digits; the cog method that consumes it does the
    guild-scope + existence check + sends a 'not found' response if
    it's a stale id, so we don't need to query here."""

    game_id_input = discord.ui.TextInput(
        label="Match ID (see /oantuti-admin history)",
        placeholder="e.g. 42",
        required=True,
        min_length=1,
        max_length=10,
    )

    def __init__(self, cog: "Oantuti") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.game_id_input.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "❌ ID must be a positive integer.", ephemeral=True,
            )
            return
        await self.cog._send_match_detail(interaction, int(raw))


class _MatchHistoryView(discord.ui.View):
    """Single 🔍 button under the /oantuti-admin history list — opens
    the id-input modal so owner doesn't have to re-type the slash
    command + game_id when they spot something in the list."""

    def __init__(self, cog: "Oantuti") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(
        label="View detail",
        emoji="🔍",
        style=discord.ButtonStyle.primary,
    )
    async def btn_detail(self, interaction, _btn) -> None:
        await interaction.response.send_modal(_MatchIdModal(self.cog))

    @discord.ui.button(
        label="Close",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def btn_close(self, interaction, _btn) -> None:
        try:
            await interaction.response.edit_message(
                content="_(History closed.)_",
                embed=None, view=None,
            )
        except discord.HTTPException:
            pass


class _MatchDetailView(discord.ui.View):
    """Owner-only action panel attached to /oantuti-admin match. Cancel
    appears only while the game is still 'running'; Delete is always
    available since it's the nuclear cleanup option. Both buttons
    defer to the cog's admin_cancel_match / admin_delete_match for
    the actual work — this view is dumb routing + a Close button.
    Ephemeral, timeout 5min — no DynamicItem registration needed."""

    def __init__(
        self,
        cog: "Oantuti",
        game_id: int,
        status: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.game_id = game_id
        if status == "running":
            cancel_btn = discord.ui.Button(
                label="Cancel & refund",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                row=0,
            )
            cancel_btn.callback = self._cancel_callback  # type: ignore[assignment]
            self.add_item(cancel_btn)
        delete_btn = discord.ui.Button(
            label="Delete permanently",
            emoji="🗑️",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        delete_btn.callback = self._delete_callback  # type: ignore[assignment]
        self.add_item(delete_btn)
        close_btn = discord.ui.Button(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        close_btn.callback = self._close_callback  # type: ignore[assignment]
        self.add_item(close_btn)

    async def _cancel_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        await self.cog.admin_cancel_match(interaction, self.game_id)

    async def _delete_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        await self.cog.admin_delete_match(interaction, self.game_id)

    async def _close_callback(
        self, interaction: discord.Interaction,
    ) -> None:
        try:
            await interaction.response.edit_message(
                content="_(Match panel closed.)_",
                embed=None, view=None,
            )
        except discord.HTTPException:
            pass


def _build_round_view(
    game_id: int, icons: dict[str, str], *, disabled: bool = False,
    show_session_controls: bool = True, lang: str = LANG_VI,
) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for c in CHOICES:
        # Custom emojis stored as "<:name:id>" can't be passed to button.emoji
        # directly; render them via the label fallback when not unicode.
        emoji = icons[c] if len(icons[c]) <= 4 else None
        choice_name = _choice_label(c, lang)
        label = choice_name if emoji else f"{icons[c]} {choice_name}"
        v.add_item(ChoiceButton(
            game_id, c, label=label, emoji=emoji, disabled=disabled,
        ))
    # Row 1 — session controls (restart / end). Caller passes
    # show_session_controls=False for tournament matches since the
    # bracket cog owns lifecycle there; non-tournament matches always
    # render them so either player can abandon or reset.
    if show_session_controls:
        v.add_item(RestartButton(game_id, disabled=disabled, lang=lang))
        v.add_item(EndGameButton(game_id, disabled=disabled, lang=lang))
    # Row 2 — language toggle. Always available so either player can
    # switch the card language mid-game (display only, doesn't affect
    # the game state).
    v.add_item(LangToggleButton(game_id, lang=lang))
    return v


# =============================================================================
# Cog
# =============================================================================


class Oantuti(commands.Cog):
    def __init__(self, bot: "AkimBot") -> None:
        self.bot = bot
        # game_id -> asyncio.Lock — prevents two simultaneous picks racing
        # on the "is round complete?" check.
        self._locks: dict[int, asyncio.Lock] = {}
        # (game_id, round_index) -> list of ephemeral pick-ack interactions
        # to delete when the round reveals. Tokens expire after 15 min so
        # any reasonable round duration is fine.
        self._pick_acks: dict[tuple[int, int], list[discord.Interaction]] = {}

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and self.bot.is_owner(interaction.user.id, interaction.guild.owner_id)
        )

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if self._is_owner(interaction):
            return True
        msg = "❌ Only the **server owner** can use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    def _lock(self, game_id: int) -> asyncio.Lock:
        if game_id not in self._locks:
            self._locks[game_id] = asyncio.Lock()
        return self._locks[game_id]

    # ---- /oantuti group (user-facing) ----
    oantuti = app_commands.Group(
        name="oantuti",
        description="Rock-Paper-Scissors (Oan Tu Ti) 1v1 minigame",
        guild_only=True,
    )

    # ---- /oantuti-admin group (owner-only) ----
    # Discord can't gate subcommands separately from their group, so
    # admin commands live in a sibling group locked to Administrator
    # at the UI level + _require_owner at runtime. Per-command
    # default_permissions on subcommands was a no-op (Discord only
    # honours group/top-level perms), so those decorators get dropped.
    oantuti_admin = app_commands.Group(
        name="oantuti-admin",
        description="(Owner) Manage Oan Tu Ti: setup, hall of fame",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    # ---------- /oantuti-admin setup ----------

    @oantuti_admin.command(
        name="setup",
        description="(Owner) Configure /oantuti: icons, roles, channels, hall of fame",
    )
    @app_commands.describe(
        icon_bua="Icon for Rock (e.g. 🔨 or <:name:id>)",
        icon_bao="Icon for Paper",
        icon_keo="Icon for Scissors",
        channel="Default channel to post /oantuti start into",
        announce_channel="Hall-of-fame channel — posts each match's winner",
        creator_role1="Role allowed to run /oantuti start (leave empty = anyone)",
        creator_role2="(optional)",
        creator_role3="(optional)",
    )
    async def oantuti_setup(
        self,
        interaction: discord.Interaction,
        icon_bua: app_commands.Range[str, 1, 64] | None = None,
        icon_bao: app_commands.Range[str, 1, 64] | None = None,
        icon_keo: app_commands.Range[str, 1, 64] | None = None,
        channel: discord.TextChannel | None = None,
        announce_channel: discord.TextChannel | None = None,
        creator_role1: discord.Role | None = None,
        creator_role2: discord.Role | None = None,
        creator_role3: discord.Role | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                s.add(cfg)
            if icon_bua is not None:
                cfg.oantuti_icon_bua = icon_bua
            if icon_bao is not None:
                cfg.oantuti_icon_bao = icon_bao
            if icon_keo is not None:
                cfg.oantuti_icon_keo = icon_keo
            if channel is not None:
                cfg.oantuti_channel_id = channel.id
            if announce_channel is not None:
                cfg.oantuti_announce_channel_id = announce_channel.id
            roles = [r for r in (creator_role1, creator_role2, creator_role3) if r]
            if roles:
                seen: set[int] = set()
                kept: list[str] = []
                for r in roles:
                    if r.id in seen:
                        continue
                    seen.add(r.id)
                    kept.append(str(r.id))
                cfg.oantuti_creator_role_ids = ",".join(kept)
            await s.commit()
            cfg = await s.get(GuildConfig, interaction.guild_id)

        assert cfg is not None
        icons = {
            "bua": cfg.oantuti_icon_bua or DEFAULT_ICON["bua"],
            "bao": cfg.oantuti_icon_bao or DEFAULT_ICON["bao"],
            "keo": cfg.oantuti_icon_keo or DEFAULT_ICON["keo"],
        }
        ch = f"<#{cfg.oantuti_channel_id}>" if cfg.oantuti_channel_id else "—"
        ach = (
            f"<#{cfg.oantuti_announce_channel_id}>"
            if cfg.oantuti_announce_channel_id else "—"
        )
        crs = cfg.oantuti_creator_ids()
        cr_text = (
            ", ".join(f"<@&{x}>" for x in crs) if crs
            else "_anyone_"
        )
        embed = discord.Embed(
            title="🥊 /oantuti configuration", color=discord.Color.blurple()
        )
        embed.add_field(
            name="Icons",
            value=f"{icons['bua']} Rock  •  {icons['bao']} Paper  •  {icons['keo']} Scissors",
            inline=False,
        )
        embed.add_field(name="Default channel", value=ch, inline=True)
        embed.add_field(name="Hall-of-fame channel", value=ach, inline=True)
        embed.add_field(name="Creator roles", value=cr_text, inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- /oantuti start ----------

    @oantuti.command(
        name="start",
        description="Challenge another player to a Rock-Paper-Scissors match",
    )
    @app_commands.describe(
        opponent="Player to challenge",
        rounds="Number of rounds (1-15)",
        bet="Coins each side stakes (0 = friendly match)",
    )
    async def oantuti_start(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
        rounds: app_commands.Range[int, 1, 15] = 3,
        bet: app_commands.Range[int, 0, 1_000_000] = 0,
    ) -> None:
        assert interaction.guild_id is not None
        if opponent.bot:
            await interaction.response.send_message(
                "❌ You can't challenge a bot.", ephemeral=True
            )
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You can't challenge yourself.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, interaction.guild_id)

        # Permission check (creator role list)
        if isinstance(interaction.user, discord.Member) and cfg is not None:
            allowed = cfg.oantuti_creator_ids()
            if allowed:
                user_role_ids = {r.id for r in interaction.user.roles}
                is_admin = interaction.user.guild_permissions.manage_guild
                if not (user_role_ids & set(allowed)) and not is_admin:
                    await interaction.response.send_message(
                        "❌ You don't have a role allowed to start /oantuti.",
                        ephemeral=True,
                    )
                    return

        # Resolve target channel — explicit channel from config or current
        target_channel: discord.TextChannel
        if cfg and cfg.oantuti_channel_id and interaction.guild:
            ch = interaction.guild.get_channel(cfg.oantuti_channel_id)
            if isinstance(ch, discord.TextChannel):
                target_channel = ch
            elif isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message(
                    "❌ Could not determine a channel to post the game.",
                    ephemeral=True,
                )
                return
        elif isinstance(interaction.channel, discord.TextChannel):
            target_channel = interaction.channel
        else:
            await interaction.response.send_message(
                "❌ Could not determine a channel to post the game.",
                ephemeral=True,
            )
            return

        # Create the game row
        async with self.bot.db.session() as s:
            game = OantutiGame(
                guild_id=interaction.guild_id,
                channel_id=target_channel.id,
                player_a_id=interaction.user.id,
                player_b_id=opponent.id,
                rounds=int(rounds),
                bet_amount=int(bet),
                status="invited",
            )
            s.add(game)
            await s.commit()
            await s.refresh(game)
            game_id = game.id

        icons = await _get_icons(self.bot, interaction.guild_id)
        coin_label = await self._coin_label(interaction.guild_id)
        embed = _build_challenge_embed(game, icons, coin_label)
        view = _build_challenge_view(game_id, lang=_game_lang(game))
        try:
            msg = await target_channel.send(
                content=f"<@{opponent.id}>",
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(users=[opponent]),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Bot doesn't have permission to post in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            row = await s.get(OantutiGame, game_id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()

        if target_channel.id == interaction.channel_id:
            await interaction.response.send_message(
                f"✅ Challenge #{game_id} sent ↑", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Challenge #{game_id} sent in {target_channel.mention}",
                ephemeral=True,
            )

    # ---------- language toggle ----------

    async def handle_lang_toggle(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        """Flip the card language and redraw whatever embed the game is
        currently showing (challenge / round / final). Language is a
        display concern — either player, or any observer, can toggle."""
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None:
                await interaction.followup.send(
                    "Game không tồn tại. / Game not found.",
                    ephemeral=True,
                )
                return
            new_lang = next_lang(game.language)
            game.language = new_lang
            await s.commit()
        await self._refresh_card(game_id)
        ack_map = {
            LANG_VI: "🌐 Chuyển sang **Tiếng Việt**.",
            LANG_EN: "🌐 Switched to **English**.",
        }
        await interaction.followup.send(ack_map[new_lang], ephemeral=True)

    async def handle_board_lang_toggle(
        self, interaction: discord.Interaction, guild_id: int,
    ) -> None:
        await interaction.response.defer()
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
            if board is None:
                await interaction.followup.send("❌ Board not found.", ephemeral=True)
                return
            new_lang = LANG_EN if getattr(board, "language", LANG_RU) == LANG_RU else LANG_RU
            board.language = new_lang
            display_count = board.display_count
            await s.commit()
        embed = await self._build_vodich_embed(guild_id, display_count, lang=new_lang)
        view = await self._build_vodich_view(guild_id, lang=new_lang)
        await interaction.edit_original_response(
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _refresh_card(self, game_id: int) -> None:
        """Redraw the game card in whatever status it's currently in.
        Shared by the language toggle so we don't have to rebuild the
        transition-specific edit path just to flip a display flag."""
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None:
                return
            history_rows = await s.execute(
                select(OantutiRound)
                .where(OantutiRound.game_id == game_id)
                .order_by(OantutiRound.round_index)
            )
            history = list(history_rows.scalars().all())
            current = None
            if game.status == "running":
                current = await s.scalar(
                    select(OantutiRound).where(
                        OantutiRound.game_id == game_id,
                        OantutiRound.round_index == game.current_round,
                    )
                )
        msg = await self._fetch_message(game)
        if msg is None:
            return
        icons = await _get_icons(self.bot, game.guild_id)
        coin_label = await self._coin_label(game.guild_id)
        lang = _game_lang(game)
        try:
            if game.status == "invited":
                await msg.edit(
                    embed=_build_challenge_embed(game, icons, coin_label),
                    view=_build_challenge_view(game_id, lang=lang),
                )
            elif game.status == "running" and current is not None:
                await msg.edit(
                    embed=_build_round_embed(
                        game, current, icons, coin_label=coin_label,
                        history=history,
                    ),
                    view=_build_round_view(
                        game_id, icons,
                        show_session_controls=game.tournament_match_id is None,
                        lang=lang,
                    ),
                )
            elif game.status == "finished":
                attach_rematch = game.tournament_match_id is None
                await msg.edit(
                    embed=_build_final_embed(
                        game, icons, coin_label=coin_label,
                        history=history,
                    ),
                    view=(
                        _build_final_view(game_id, lang=lang)
                        if attach_rematch else None
                    ),
                )
            elif game.status == "cancelled":
                # Cancel embeds are one-shots created inline; leave the
                # existing embed and just refresh the (disabled) view.
                await msg.edit(view=_build_challenge_view(
                    game_id, disabled=True, lang=lang,
                ))
        except discord.HTTPException as e:
            log.warning("oantuti: language refresh edit failed: %s", e)

    # ---------- helpers ----------

    async def _coin_label(self, guild_id: int) -> str:
        async with self.bot.db.session() as s:
            ecfg = await s.get(GuildEconomyConfig, guild_id)
        return ecfg.coin_name if ecfg else "coin"

    async def _fetch_game(self, game_id: int) -> OantutiGame | None:
        async with self.bot.db.session() as s:
            return await s.get(OantutiGame, game_id)

    async def _fetch_message(
        self, game: OantutiGame
    ) -> discord.Message | None:
        if game.message_id is None:
            return None
        channel = self.bot.get_channel(game.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None
        try:
            return await channel.fetch_message(game.message_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    # ---------- accept / decline ----------

    async def handle_accept(
        self, interaction: discord.Interaction, game_id: int
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True
                )
                return
            if interaction.user.id != game.player_b_id:
                await interaction.response.send_message(
                    "❌ Only the challenged player can accept.", ephemeral=True
                )
                return
            if game.status != "invited":
                await interaction.response.send_message(
                    f"Game is already in status **{game.status}**.", ephemeral=True
                )
                return

        # Escrow bets (if any)
        guild_id = game.guild_id
        if game.bet_amount > 0:
            wallet_svc = WalletService(self.bot)
            paid: list[int] = []
            try:
                await wallet_svc.adjust(
                    guild_id, game.player_a_id, -game.bet_amount,
                    "minigame_oantuti_bet",
                    description=f"Bet for oantuti #{game_id}",
                    metadata={"game_id": game_id, "side": "a"},
                )
                paid.append(game.player_a_id)
                await wallet_svc.adjust(
                    guild_id, game.player_b_id, -game.bet_amount,
                    "minigame_oantuti_bet",
                    description=f"Bet for oantuti #{game_id}",
                    metadata={"game_id": game_id, "side": "b"},
                )
                paid.append(game.player_b_id)
            except InsufficientBalanceError as e:
                # Refund anyone who already paid
                for uid in paid:
                    try:
                        await wallet_svc.adjust(
                            guild_id, uid, game.bet_amount,
                            "minigame_oantuti_refund",
                            description=f"Refund oantuti #{game_id} insufficient balance",
                            metadata={"game_id": game_id},
                        )
                    except Exception:
                        log.exception(
                            "oantuti: refund failed for %s game %s", uid, game_id
                        )
                async with self.bot.db.session() as s:
                    g = await s.get(OantutiGame, game_id)
                    if g is not None:
                        g.status = "cancelled"
                        g.finished_at = datetime.now(timezone.utc)
                        await s.commit()
                await interaction.response.send_message(
                    f"❌ Not enough coins to place the bet: {e}. Game cancelled.",
                    ephemeral=True,
                )
                # Best-effort embed refresh to cancelled
                msg = await self._fetch_message(game)
                if msg is not None:
                    try:
                        await msg.edit(
                            embed=discord.Embed(
                                title="🥊 Rock-Paper-Scissors — Cancelled",
                                description=f"❌ {e}",
                                color=discord.Color.red(),
                            ),
                            view=_build_challenge_view(
                                game_id, disabled=True, lang=_game_lang(game),
                            ),
                        )
                    except discord.HTTPException:
                        pass
                return

        # Flip to running + create first round row
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
            if g is None or g.status != "invited":
                return
            g.status = "running"
            g.started_at = datetime.now(timezone.utc)
            g.current_round = 0
            s.add(OantutiRound(game_id=game_id, round_index=0))
            await s.commit()

        icons = await _get_icons(self.bot, guild_id)
        coin_label = await self._coin_label(guild_id)
        # Refresh public message into round 1
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
            rnd = await s.scalar(
                select(OantutiRound).where(
                    OantutiRound.game_id == game_id,
                    OantutiRound.round_index == 0,
                )
            )
        assert g is not None and rnd is not None
        embed = _build_round_embed(
            g, rnd, icons, coin_label=coin_label, history=[],
        )
        view = _build_round_view(
            game_id, icons,
            show_session_controls=g.tournament_match_id is None,
            lang=_game_lang(g),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_decline(
        self, interaction: discord.Interaction, game_id: int
    ) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None:
                await interaction.response.send_message(
                    "Game no longer exists.", ephemeral=True
                )
                return
            if interaction.user.id not in (game.player_b_id, game.player_a_id):
                await interaction.response.send_message(
                    "❌ Only the two players in the game can cancel it.", ephemeral=True
                )
                return
            if game.status != "invited":
                await interaction.response.send_message(
                    f"Game is already in status **{game.status}**.", ephemeral=True
                )
                return
            game.status = "cancelled"
            game.finished_at = datetime.now(timezone.utc)
            await s.commit()

        # No bet was charged before accept, so nothing to refund.
        embed = discord.Embed(
            title="🥊 Rock-Paper-Scissors — Cancelled",
            description=(
                f"<@{interaction.user.id}> declined the challenge."
            ),
            color=discord.Color.dark_gray(),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=_build_challenge_view(
                game_id, disabled=True, lang=_game_lang(game),
            ),
        )
        # Dispatch so tournament cog can react (forfeit)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
        if g is not None:
            self.bot.dispatch("oantuti_finished", g)

    # ---------- per-round pick ----------

    async def handle_pick(
        self, interaction: discord.Interaction, game_id: int, choice: str
    ) -> None:
        if choice not in CHOICES:
            await interaction.response.send_message(
                "Invalid choice.", ephemeral=True
            )
            return
        # Defer FIRST so we have 15 min instead of Discord's 3-second
        # window for the first response. The previous version held the
        # game lock through a slow msg.edit; when both players clicked
        # within seconds the second click couldn't even acquire the
        # lock before Discord timed its interaction out — bot wrote
        # nothing for that player and the round hung at "picking".
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return

        # Lock scope is now JUST the DB pick write. Discord API calls
        # (msg.edit, _advance_round) run outside the lock so a slow edit
        # never delays another player's pick.
        error_msg: str | None = None
        both_picked = False
        guild_id: int | None = None
        current_round: int | None = None
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None:
                    error_msg = "Game no longer exists."
                elif game.status != "running":
                    error_msg = (
                        f"Game is not running ({game.status})."
                    )
                elif interaction.user.id not in (
                    game.player_a_id, game.player_b_id,
                ):
                    error_msg = "❌ Only the two players in the game can click."
                else:
                    rnd = await s.scalar(
                        select(OantutiRound).where(
                            OantutiRound.game_id == game_id,
                            OantutiRound.round_index == game.current_round,
                        )
                    )
                    if rnd is None:
                        error_msg = "Current round not found."
                    else:
                        side = (
                            "a" if interaction.user.id == game.player_a_id
                            else "b"
                        )
                        if side == "a" and rnd.a_choice is not None:
                            error_msg = (
                                f"❌ You already picked "
                                f"**{_choice_label(rnd.a_choice, _game_lang(game))}** "
                                "for this round."
                            )
                        elif side == "b" and rnd.b_choice is not None:
                            error_msg = (
                                f"❌ You already picked "
                                f"**{_choice_label(rnd.b_choice, _game_lang(game))}** "
                                "for this round."
                            )
                        else:
                            if side == "a":
                                rnd.a_choice = choice
                            else:
                                rnd.b_choice = choice
                            await s.commit()
                            both_picked = (
                                rnd.a_choice is not None
                                and rnd.b_choice is not None
                            )
                            guild_id = game.guild_id
                            current_round = game.current_round

        # ===== LOCK RELEASED =====

        if error_msg is not None:
            try:
                await interaction.edit_original_response(content=error_msg)
            except discord.HTTPException:
                pass
            return

        assert guild_id is not None and current_round is not None
        # Ack the pick. Tracked in _pick_acks so the reveal cleanup can
        # delete this ephemeral when both players have picked.
        try:
            await interaction.edit_original_response(
                content=(
                    f"✅ Picked **{CHOICE_LABEL_EN[choice]}** — "
                    + (
                        "revealing result."
                        if both_picked else "waiting for opponent."
                    )
                )
            )
        except discord.HTTPException:
            pass
        self._pick_acks.setdefault(
            (game_id, current_round), []
        ).append(interaction)

        if both_picked:
            # Reveal + advance happens outside the lock so it can't
            # back-pressure the next player's pick.
            await self._advance_round(game_id)
            return

        # Single-pick path: refresh the public embed to show ✅ for
        # this player. Re-check both_picked here — the OTHER player
        # may have raced our refresh and picked between our DB write
        # and this fetch, in which case _advance_round (called from
        # their handler) will paint the reveal embed; we'd just
        # overwrite it with a stale "both ✅ no reveal" edit. Skip.
        icons = await _get_icons(self.bot, guild_id)
        coin_label = await self._coin_label(guild_id)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
            if g is None:
                return
            fresh = await s.scalar(
                select(OantutiRound).where(
                    OantutiRound.game_id == game_id,
                    OantutiRound.round_index == g.current_round,
                )
            )
            if (
                fresh is not None
                and fresh.a_choice is not None
                and fresh.b_choice is not None
            ):
                # Race — the other player already locked in. Leave the
                # reveal to _advance_round (it owns the embed now).
                return
            history_rows = await s.execute(
                select(OantutiRound)
                .where(OantutiRound.game_id == game_id)
                .order_by(OantutiRound.round_index)
            )
            history = list(history_rows.scalars().all())
        msg = await self._fetch_message(g)
        if msg is not None and fresh is not None:
            try:
                await msg.edit(
                    embed=_build_round_embed(
                        g, fresh, icons, coin_label=coin_label,
                        history=history,
                    ),
                    view=_build_round_view(
                        game_id, icons,
                        show_session_controls=g.tournament_match_id is None,
                        lang=_game_lang(g),
                    ),
                )
            except discord.HTTPException:
                pass

    # ---------- restart / end (mid-game session controls) ----------

    async def handle_restart(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        """Wipe scores + rounds, restart from round 1. Bet stays
        escrowed — refunding then re-debiting on the fresh game would
        let players grind transactions (and risk InsufficientBalance
        if either spent the refund). Tournament matches reject."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id: int | None = None
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None:
                    await interaction.edit_original_response(
                        content="❌ Game no longer exists.",
                    )
                    return
                if game.status != "running":
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Game is not running "
                            f"({game.status})."
                        ),
                    )
                    return
                if interaction.user.id not in (
                    game.player_a_id, game.player_b_id,
                ):
                    await interaction.edit_original_response(
                        content="❌ Only the two players in the game can click.",
                    )
                    return
                if game.tournament_match_id is not None:
                    await interaction.edit_original_response(
                        content=(
                            "❌ Cannot restart a tournament match — "
                            "the bracket cog owns its lifecycle."
                        ),
                    )
                    return
                # Wipe round rows + reset scores + create fresh round 0.
                await s.execute(
                    delete(OantutiRound).where(
                        OantutiRound.game_id == game_id,
                    )
                )
                game.a_score = 0
                game.b_score = 0
                game.current_round = 0
                s.add(OantutiRound(game_id=game_id, round_index=0))
                await s.commit()
                await s.refresh(game)
                guild_id = game.guild_id

        # Acks for any in-flight picks of the previous round set become
        # stale — clean them up best-effort so the players' chats stay
        # consistent with the fresh game state.
        for round_index in range(0, 16):
            await self._dismiss_pick_acks(game_id, round_index)

        assert guild_id is not None
        icons = await _get_icons(self.bot, guild_id)
        coin_label = await self._coin_label(guild_id)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
            rnd = await s.scalar(
                select(OantutiRound).where(
                    OantutiRound.game_id == game_id,
                    OantutiRound.round_index == 0,
                )
            )
        msg = await self._fetch_message(g) if g is not None else None
        if msg is not None and g is not None and rnd is not None:
            try:
                await msg.edit(
                    embed=_build_round_embed(
                        g, rnd, icons, coin_label=coin_label, history=[],
                    ),
                    view=_build_round_view(
                        game_id, icons,
                        show_session_controls=g.tournament_match_id is None,
                        lang=_game_lang(g),
                    ),
                )
            except discord.HTTPException:
                pass
        await interaction.edit_original_response(
            content=(
                f"🔄 <@{interaction.user.id}> restarted the match — "
                "back to round 1, score 0-0."
            ),
        )

    async def handle_endgame(
        self, interaction: discord.Interaction, game_id: int,
    ) -> None:
        """Manual cancel mid-game. Mark cancelled, refund the bet to
        both players if any, dispatch oantuti_finished so anyone
        listening can react, and gray out the embed."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        a_id: int | None = None
        b_id: int | None = None
        bet: int = 0
        guild_id: int | None = None
        last_round_idx: int | None = None
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None:
                    await interaction.edit_original_response(
                        content="❌ Game no longer exists.",
                    )
                    return
                if game.status != "running":
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Game is not running "
                            f"({game.status})."
                        ),
                    )
                    return
                if interaction.user.id not in (
                    game.player_a_id, game.player_b_id,
                ):
                    await interaction.edit_original_response(
                        content="❌ Only the two players in the game can click.",
                    )
                    return
                if game.tournament_match_id is not None:
                    await interaction.edit_original_response(
                        content=(
                            "❌ Tournament matches must be handled by the "
                            "bracket cog — you can't end them manually."
                        ),
                    )
                    return
                game.status = "cancelled"
                game.finished_at = datetime.now(timezone.utc)
                game.is_tie = False
                await s.commit()
                await s.refresh(game)
                a_id = game.player_a_id
                b_id = game.player_b_id
                bet = int(game.bet_amount)
                guild_id = game.guild_id
                last_round_idx = game.current_round

        assert (
            a_id is not None and b_id is not None and guild_id is not None
        )
        # Refund outside the lock — wallet adjust is its own DB session.
        refund_ok = True
        if bet > 0:
            wallet_svc = WalletService(self.bot)
            for uid in (a_id, b_id):
                try:
                    await wallet_svc.adjust(
                        guild_id, uid, bet,
                        "minigame_oantuti_refund",
                        description=(
                            f"End oantuti match #{game_id} — "
                            "refund bet"
                        ),
                        metadata={
                            "game_id": game_id, "reason": "manual_end",
                        },
                    )
                except Exception:
                    refund_ok = False
                    log.exception(
                        "oantuti manual-end refund failed: "
                        "guild=%s user=%s", guild_id, uid,
                    )
        if last_round_idx is not None:
            await self._dismiss_pick_acks(game_id, last_round_idx)

        coin_label = await self._coin_label(guild_id)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
        msg = await self._fetch_message(g) if g is not None else None
        refund_line = ""
        if bet > 0:
            if refund_ok:
                refund_line = (
                    f"\n💰 Refunded **{bet:,}** {coin_label} "
                    "to both players."
                )
            else:
                refund_line = (
                    f"\n⚠️ Bet refund failed — please contact an admin "
                    f"(bet: **{bet:,}** {coin_label})."
                )
        cancel_embed = discord.Embed(
            title="🛑 Match ended",
            description=(
                f"<@{interaction.user.id}> ended the match early.{refund_line}"
            ),
            color=discord.Color.dark_gray(),
        )
        if msg is not None:
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except discord.HTTPException:
                pass
        await interaction.edit_original_response(
            content=(
                "🛑 Match ended."
                + (
                    f" Refunded **{bet:,}** {coin_label} to both players."
                    if bet > 0 and refund_ok else
                    ""
                )
            ),
        )
        if g is not None:
            self.bot.dispatch("oantuti_finished", g)

    async def _dismiss_pick_acks(self, game_id: int, round_index: int) -> None:
        """Delete the 'Picked X' ephemeral messages for a round so the
        choice prompt doesn't linger in players' chats after reveal."""
        for ack in self._pick_acks.pop((game_id, round_index), []):
            try:
                await ack.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                pass

    async def _advance_round(self, game_id: int) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None or game.status != "running":
                return
            rnd = await s.scalar(
                select(OantutiRound).where(
                    OantutiRound.game_id == game_id,
                    OantutiRound.round_index == game.current_round,
                )
            )
            if rnd is None or rnd.a_choice is None or rnd.b_choice is None:
                return
            result = _decide_round(rnd.a_choice, rnd.b_choice)
            rnd.result = result
            rnd.completed_at = datetime.now(timezone.utc)
            if result == "a":
                game.a_score += 1
            elif result == "b":
                game.b_score += 1
            await s.commit()
            await s.refresh(game)
            await s.refresh(rnd)
            history_rows = await s.execute(
                select(OantutiRound)
                .where(OantutiRound.game_id == game_id)
                .order_by(OantutiRound.round_index)
            )
            history = list(history_rows.scalars().all())

        # Auto-dismiss the per-player "Picked X" ephemerals right at
        # reveal so chats stay clean for the next round / scoreboard.
        await self._dismiss_pick_acks(game_id, rnd.round_index)

        # Mathematical elimination — end the match the instant the
        # trailer can no longer overtake the leader. With score a-b
        # and `remaining` rounds left, the trailer's ceiling is
        # their_score + remaining; the leader is locked in when
        # (leader > trailer + remaining). Strict inequality, because
        # equality still leaves a tie reachable and tie ≠ win (the
        # bet refunds rather than paying out).
        #
        # This subsumes the old "majority wins" check: for odd N
        # reaching ⌈N/2⌉ wins already implies `a > b + remaining`,
        # and for even N it ends sooner where mathematically valid
        # (e.g. best-of-4, 2-0 with 1 round left).
        remaining = game.rounds - game.current_round - 1
        decided = (
            game.a_score > game.b_score + remaining
            or game.b_score > game.a_score + remaining
        )
        has_more_rounds = game.current_round + 1 < game.rounds
        # "Early end" is true only when we're SKIPPING rounds — on a
        # natural last-round finish there's nothing to skip so the
        # reveal embed shouldn't claim the match ended early.
        is_early_end = decided and has_more_rounds
        will_continue = has_more_rounds and not decided

        icons = await _get_icons(self.bot, game.guild_id)
        coin_label = await self._coin_label(game.guild_id)
        msg = await self._fetch_message(game)
        if msg is not None:
            try:
                await msg.edit(
                    embed=_build_round_embed(
                        game, rnd, icons, reveal=True, coin_label=coin_label,
                        history=history, early_end=is_early_end,
                    ),
                    view=_build_round_view(
                        game_id, icons, disabled=True,
                        lang=_game_lang(game),
                    ),
                )
            except discord.HTTPException:
                pass

        if will_continue:
            await asyncio.sleep(_REVEAL_PAUSE_SECONDS)
            async with self.bot.db.session() as s:
                g = await s.get(OantutiGame, game_id)
                if g is None or g.status != "running":
                    return
                g.current_round += 1
                s.add(OantutiRound(game_id=game_id, round_index=g.current_round))
                await s.commit()
                await s.refresh(g)
                new_rnd = await s.scalar(
                    select(OantutiRound).where(
                        OantutiRound.game_id == game_id,
                        OantutiRound.round_index == g.current_round,
                    )
                )
                history_rows = await s.execute(
                    select(OantutiRound)
                    .where(OantutiRound.game_id == game_id)
                    .order_by(OantutiRound.round_index)
                )
                history = list(history_rows.scalars().all())
            if msg is not None and new_rnd is not None:
                try:
                    await msg.edit(
                        embed=_build_round_embed(
                            g, new_rnd, icons, coin_label=coin_label,
                            history=history,
                        ),
                        view=_build_round_view(
                            game_id, icons,
                            show_session_controls=g.tournament_match_id is None,
                            lang=_game_lang(g),
                        ),
                    )
                except discord.HTTPException:
                    pass
            return

        # No more rounds OR early termination → finalize after a brief
        # pause so the reveal embed is readable first.
        await asyncio.sleep(_REVEAL_PAUSE_SECONDS)
        await self._finalize(game_id, early_end=is_early_end)

    async def _finalize(self, game_id: int, *, early_end: bool = False) -> None:
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None or game.status != "running":
                return
            if game.a_score == game.b_score:
                game.is_tie = True
                game.winner_id = None
            else:
                game.is_tie = False
                game.winner_id = (
                    game.player_a_id if game.a_score > game.b_score
                    else game.player_b_id
                )
            game.status = "finished"
            game.finished_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(game)
            history_rows = await s.execute(
                select(OantutiRound)
                .where(OantutiRound.game_id == game_id)
                .order_by(OantutiRound.round_index)
            )
            history = list(history_rows.scalars().all())

        # Payouts
        payout_winner = 0
        refunded = 0
        if game.bet_amount > 0:
            wallet_svc = WalletService(self.bot)
            if game.is_tie:
                # Refund both
                for uid in (game.player_a_id, game.player_b_id):
                    try:
                        await wallet_svc.adjust(
                            game.guild_id, uid, game.bet_amount,
                            "minigame_oantuti_refund",
                            description=f"Tie in oantuti #{game_id} — bet refund",
                            metadata={"game_id": game_id},
                        )
                    except Exception:
                        log.exception("oantuti: refund-on-tie failed")
                refunded = game.bet_amount
            else:
                # Winner gets 2× bet (their own escrow + loser's)
                payout = game.bet_amount * 2
                try:
                    await wallet_svc.adjust(
                        game.guild_id, game.winner_id, payout,
                        "minigame_oantuti_win",
                        description=f"Win oantuti #{game_id}",
                        metadata={"game_id": game_id},
                    )
                    payout_winner = payout
                    # Pet buff on the NET win (bet_amount) — buffing the
                    # full 2x payout would give the player a bonus on
                    # their own returned escrow.
                    try:
                        buff = await wallet_svc.apply_pet_minigame_buff(
                            game.guild_id, game.winner_id, game.bet_amount,
                            "oantuti", session_id=game_id, outcome="win",
                        )
                        payout_winner += buff
                    except Exception:
                        log.exception("oantuti: pet buff failed")
                except Exception:
                    log.exception("oantuti: payout failed")

        icons = await _get_icons(self.bot, game.guild_id)
        coin_label = await self._coin_label(game.guild_id)
        msg = await self._fetch_message(game)
        # Tournament-mode games don't get a Rematch button (the bracket
        # runs its own pairing — a rogue rematch would corrupt it).
        attach_rematch = game.tournament_match_id is None
        if msg is not None:
            try:
                await msg.edit(
                    embed=_build_final_embed(
                        game, icons, coin_label=coin_label,
                        payout_winner=payout_winner, refunded=refunded,
                        history=history, early_end=early_end,
                    ),
                    view=(
                        _build_final_view(game_id, lang=_game_lang(game))
                        if attach_rematch else None
                    ),
                )
            except discord.HTTPException:
                pass

        # Free-play games only: hall-of-fame + leaderboard refresh.
        # Tournament games are handled by the tournament cog's own
        # hall-of-fame + tournament-champion leaderboard.
        if game.tournament_match_id is None:
            try:
                await self._post_vinh_danh(
                    game, icons, coin_label,
                    payout_winner=payout_winner, refunded=refunded,
                )
            except Exception:
                log.exception("oantuti: hall-of-fame post failed")
            try:
                await self._refresh_vodich_board(game.guild_id)
            except Exception:
                log.exception("oantuti: vodich board refresh failed")

        # Tell tournament (if any) that this match decided.
        self.bot.dispatch("oantuti_finished", game)
        self._locks.pop(game_id, None)

    async def _post_vinh_danh(
        self,
        game: OantutiGame,
        icons: dict[str, str],
        coin_label: str,
        *,
        payout_winner: int,
        refunded: int,
    ) -> None:
        """Best-effort celebratory post in the configured hall-of-fame
        channel. Ties are skipped (no winner = nothing to celebrate)."""
        if game.is_tie or game.winner_id is None:
            return
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, game.guild_id)
        if cfg is None or cfg.oantuti_announce_channel_id is None:
            return
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(cfg.oantuti_announce_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        loser = (
            game.player_b_id if game.winner_id == game.player_a_id
            else game.player_a_id
        )
        # Lifetime free-play wins for the winner
        async with self.bot.db.session() as s:
            total = await s.scalar(
                select(func.count(OantutiGame.id)).where(
                    OantutiGame.guild_id == game.guild_id,
                    OantutiGame.status == "finished",
                    OantutiGame.winner_id == game.winner_id,
                    OantutiGame.tournament_match_id.is_(None),
                )
            )
        # Prize breakdown — same shape as the final embed so the channel
        # archive is self-explanatory.
        if game.bet_amount > 0:
            net_profit = payout_winner - game.bet_amount
            prize_section = (
                f"\n\n💰 **Prize breakdown**\n"
                f"• Each stake: **{game.bet_amount:,}** {coin_label}/side "
                f"(total pot **{game.bet_amount * 2:,}**)\n"
                f"• 🏆 <@{game.winner_id}> takes **+{payout_winner:,}** "
                f"{coin_label} (net +{net_profit:,})\n"
                f"• 💀 <@{loser}> loses **-{game.bet_amount:,}** {coin_label}"
            )
        else:
            prize_section = (
                "\n\n🎉 Friendly match — no bet."
            )
        embed = discord.Embed(
            title="🏆 Hall of Fame — Rock-Paper-Scissors",
            description=(
                f"<@{game.winner_id}> defeated <@{loser}>!\n"
                f"Score: **{game.a_score} - {game.b_score}**"
                f"{prize_section}\n\n"
                f"🎖 Free-play wins by "
                f"<@{game.winner_id}>: **{total or 0}**"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(
            text=f"{icons['bua']} Rock  •  {icons['bao']} Paper  •  {icons['keo']} Scissors"
        )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            log.warning("oantuti: forbidden to post vinh danh in %s", channel.id)

    # ---------- rematch ----------

    async def handle_rematch(
        self, interaction: discord.Interaction, game_id: int
    ) -> None:
        """Spawn a fresh challenge with the same rounds + bet. The clicker
        becomes the new challenger; the OTHER original player is the new
        opponent. Idempotent: a second click anywhere returns a pointer
        to the rematch that already exists."""
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None:
                    await interaction.response.send_message(
                        "Game no longer exists.", ephemeral=True
                    )
                    return
                if game.status != "finished":
                    await interaction.response.send_message(
                        "This game hasn't finished yet — a rematch isn't available.",
                        ephemeral=True,
                    )
                    return
                if interaction.user.id not in (game.player_a_id, game.player_b_id):
                    await interaction.response.send_message(
                        "❌ Only the two original players can start a rematch.",
                        ephemeral=True,
                    )
                    return
                if game.rematch_game_id is not None:
                    await interaction.response.send_message(
                        f"♻️ Rematch already created: game #{game.rematch_game_id} "
                        f"in <#{game.channel_id}>.",
                        ephemeral=True,
                    )
                    return
                # Build new game with clicker as challenger
                new_challenger = interaction.user.id
                new_opponent = (
                    game.player_b_id if new_challenger == game.player_a_id
                    else game.player_a_id
                )
                new_game = OantutiGame(
                    guild_id=game.guild_id,
                    channel_id=game.channel_id,
                    player_a_id=new_challenger,
                    player_b_id=new_opponent,
                    rounds=game.rounds,
                    bet_amount=game.bet_amount,
                    status="invited",
                    language=_game_lang(game),
                )
                s.add(new_game)
                await s.commit()
                await s.refresh(new_game)
                new_id = new_game.id
                # Link rematch back to original
                orig = await s.get(OantutiGame, game_id)
                if orig is not None:
                    orig.rematch_game_id = new_id
                    await s.commit()

        guild_id = new_game.guild_id
        icons = await _get_icons(self.bot, guild_id)
        coin_label = await self._coin_label(guild_id)
        embed = _build_challenge_embed(new_game, icons, coin_label)
        view = _build_challenge_view(new_id, lang=_game_lang(new_game))

        # Post the new challenge
        channel = self.bot.get_channel(new_game.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "❌ Could not post in the original channel — try /oantuti start instead.",
                ephemeral=True,
            )
            return
        try:
            msg = await channel.send(
                content=f"<@{new_opponent}>",
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Bot doesn't have permission to post in <#{new_game.channel_id}>.",
                ephemeral=True,
            )
            return

        async with self.bot.db.session() as s:
            row = await s.get(OantutiGame, new_id)
            if row is not None:
                row.message_id = msg.id
                await s.commit()

        # Disable the rematch button on the ORIGINAL game's final embed
        # so it can't be clicked again.
        orig_msg = await self._fetch_message(game)
        if orig_msg is not None:
            try:
                await orig_msg.edit(view=_build_final_view(
                    game_id, disabled=True, lang=_game_lang(game),
                ))
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            f"🔁 Rematch #{new_id} created — <@{new_opponent}>, please accept.",
            ephemeral=True,
        )


    # =========================================================================
    # /oantuti-admin history / match — owner-only match audit
    # =========================================================================

    _STATUS_ICON: dict[str, str] = {
        "invited": "📨",
        "running": "▶️",
        "finished": "✅",
        "cancelled": "🛑",
    }
    _STATUS_LABEL: dict[str, str] = {
        "invited": "Waiting for accept",
        "running": "Running",
        "finished": "Finished",
        "cancelled": "Cancelled",
    }

    @oantuti_admin.command(
        name="history",
        description="(Owner) View oantuti match history — filter by status",
    )
    @app_commands.describe(
        filter=(
            "Filter by status — default 'all'. "
            "'stuck' = running for more than 1 hour"
        ),
        limit="Number of matches to show (1-25, default 15)",
    )
    @app_commands.choices(filter=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="running", value="running"),
        app_commands.Choice(name="stuck", value="stuck"),
        app_commands.Choice(name="finished", value="finished"),
        app_commands.Choice(name="cancelled", value="cancelled"),
    ])
    async def oantuti_history(
        self,
        interaction: discord.Interaction,
        filter: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 25] = 15,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        filter_value = filter.value if filter else "all"
        async with self.bot.db.session() as s:
            stmt = select(OantutiGame).where(
                OantutiGame.guild_id == interaction.guild_id,
            )
            if filter_value == "stuck":
                cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
                stmt = stmt.where(
                    OantutiGame.status == "running",
                    OantutiGame.created_at < cutoff,
                )
            elif filter_value != "all":
                stmt = stmt.where(OantutiGame.status == filter_value)
            stmt = stmt.order_by(OantutiGame.created_at.desc()).limit(limit)
            games = list((await s.execute(stmt)).scalars().all())

        if not games:
            await interaction.followup.send(
                f"_No matches found for filter `{filter_value}`._",
                ephemeral=True,
            )
            return
        coin_label = await self._coin_label(interaction.guild_id)
        now = datetime.now(timezone.utc)
        lines: list[str] = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for g in games:
            icon = self._STATUS_ICON.get(g.status, "❔")
            label = self._STATUS_LABEL.get(g.status, g.status)
            # Stuck flag = running but older than 1 hour. Owner sees
            # this in 'all' filter too so they spot zombies.
            age = now - g.created_at if g.created_at else None
            stuck_flag = ""
            if g.status == "running" and age and age > timedelta(hours=1):
                stuck_flag = " ⚠️ **STUCK**"
            age_str = (
                f"<t:{int(g.created_at.timestamp())}:R>"
                if g.created_at else "—"
            )
            bet_str = (
                f" • 💰 {g.bet_amount:,} {coin_label}"
                if g.bet_amount > 0 else ""
            )
            lines.append(
                f"`#{g.id:>4}` {icon} **{label}**{stuck_flag}\n"
                f"     🅰️ <@{g.player_a_id}> **{g.a_score}-{g.b_score}** "
                f"<@{g.player_b_id}> 🅱️\n"
                f"     {age_str} • {g.rounds} rounds{bet_str}"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        embed = discord.Embed(
            title=f"📜 RPS match history — {filter_value} (top {len(games)})",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.from_rgb(241, 196, 15),
        )
        embed.set_footer(
            text=(
                "Click 🔍 View detail to enter a match ID and open the "
                "Cancel / Delete panel."
            ),
        )
        await interaction.followup.send(
            embed=embed,
            view=_MatchHistoryView(self),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @oantuti_admin.command(
        name="match",
        description="(Owner) View one match's detail + Cancel / Delete buttons",
    )
    @app_commands.describe(game_id="Match ID (see /oantuti-admin history)")
    async def oantuti_match(
        self,
        interaction: discord.Interaction,
        game_id: app_commands.Range[int, 1, 9_999_999],
    ) -> None:
        # Defer + owner check + lookup + send all live in the shared
        # helper so the slash command and the modal submit hit the
        # same code path.
        await self._send_match_detail(interaction, int(game_id))

    async def _send_match_detail(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ) -> None:
        """Look up a single oantuti game by id (scoped to the
        clicker's guild) and reply with an ephemeral detail embed +
        _MatchDetailView. Called by /oantuti-admin match (slash) and
        by _MatchIdModal.on_submit (modal). Owner-only — modal path
        is reachable only after the history list, which is itself
        owner-gated, but we re-check here so it can be called
        anywhere safely."""
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        assert interaction.guild_id is not None
        async with self.bot.db.session() as s:
            game = await s.get(OantutiGame, game_id)
            if game is None or game.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"❌ Match `#{game_id}` not found in this server.",
                    ephemeral=True,
                )
                return
            rounds = list((await s.execute(
                select(OantutiRound)
                .where(OantutiRound.game_id == game.id)
                .order_by(OantutiRound.round_index)
            )).scalars().all())
        embed = await self._build_match_detail_embed(game, rounds)
        view = _MatchDetailView(self, game.id, game.status)
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _build_match_detail_embed(
        self,
        game: "OantutiGame",
        rounds: list["OantutiRound"],
    ) -> discord.Embed:
        coin_label = await self._coin_label(game.guild_id)
        icon = self._STATUS_ICON.get(game.status, "❔")
        status_label = self._STATUS_LABEL.get(game.status, game.status)
        bet_line = (
            f"💰 Bet: **{game.bet_amount:,}** {coin_label} per side"
            if game.bet_amount > 0 else "🎉 No bet"
        )
        winner_line = ""
        if game.status == "finished":
            if game.is_tie:
                winner_line = "🤝 Tie"
            elif game.winner_id:
                winner_line = f"🏆 Winner: <@{game.winner_id}>"
        elif game.status == "cancelled":
            winner_line = "🛑 Match cancelled"
        elif game.status == "running":
            now = datetime.now(timezone.utc)
            age = now - game.created_at if game.created_at else None
            if age and age > timedelta(hours=1):
                winner_line = "⚠️ Match is STUCK (running > 1 hour)"
            else:
                winner_line = "▶️ Running"
        embed = discord.Embed(
            title=f"{icon} Match #{game.id} — {status_label}",
            description=(
                f"🅰️ <@{game.player_a_id}> **{game.a_score} - {game.b_score}** "
                f"<@{game.player_b_id}> 🅱️\n"
                f"🎯 {game.rounds} rounds • {bet_line}\n"
                f"{winner_line}"
            ),
            color=discord.Color.from_rgb(241, 196, 15),
        )
        if game.created_at:
            embed.add_field(
                name="Created",
                value=f"<t:{int(game.created_at.timestamp())}:f>",
                inline=True,
            )
        if game.finished_at:
            embed.add_field(
                name="Ended",
                value=f"<t:{int(game.finished_at.timestamp())}:f>",
                inline=True,
            )
        if game.tournament_match_id is not None:
            embed.add_field(
                name="🏆 Tournament",
                value=f"match #{game.tournament_match_id}",
                inline=True,
            )
        # Round-by-round breakdown
        icons = await _get_icons(self.bot, game.guild_id)
        hist_lines = _build_history_lines(rounds, icons)
        if hist_lines:
            embed.add_field(
                name=f"📜 Round history ({len(hist_lines)})",
                value="\n".join(hist_lines)[:1024],
                inline=False,
            )
        return embed

    @staticmethod
    async def _auto_dismiss_match_panel(
        interaction: discord.Interaction,
        panel_message: discord.Message | None,
        delay: int = 4,
    ) -> None:
        """Fire-and-forget cleanup after Cancel / Delete from the detail
        panel. Waits long enough for the owner to read the success
        toast, then deletes both:
          - the toast (deferred → edited 'thinking' ephemeral)
          - the parent detail panel (interaction.message)
        All failures are swallowed — if either message has already
        been dismissed by the user there's nothing to recover."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Toast: the deferred + edited ephemeral that holds the
        # "Cancelled" / "Deleted" content lives as the interaction's
        # original response.
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass
        # Panel: the detail embed that hosted the Cancel/Delete buttons.
        # Component interactions own the source message, so
        # message.delete() routes through the interaction webhook.
        if panel_message is not None:
            try:
                await panel_message.delete()
            except (
                discord.NotFound, discord.Forbidden,
                discord.HTTPException,
            ):
                pass

    async def admin_cancel_match(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ) -> None:
        """Admin-initiated cancel: mark cancelled, refund bet, fire
        oantuti_finished. Reuses the same refund path as the in-game
        End button so the wallet ledger reads consistent."""
        # Capture the panel message NOW — once we defer, downstream
        # code mutates interaction state and we still want a stable
        # handle for _auto_dismiss_match_panel.
        panel_msg = interaction.message
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._require_owner(interaction):
            return
        a_id: int | None = None
        b_id: int | None = None
        bet: int = 0
        guild_id: int | None = None
        last_round_idx: int | None = None
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None or game.guild_id != interaction.guild_id:
                    await interaction.edit_original_response(
                        content=f"❌ Match `#{game_id}` not found.",
                    )
                    return
                if game.status != "running":
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Match `#{game_id}` is not running "
                            f"(current: `{game.status}`). Nothing to cancel."
                        ),
                    )
                    return
                game.status = "cancelled"
                game.finished_at = datetime.now(timezone.utc)
                game.is_tie = False
                await s.commit()
                await s.refresh(game)
                a_id = game.player_a_id
                b_id = game.player_b_id
                bet = int(game.bet_amount)
                guild_id = game.guild_id
                last_round_idx = game.current_round

        assert (
            a_id is not None and b_id is not None and guild_id is not None
        )
        refund_ok = True
        if bet > 0:
            wallet_svc = WalletService(self.bot)
            for uid in (a_id, b_id):
                try:
                    await wallet_svc.adjust(
                        guild_id, uid, bet,
                        "minigame_oantuti_refund",
                        description=(
                            f"Admin cancelled oantuti #{game_id} — bet refund"
                        ),
                        metadata={
                            "game_id": game_id,
                            "reason": "admin_cancel",
                            "actor_id": interaction.user.id,
                        },
                    )
                except Exception:
                    refund_ok = False
                    log.exception(
                        "oantuti admin-cancel refund failed: "
                        "guild=%s user=%s", guild_id, uid,
                    )
        if last_round_idx is not None:
            await self._dismiss_pick_acks(game_id, last_round_idx)

        coin_label = await self._coin_label(guild_id)
        async with self.bot.db.session() as s:
            g = await s.get(OantutiGame, game_id)
        msg = await self._fetch_message(g) if g is not None else None
        if msg is not None:
            try:
                await msg.edit(
                    embed=discord.Embed(
                        title="🛑 Match cancelled by admin",
                        description=(
                            f"<@{interaction.user.id}> (admin) cancelled this match."
                            + (
                                f"\n💰 Refunded **{bet:,}** {coin_label} "
                                "to both players."
                                if bet > 0 and refund_ok else ""
                            )
                            + (
                                f"\n⚠️ Refund failed — please contact a dev."
                                if bet > 0 and not refund_ok else ""
                            )
                        ),
                        color=discord.Color.dark_gray(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass
        await interaction.edit_original_response(
            content=(
                f"🛑 Match `#{game_id}` cancelled."
                + (
                    f" Refunded **{bet:,}** {coin_label} × 2 players."
                    if bet > 0 and refund_ok else
                    ""
                )
            ),
        )
        if g is not None:
            self.bot.dispatch("oantuti_finished", g)
        # Auto-dismiss the detail panel + this toast a few seconds
        # later so the owner doesn't have to close them manually.
        asyncio.create_task(
            self._auto_dismiss_match_panel(interaction, panel_msg, delay=4),
        )

    async def admin_delete_match(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ) -> None:
        """Hard-delete game + round rows. Bet is NOT refunded — this
        is the nuclear option for cleaning up zombie / test rows.
        Owner-only. If the game is still running with a non-zero
        bet, surface a warning telling the admin to cancel first
        (which refunds) rather than nuke and leave coin floating."""
        panel_msg = interaction.message
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._require_owner(interaction):
            return
        async with self._lock(game_id):
            async with self.bot.db.session() as s:
                game = await s.get(OantutiGame, game_id)
                if game is None or game.guild_id != interaction.guild_id:
                    await interaction.edit_original_response(
                        content=f"❌ Match `#{game_id}` not found.",
                    )
                    return
                if game.status == "running" and game.bet_amount > 0:
                    await interaction.edit_original_response(
                        content=(
                            f"⚠️ Match `#{game_id}` is running with a bet of "
                            f"**{game.bet_amount:,}** coins. "
                            "Use **Cancel & refund** first, then Delete — a "
                            "direct Delete does not refund."
                        ),
                    )
                    return
                if game.tournament_match_id is not None:
                    await interaction.edit_original_response(
                        content=(
                            f"❌ Match `#{game_id}` belongs to tournament "
                            f"#{game.tournament_match_id} — cannot be deleted. "
                            "The bracket cog owns its lifecycle."
                        ),
                    )
                    return
                await s.execute(
                    delete(OantutiRound).where(
                        OantutiRound.game_id == game_id,
                    )
                )
                await s.delete(game)
                await s.commit()
        # Best-effort drop the public channel embed too — leaving an
        # orphan "Match #X" card with non-functional buttons is worse
        # than no card.
        async with self.bot.db.session() as s:
            # Game row already gone, but we cached message_id/channel_id
            # at fetch time in the deleted row — we can't reach back to
            # the message. Skip the embed cleanup: admin can delete
            # manually if needed.
            pass
        await interaction.edit_original_response(
            content=f"🗑️ Deleted match `#{game_id}` and all its rounds.",
        )
        # Auto-dismiss the detail panel + this toast a few seconds
        # later so the owner doesn't have to close them manually.
        asyncio.create_task(
            self._auto_dismiss_match_panel(interaction, panel_msg, delay=4),
        )

    # =========================================================================
    # /oantuti vodich — free-play wins leaderboard
    # =========================================================================

    vodich = app_commands.Group(
        name="vodich",
        description="Free-play champion leaderboard (Rock-Paper-Scissors 1v1)",
        parent=oantuti_admin,
    )

    _VODICH_I18N: dict[str, dict[str, str]] = {
        LANG_RU: {
            "title":      "🥊  КАМЕНЬ-НОЖНИЦЫ-БУМАГА — ТАБЛИЦА ЧЕМПИОНОВ  🥊",
            "no_winners": "_Ещё нет побед в свободном режиме._\n**Сыграй матч и стань первым!**",
            "win_unit":   "побед",
            "footer":     "{guild} • Топ {n} • Обновляется после каждого матча",
        },
        LANG_EN: {
            "title":      "🥊  ROCK-PAPER-SCISSORS — FREE-PLAY CHAMPION LEADERBOARD  🥊",
            "no_winners": "_No free-play wins yet._\n**Join a match and be the first!**",
            "win_unit":   "wins",
            "footer":     "{guild} • Top {n} • Auto-updates after each match",
        },
    }

    async def _build_vodich_embed(
        self, guild_id: int, display_count: int, lang: str = LANG_RU,
    ) -> discord.Embed:
        strings = self._VODICH_I18N.get(lang, self._VODICH_I18N[LANG_RU])
        async with self.bot.db.session() as s:
            rows = await s.execute(
                select(
                    OantutiGame.winner_id,
                    func.count(OantutiGame.id).label("wins"),
                )
                .where(
                    OantutiGame.guild_id == guild_id,
                    OantutiGame.status == "finished",
                    OantutiGame.winner_id.isnot(None),
                    OantutiGame.tournament_match_id.is_(None),
                )
                .group_by(OantutiGame.winner_id)
                .order_by(
                    func.count(OantutiGame.id).desc(), OantutiGame.winner_id
                )
                .limit(display_count)
            )
            results = list(rows.all())

        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else "Server"
        embed = discord.Embed(
            title=strings["title"],
            color=discord.Color.from_rgb(241, 196, 15),
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if not results:
            embed.description = strings["no_winners"]
        else:
            lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
            for rank, (uid, wins) in enumerate(results, start=1):
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"`#{rank:>2}`"
                lines.append(
                    f"{medal}  <@{uid}>  •  ⚔️ `{wins:,}` {strings['win_unit']}"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            embed.description = "\n".join(lines)
        embed.set_footer(
            text=strings["footer"].format(guild=guild_name, n=display_count)
        )
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _build_play_view(
        self, guild_id: int,
    ) -> discord.ui.View:
        """Link button to the configured /oantuti channel — the place
        challengers post /oantuti start. Empty view when no channel
        is set."""
        view = discord.ui.View(timeout=None)
        async with self.bot.db.session() as s:
            cfg = await s.get(GuildConfig, guild_id)
        ch_id = cfg.oantuti_channel_id if cfg else None
        if ch_id is None:
            return view
        guild = self.bot.get_guild(guild_id)
        ch = guild.get_channel(ch_id) if guild else None
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return view
        view.add_item(discord.ui.Button(
            label="Play now",
            emoji="🎮",
            style=discord.ButtonStyle.link,
            url=ch.jump_url,
        ))
        return view

    async def _build_vodich_view(
        self, guild_id: int, lang: str = LANG_RU,
    ) -> discord.ui.View:
        view = await self._build_play_view(guild_id)
        view.add_item(VodichBoardLangButton(guild_id, lang=lang))
        return view

    async def _refresh_vodich_board(self, guild_id: int) -> None:
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
        if board is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(board.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        lang = getattr(board, "language", LANG_RU)
        embed = await self._build_vodich_embed(guild_id, board.display_count, lang=lang)
        view = await self._build_vodich_view(guild_id, lang=lang)
        msg = None
        if board.message_id is not None:
            try:
                msg = await channel.fetch_message(board.message_id)
            except (discord.NotFound, discord.Forbidden):
                msg = None
        if msg is not None:
            try:
                await msg.edit(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as e:
                log.warning("oantuti vodich: edit failed for %s: %s", guild_id, e)
            return
        try:
            new_msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            log.warning("oantuti vodich: forbidden to repost in %s", guild_id)
            return
        async with self.bot.db.session() as s:
            row = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
            if row is not None:
                row.message_id = new_msg.id
                await s.commit()

    @vodich.command(
        name="post",
        description="(Admin) Create / move the free-play champion leaderboard",
    )
    @app_commands.describe(
        channel="Channel to post the leaderboard in (any text channel)",
        display_count="How many users to show (1-25, default 10)",
    )
    async def vodich_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        display_count: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            prev = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
        if prev is not None and prev.message_id is not None and interaction.guild:
            prev_ch = interaction.guild.get_channel(prev.channel_id)
            if isinstance(prev_ch, (discord.TextChannel, discord.Thread)):
                try:
                    old_msg = await prev_ch.fetch_message(prev.message_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        embed = await self._build_vodich_embed(
            interaction.guild_id, int(display_count), lang=LANG_RU,
        )
        view = await self._build_vodich_view(interaction.guild_id, lang=LANG_RU)
        try:
            msg = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot doesn't have permission to post in {channel.mention}.", ephemeral=True
            )
            return

        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
            if board is None:
                board = GameWinBoard(
                    guild_id=interaction.guild_id,
                    game_type="oantuti",
                    channel_id=channel.id,
                    message_id=msg.id,
                    display_count=int(display_count),
                    language=LANG_RU,
                )
                s.add(board)
            else:
                board.channel_id = channel.id
                board.message_id = msg.id
                board.display_count = int(display_count)
                board.language = LANG_RU
            await s.commit()
        await interaction.followup.send(
            f"✅ Champion leaderboard posted in {channel.mention} (top {display_count}). "
            "It will auto-update after every free-play match.",
            ephemeral=True,
        )

    @vodich.command(
        name="remove",
        description="(Admin) Delete the champion leaderboard + stop auto-update",
    )
    async def vodich_remove(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
            if board is None:
                await interaction.followup.send(
                    "This server has no champion leaderboard.", ephemeral=True
                )
                return
            channel_id = board.channel_id
            message_id = board.message_id
            await s.delete(board)
            await s.commit()
        if message_id is not None and interaction.guild is not None:
            ch = interaction.guild.get_channel(channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(
            "✅ Champion leaderboard deleted + auto-update stopped.", ephemeral=True
        )

    @vodich.command(
        name="refresh",
        description="(Admin) Refresh the champion leaderboard immediately",
    )
    async def vodich_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        if not await self._require_owner(interaction):
            return
        async with self.bot.db.session() as s:
            board = await s.scalar(
                select(GameWinBoard).where(
                    GameWinBoard.guild_id == interaction.guild_id,
                    GameWinBoard.game_type == "oantuti",
                )
            )
        if board is None:
            await interaction.followup.send(
                "This server has no leaderboard. Run `/oantuti-admin vodich post` first.",
                ephemeral=True,
            )
            return
        await self._refresh_vodich_board(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Champion leaderboard refreshed in <#{board.channel_id}>.",
            ephemeral=True,
        )


async def setup(bot: "AkimBot") -> None:
    await bot.add_cog(Oantuti(bot))
