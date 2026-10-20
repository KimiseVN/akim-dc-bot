# akim-dc-bot — Project Context

## Repo & Branch
- **Local dev + push target:** `https://github.com/KimiseVN/akim-dc-bot.git` branch `main`
- Working directory: `D:\ClaudeKIM\MYWORKSPACE\zeroclaw\0CLAW-PROJECTS\akim-dc-bot`

## Standing Instructions
- **Luôn tự động mark ready + merge mọi PR** — không hỏi lại

## Stack
- Python 3.12 / discord.py 2.4.0
- SQLAlchemy 2.0 async + asyncpg + PostgreSQL
- Deployed on Railway from `KimiseVN/akim-dc-bot`

## Recent Changes (as of 2026-10-20)

### 1. Sohoc bugs fixed (`src/cogs/sohoc.py`, `src/db/models.py`, `src/db/session.py`)
- Added `sohoc_default_language` column to `GuildConfig` (VARCHAR(4), nullable)
- Added migration entry in `src/db/session.py`
- Fixed `_maybe_autostart_next`: moved guard inside `async with`, added active-session check before spawning
- Fixed `_spawn_next_game` and `sohoc_start`: use `cfg.sohoc_default_language or LANG_RU`
- Added `sohoc_setup` choices for `default_language` param

### 2. LFG Guild War per-day bars (`src/views/lfg.py`)
- `build_gw_embed`: replaced single combined progress bar with two per-day bars
- Saturday and Sunday each show their own `count/limit`, `🟢 remaining`, and FULL indicator
- Reserve shown as a separate line if any

## Key Files
- `src/cogs/sohoc.py` — Sohoc game logic
- `src/cogs/lfg.py` — LFG slash commands
- `src/views/lfg.py` — LFG embeds and button handlers (`build_gw_embed` for Guild War)
- `src/db/models.py` — SQLAlchemy ORM models (`GuildConfig`, `LFGParty`, `LFGMember`)
- `src/db/session.py` — DB session + incremental column migrations

## DB Migration Pattern
New columns are added incrementally in `src/db/session.py` as tuples:
```python
("table_name", "column_name", "COLUMN_TYPE DEFAULT value"),
```
Bot applies them automatically on startup via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
