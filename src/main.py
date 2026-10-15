from __future__ import annotations

import asyncio
import logging

import discord

from .bot import AkimBot
from .config import load_settings
from .desk_api.app import run_uvicorn_in_background
from .desk_api.config import load_desk_api_settings

log = logging.getLogger("akim-bot.main")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


# Backoff schedule for Cloudflare 429s on bot login. Fewer, longer
# waits — Cloudflare's per-IP counter recovers over minutes, and every
# extra 429 we generate extends the block. Total budget ≈ 15 min; if
# we can't log in by then, retrying more just piles onto Cloudflare's
# strike counter.
_LOGIN_BACKOFF_SECONDS: tuple[int, ...] = (60, 240, 600)


async def _cleanup_http(bot: AkimBot) -> None:
    """Explicitly close the aiohttp adapter between login attempts.
    discord.py's bot.start creates the connector during login; a failed
    static_login leaves it dangling and the GC prints
    'Unclosed client session' warnings on the next attempt. Closing
    here also frees Cloudflare's per-connection counter faster."""
    http = getattr(bot, "http", None)
    if http is None:
        return
    try:
        await http.close()
    except Exception:
        # Close is best-effort — we're about to try login again, and a
        # partial close is still better than leaking the session.
        pass


async def _start_bot_with_backoff(bot: AkimBot, token: str) -> None:
    """Wrap bot.start with retries on 429 / transient HTTP failures.

    The typical failure mode: Railway restarts the container after a
    crash, discord.py's static_login hits Discord's Cloudflare edge to
    validate the token, Cloudflare returns 429 because our IP block
    (shared Railway egress) has been hammered, and the process dies
    immediately. Sleeping inside the same process breaks that loop and
    lets Cloudflare's per-IP counter decay. But when Cloudflare has
    escalated to a longer block (hours), no amount of retrying from
    this IP will help — the operator needs to redeploy on a fresh IP.
    """
    for attempt, delay in enumerate(_LOGIN_BACKOFF_SECONDS, start=1):
        try:
            await bot.start(token)
            return
        except discord.HTTPException as e:
            # 401 = bad token, 403 = permission — retrying is pointless.
            if e.status in (401, 403):
                log.error(
                    "bot.start failed with %s — token/permission problem, not retrying.",
                    e.status,
                )
                raise
            log.warning(
                "bot.start failed (attempt %d/%d): HTTP %s. Sleeping %ds before retry.",
                attempt, len(_LOGIN_BACKOFF_SECONDS), e.status, delay,
            )
        except (discord.ConnectionClosed, ConnectionError, OSError) as e:
            # Same treatment for the gateway dropping / DNS blips.
            log.warning(
                "bot.start network error (attempt %d/%d): %s. Sleeping %ds before retry.",
                attempt, len(_LOGIN_BACKOFF_SECONDS), e, delay,
            )
        # Free the aiohttp connector before the next attempt so we don't
        # leak sessions AND so Cloudflare sees fewer live connections.
        await _cleanup_http(bot)
        await asyncio.sleep(delay)

    # All backoff slots used. Emit an actionable error and bail. Piling
    # more attempts onto Cloudflare from the same blocked IP only makes
    # the block last longer.
    log.error(
        "bot.start exhausted backoff (%d attempts). Discord's Cloudflare "
        "edge is rate-limiting this IP. Recovery options:\n"
        "  1. Redeploy the Railway service to get a fresh IP.\n"
        "  2. Wait 1-2 hours, then redeploy.\n"
        "  3. If persistent, contact Railway support to check whether "
        "the shared egress IP has been flagged.\n"
        "Exiting so Railway shows the deploy as unhealthy.",
        len(_LOGIN_BACKOFF_SECONDS),
    )
    raise RuntimeError(
        "Discord login rate-limited by Cloudflare after all retries. "
        "Redeploy the service to get a new IP."
    )


async def _run() -> None:
    _configure_logging()
    settings = load_settings()
    desk_settings = load_desk_api_settings()
    bot = AkimBot(settings, desk_settings)

    api_task: asyncio.Task[None] | None = None
    async with bot:
        # Start FastAPI only when DESKTOP_TOKEN_SECRET is present so a
        # plain-bot deploy isn't forced to set up the desktop env.
        if desk_settings.enabled:
            # Wait for db.init_models() to land before serving — done in
            # bot.setup_hook(). discord.py calls setup_hook before login
            # completes, but we don't have an explicit hook here, so we
            # let the API start in parallel and the endpoints will hit
            # an initialized DB by the time the desktop polls them.
            api_task = await run_uvicorn_in_background(desk_settings, bot.db)
        else:
            log.info(
                "Desk API disabled (DESKTOP_TOKEN_SECRET unset). Bot starting "
                "without /api endpoints."
            )

        try:
            await _start_bot_with_backoff(bot, settings.discord_token)
        finally:
            if api_task is not None:
                api_task.cancel()
                # Suppress the cancellation traceback — we expect it.
                try:
                    await api_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
