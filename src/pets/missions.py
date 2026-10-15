"""Pet daily / weekly missions.

The mission catalogue is hardcoded — keeping it static lets the bot
ship without a per-guild config table while still being easy to tweak
by editing this file. Each mission is an entry with:

  key      stable identifier used in pet_mission_progress.mission_key
  label    user-facing Vietnamese label
  icon     small emoji shown in the Pet House mission embed
  target   how many `progress` ticks complete the mission
  reward   coin granted on claim

Action → missions mapping lives in ACTION_TO_MISSIONS: when the user
performs a care action via _try_apply_action, the cog calls
MissionService.increment_actions to bump every mission tied to that
action key (e.g. "feed" bumps both the daily "feed 3 times" and the
weekly "care 30 times (any action)").

Period bucketing
  The bot serves a Vietnam-time audience, so daily resets at local
  midnight UTC+7 and weekly resets Monday 00:00 UTC+7. We do NOT use
  the guild's economy tz_offset because the mission cadence is global
  to the bot — a multi-server user shouldn't see different daily resets
  in different guilds. period_start is stored as ISO date string
  ("YYYY-MM-DD") which is portable across Postgres/SQLite without
  needing a real DATE column type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


VN_TZ_OFFSET_HOURS = 7


@dataclass(frozen=True)
class MissionDef:
    key: str
    label: str
    icon: str
    target: int
    reward: int
    period: str  # "daily" or "weekly"


DAILY_MISSIONS: tuple[MissionDef, ...] = (
    MissionDef("feed_3",  "Feed pet 3 times",       "🍖", 3, 15, "daily"),
    MissionDef("water_3", "Water pet 3 times",      "💧", 3, 15, "daily"),
    MissionDef("play_1",  "Play with pet 1 time",   "🎾", 1, 20, "daily"),
    MissionDef("train_1", "Train pet 1 time",       "💪", 1, 30, "daily"),
)

WEEKLY_MISSIONS: tuple[MissionDef, ...] = (
    MissionDef("care_30",    "Care for pet 30 times (any action)", "❤️", 30, 150, "weekly"),
    MissionDef("level_up_3", "Level up active pet 3 times",        "🆙",  3, 200, "weekly"),
)

ALL_MISSIONS: tuple[MissionDef, ...] = DAILY_MISSIONS + WEEKLY_MISSIONS

# Care action → missions it counts toward. "care_30" lives under every
# action so any care work progresses the weekly catch-all.
ACTION_TO_MISSIONS: dict[str, tuple[str, ...]] = {
    "feed":  ("feed_3", "care_30"),
    "water": ("water_3", "care_30"),
    "play":  ("play_1", "care_30"),
    "train": ("train_1", "care_30"),
    "clean": ("care_30",),
    "rest":  ("care_30",),
}


def _by_key() -> dict[str, MissionDef]:
    return {m.key: m for m in ALL_MISSIONS}


_BY_KEY = _by_key()


def get_mission(key: str) -> MissionDef | None:
    return _BY_KEY.get(key)


def _local_shifted(
    now: datetime | None,
    tz_offset: int,
    reset_hour: int,
) -> datetime:
    """UTC `now` → guild-local time, then shifted backwards by
    `reset_hour` so the day boundary aligns with the configured
    reset clock instead of midnight. Example:

      tz_offset=7, reset_hour=6  → reset at 06:00 VN
      A care action at 03:00 VN  → shifted = previous day 21:00
        → period_start = previous date (still in 'yesterday's
        mission day until 06:00).
      A care action at 07:00 VN  → shifted = same day 01:00
        → period_start = today (new mission day has begun).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now + timedelta(hours=tz_offset) - timedelta(hours=reset_hour)


def current_daily_period(
    tz_offset: int = VN_TZ_OFFSET_HOURS,
    reset_hour: int = 0,
    now: datetime | None = None,
) -> str:
    """ISO date marking which mission-day a care action falls in.
    Honours per-guild reset hour + timezone so an owner can re-anchor
    "midnight" to fit their playerbase's schedule (e.g. reset at
    06:00 SGT so the EU-evening crowd doesn't flip days mid-session)."""
    return _local_shifted(now, tz_offset, reset_hour).date().isoformat()


def current_weekly_period(
    tz_offset: int = VN_TZ_OFFSET_HOURS,
    reset_hour: int = 0,
    now: datetime | None = None,
) -> str:
    """ISO date of the Monday that begins the mission-week containing
    `now`. Monday boundary still aligns to the reset hour — so with
    reset_hour=6, the week flips at Monday 06:00 local."""
    local_date = _local_shifted(now, tz_offset, reset_hour).date()
    monday = local_date - timedelta(days=local_date.weekday())
    return monday.isoformat()


def period_for_mission(
    m: MissionDef,
    tz_offset: int = VN_TZ_OFFSET_HOURS,
    reset_hour: int = 0,
    now: datetime | None = None,
) -> str:
    if m.period == "daily":
        return current_daily_period(tz_offset, reset_hour, now)
    return current_weekly_period(tz_offset, reset_hour, now)


__all__ = (
    "MissionDef",
    "DAILY_MISSIONS",
    "WEEKLY_MISSIONS",
    "ALL_MISSIONS",
    "ACTION_TO_MISSIONS",
    "VN_TZ_OFFSET_HOURS",
    "get_mission",
    "current_daily_period",
    "current_weekly_period",
    "period_for_mission",
)
