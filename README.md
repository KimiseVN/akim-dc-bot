# Akim Discord Bot

Discord bot tiếng Việt cho cộng đồng game: báo danh nhân vật + tìm tổ đội (LFG) với UI nút bấm.

## Tính năng

| Slash | Mô tả |
|---|---|
| `/baodanh moi` | Đăng ký/cập nhật nhân vật: chọn vũ khí chính/phụ + trả lời câu hỏi "Có phải thành viên Guild ...?" → modal điền Tên/UID/Lực chiến/Hạng đấu trường. User có Unverified role → submission đi vào hàng chờ duyệt. User thường → lưu thẳng + đổi nickname. |
| `/baodanh pending` | (Admin) Liệt kê các báo danh đang chờ duyệt với check UID + nút Approve/Deny. |
| `/baodanh approved` | (Admin) Liệt kê user đã duyệt, kèm người duyệt + role được cấp. |
| `/baodanh denied` | (Admin) Liệt kê user bị từ chối. |
| `/baodanh resend user:<...>` | (Admin) Đăng lại card duyệt nếu card cũ bị xóa. |
| `/capnhat` | Cập nhật lại thông tin (alias cho `/baodanh moi`). |
| `/info [user]` | Xem profile của mình hoặc thành viên khác. |
| `/member add\|delete\|list` | (**Owner-only**) Quản lý danh sách UID thành viên Guild in-game. Bulk add bằng cách nhập nhiều UID cách bằng space/comma/newline. `list` đối chiếu với Player records — mỗi UID hiển thị user Discord + tên in-game, hoặc `(chưa báo danh)` nếu chưa có ai đăng ký UID đó. |
| `/setup-baodanh` | (Admin) Cấu hình luồng duyệt: Unverified role, channel post pending, các approver roles, tên Guild hiển thị trong dialog. |
| `/server add\|edit\|delete\|list` | (Admin) Quản lý danh sách server in-game (EU/NA/JP/...). User chọn server khi `/baodanh moi`, nickname tự append `- icon+tên server` (VD: `AKim - 🇪🇺EU`). |
| `/topic set\|clear` | (Admin) Đặt/xóa channel topic (mô tả hiển thị ở header channel). `/topic set` mở modal multi-line, hỗ trợ Discord markdown (`**bold**`, `*italic*`, `__underline__`, `~~strike~~`, `` `code` ``, `> quote`, links/emoji auto-render). Max 1024 ký tự. |
| `/quiz add` | (Admin) Mở modal bulk-paste câu hỏi quiz. Format mỗi câu: `N. câu hỏi` → `A./B./C./D. option` → `Đáp án: X`. Dùng dòng trống để phân cách. Parse được cả với hoặc không có dấu (`Dap an: B` cũng OK). |
| `/quiz list\|remove` | (Admin) Quản lý bank câu hỏi. |
| `/quiz setup channel:<...> announce_channel:<...> winner_role:<...> wins_for_role:<int>` | (Admin) Cấu hình mặc định cho `/quiz`: channel game, channel Vinh danh, winner role auto-grant, số wins cần đạt để được cấp role. Mỗi param optional — chỉ truyền cái cần đổi, các giá trị khác giữ nguyên. |
| `/quiz start num_questions:<int> seconds_per_question:<int> [channel:<...>]` | (Admin) Tạo session quiz: bot random N câu, post controller. Mặc định dùng channel đã setup; truyền `channel:` để override 1 lần. Người tạo bấm **▶️ Start Quiz** → loop từng câu với timer per-question + buttons A/B/C/D. Mỗi user 1 đáp án / câu (không đổi được). Hết timer → reveal đáp án đúng + list user chọn mỗi option. Cuối quiz → bảng xếp hạng → user top score (nếu > 0) được log thành **win**, post Vinh danh + auto cấp `winner_role` khi đạt `wins_for_role`. Tie ở top → mọi người tied đều được tính win. |
| `/sohoc setup` | (Admin) Cấu hình minigame đoán số: channel game, channel vinh danh, creator roles (tối đa 3), winner role auto-grant (5+ wins), `end_time` = giờ trong ngày (24h `HH:MM`), `tz_offset` = múi giờ tính end_time (vd 7 = VN, 2 = CEST, 0 = UTC, -5 = EST; range -12..14, mặc định 7). Game sẽ luôn đóng đúng giờ đó trong múi giờ đã chọn; nếu đã qua thì rollover sang ngày kế. |
| `/sohoc start` | (Creator role) Tạo round mới — không cần param, dùng `channel` + `end_time` đã `/sohoc setup`. Nếu hiện tại đã qua `end_time` hôm nay → tự rollover sang ngày mai. Bot post embed với nút **Tham gia** + countdown. User bấm → modal nhập số 00-99 → nếu admin đã setup `entry_fee` qua `/economy reward setup game_type:sohoc entry_fee:N` thì trừ tự động (đăng ký bị huỷ nếu ví không đủ). Đến giờ → disable Tham gia + hiện **🎲 Roll số** (creator-only). Roll random 0-99, ai chọn trùng + sớm nhất = 🏆 winner. Gửi vinh danh + cấp role winner khi đạt 5 wins. |
| `/wallet [user]` | Xem số dư + tổng đã kiếm/tiêu của bạn hoặc user khác. |
| `/leaderboard [count]` | Top N số dư trong server (mặc định 10, max 25). |
| `/history [user] [count]` | Lịch sử N giao dịch gần nhất — show delta, reason, balance_after. |
| `/economy setup` | (Admin) Cấu hình coin: tên, icon, join reward, daily text/voice reward, min msg length, voice min duration, manager roles. |
| `/economy ignore add\|remove\|list` | (Admin) Quản lý channel exclude khỏi daily reward. |
| `/economy reward setup game_type:<quiz\|sohoc> ...` | (Admin) Set reward amounts cho từng minigame: **top1 / top2 / top3** + `participate` (ngoài top 3) + `entry_fee`. Ties trong cùng score share placement (vd 2 user cùng top 1 → mỗi người nhận top1_reward). |
| `/economy sync-members` | (Admin) Tạo ví cho mọi member hiện có (skip bot, không thưởng join). |
| `/economy add\|remove\|set user amount [reason]` | (Admin) Cộng / Trừ / Set số dư trực tiếp; ghi vào CoinTransaction. |

### Economy chi tiết

**Schema (8 tables):** `economy_guild_configs`, `economy_wallets`, `economy_transactions`, `economy_daily_claims`, `economy_minigame_configs`, `economy_minigame_claims`, `economy_ignored_channels`, `economy_voice_sessions`. Mọi mutation đi qua `WalletService.adjust()` — đảm bảo `CoinTransaction` được ghi cho mọi thay đổi và balance không bao giờ < 0.

**Daily reward scope:** mỗi user được thưởng **1 lần / channel / ngày** cho mỗi loại (text + voice). Unique constraint trên `(guild_id, user_id, channel_id, reward_type, claim_date)` chặn double-claim.

**Anti-spam:** bot bị skip, message ngắn hơn `min_message_length` bị skip, channel trong ignore list bị skip. Voice reward chỉ tính khi user ở voice >= `voice_min_duration_seconds`.

**Minigame integration:** `/quiz` + `/sohoc` dispatch event (`bot.dispatch('quiz_finished', MiniGameResult)` / `sohoc_finished`) sau khi xác định winner; Economy cog listen + gọi `grant_minigame_rewards()`. Outcome buckets: `top1` / `top2` / `top3` / `participate`; ai có mặt ở nhiều bucket chỉ nhận tier cao nhất. Sohoc chỉ có 1 winner → `top1=[winner]`, không có top2/top3. Quiz bucket theo distinct score (ties cùng score share placement). Idempotent qua `MiniGameRewardClaim` unique key `(game_type, session_id, user_id, outcome)` — nếu game cog re-dispatch (vd bot retry) thì không trả thưởng 2 lần.

**Listener intent (optional):** Mặc định bot **KHÔNG** request `message_content` (privileged) — nếu request nhưng chưa enable ở Developer Portal, bot crash on startup và mọi slash command sẽ báo "did not respond". Daily text reward vẫn hoạt động bình thường (chỉ skip phần kiểm tra độ dài nếu `min_message_length=0`, mặc định). Muốn enforce min length: enable intent ở Dev Portal + bật `intents.message_content=True` trong `src/bot.py` + set `min_message_length` qua `/economy setup`.
| `/sync uid:<...>` | Đồng bộ thông tin player (tên/Level/Build Power/Elegance/Guild) từ bot **WWM Workshop**. User chạy `/sync UID:...`, Akim hướng dẫn copy `/player` để gửi qua WWM, sau đó tự đọc embed kết quả và update Player record. Chỉ chính chủ UID hoặc admin được sync. |
| `/setup-sync` | (Admin) Cấu hình Discord User ID của bot nguồn (WWM Workshop). Bật Developer Mode → chuột phải vào avatar bot WWM → Copy User ID. |
| `/lfg hero-realm` | Tạo party Hero Realm (mặc định 10 người). Tham số `voice:<#voice-channel>` (optional) để bật nút **Join Voice** và **Start Match**. |
| `/lfg guild-war` | Tạo party Guild War (mặc định 30 người). |
| `/lfg sword-trial` | Tạo party Sword Trial (mặc định 5 người). |
| `/lfg custom mode_name:<...>` | Tạo party tự định nghĩa. |
| `/setup-lfg channel:<...> [role1..role5]` | Admin: cấu hình channel để post LFG và tới 5 role được phép tạo `/lfg`. Để trống hết role → mọi thành viên được dùng `/lfg`. |
| `/weapon add\|remove\|list` | Admin: quản lý danh sách vũ khí (hiện trong dropdown của `/baodanh`). |

Khi tạo `/lfg`, bot post 1 embed có nút **Tank / Healer / DPS** để mọi người bấm tham gia. Nút tự động disable khi đầy slot. Bấm lại nút role mình đang chọn = rời party. Người tạo (hoặc admin) thấy thêm nút **End LFG** để đóng đăng ký.

Nếu khi tạo có chỉ định `voice:<#voice-channel>`, embed thêm:
- 🔊 **Join Voice** (mọi người) — link button mở channel voice trong Discord client.
- 🎬 **Start Match** (creator/admin) — tự `move_to` mọi member đã chọn vị trí vào voice channel đó. Member nào chưa kết nối voice nào sẽ được liệt kê để tự join (Discord không cho phép kéo người ngoài voice). Bot cần quyền **Move Members** + **Connect** trên voice channel.

## Stack

- Python 3.12 (pinned via `.python-version`)
- [discord.py](https://discordpy.readthedocs.io/) 2.4 (slash commands, modals, dynamic persistent buttons)
- SQLAlchemy 2.0 async + asyncpg (PostgreSQL) — fallback SQLite cho dev local
- Deploy: Railway (Nixpacks)

## Setup local

```bash
git clone https://github.com/thuytk1412/akim-dc-bot.git
cd akim-dc-bot

python -m venv .venv
.venv\Scripts\activate         # Windows
# source .venv/bin/activate    # macOS/Linux

pip install -r requirements.txt
cp .env.example .env
# Mở .env, điền DISCORD_TOKEN + DISCORD_APP_ID
# DEV_GUILD_IDS=<id_server_của_bạn>   (slash sẽ sync ngay, không cần đợi 1h)

python -m src.main
```

### Lấy bot token

1. https://discord.com/developers/applications → **New Application**.
2. Tab **Bot** → **Reset Token** → copy vào `DISCORD_TOKEN`.
3. Bật **Privileged Gateway Intents → SERVER MEMBERS INTENT** (cần để bot resolve display name trong embed LFG).
4. Tab **OAuth2 → URL Generator** → scopes: `bot` + `applications.commands`; permissions: `View Channels`, `Send Messages`, `Embed Links`, `Read Message History`, `Use External Emojis`, `Manage Nicknames`, `Manage Roles`, `Manage Channels`, `Connect`, `Move Members`. Mở URL → invite vào server. (Cách nhanh: dùng permission integer `420858896`.)

   > `Manage Roles` cần để bot Approve báo danh (cấp role chính thức + xóa Unverified). Bot vẫn chỉ assign/xóa được role thấp hơn role bot, nên kéo role bot lên trên Unverified + tất cả role thành viên.

   > Trong server, **kéo role của bot lên trên** role thường (Server Settings → Roles), nếu không bot sẽ không đổi được nickname của member có role cao hơn.
5. Tab **General Information** → **Application ID** → copy vào `DISCORD_APP_ID`.

## Deploy lên Railway

1. Push repo lên GitHub (đã setup ở https://github.com/thuytk1412/akim-dc-bot).
2. Vào Railway project → **+ New** → **GitHub Repo** → chọn `thuytk1412/akim-dc-bot`.
3. **+ New** → **Database** → **Add PostgreSQL**. Railway tự inject biến `DATABASE_URL` vào service bot.
4. Trong service bot → **Variables** → thêm:
   - `DISCORD_TOKEN` = bot token
   - `DISCORD_APP_ID` = application id
   - (`DATABASE_URL` đã tự có từ Postgres reference)
5. Railway dùng `railway.json` + `Procfile` → auto chạy `python -m src.main`.
6. Sau khi deploy, slash commands sẽ sync global (tới 1h Discord cache). Nếu muốn test ngay, set thêm `DEV_GUILD_IDS=<id_server>` rồi redeploy.

## Quy trình sử dụng

1. Admin: `/setup-lfg channel:#tim-to-doi role1:@Thành viên`
2. Admin: `/setup-baodanh unverified_role:@Unverified approval_channel:#duyet-bao-danh guild_name:"ふMiêuGiaふ" approver_role1:@Moderator`
3. Admin: `/weapon add name:"Snowparting Blade" icon:❄️ slot:main` … (lặp lại cho mỗi vũ khí)
4. Admin: `/member add uids:"0048985000, 0012345678, 0099887766"` để seed UID Guild
5. User mới join (đang có role Unverified): `/baodanh moi` → chọn vũ khí + trả lời Yes/No câu hỏi Guild → điền form → submission vào hàng chờ duyệt; bot ping approver roles trong approval channel
6. Approver bấm **Approve** trên card chờ → chọn role để cấp; bot tự cấp role, gỡ Unverified, đồng bộ nickname. Hoặc bấm **Deny** để từ chối.
7. User thường (không có Unverified): `/baodanh moi` lưu thẳng + đổi nickname luôn (không qua duyệt).
8. Admin có thể xem lại hàng chờ bất kỳ lúc nào với `/baodanh pending`.
9. User có role được phép: `/lfg hero-realm dps:6 healer:2 tank:2 voice:#raid-room`.

## Cấu trúc dự án

```
src/
  main.py          # entry
  bot.py           # AkimBot class + cog loader + persistent views
  config.py        # env loading (.env)
  db/
    models.py      # SQLAlchemy models (Player, Weapon, GuildConfig, LFGParty, LFGMember)
    session.py     # async engine + session factory
  cogs/
    registration.py  # /baodanh /capnhat /info
    admin.py         # /setup-lfg + /weapon group
    lfg.py           # /lfg group
  views/
    registration.py  # weapon select dropdowns + modal
    lfg.py           # persistent join/leave/end buttons + embed builder
```

## Member lifecycle

Khi 1 member **rời server** (tự out / bị kick / bị ban):

- Bot tự xóa Player row của họ (báo danh data) trong guild đó.
- Nếu họ đang ở trạng thái `pending` và card duyệt còn ở approval channel, card được edit thành "🚪 User đã rời server" + buttons biến mất (không spam delete, giữ audit trail).
- Khi member quay lại → không còn data → `/baodanh moi` lần đầu → status='pending' (theo rule mới) → admin Approve lại.

Quiz answers và LFG memberships cũ của họ vẫn được giữ (historical data, không ảnh hưởng tới rejoin).

## Database & migrations policy

`init_models()` chạy mỗi khi bot start. Chỉ làm operations **additive**, không bao giờ destructive:

- ✅ `Base.metadata.create_all` — chỉ tạo table/column thiếu, idempotent
- ✅ `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` qua `_INCREMENTAL_COLUMNS` trong [src/db/session.py](src/db/session.py)
- ✅ `UPDATE` để backfill data sang column mới
- ❌ `drop_all`, `DROP TABLE`, `DROP COLUMN`, `TRUNCATE` — **cấm tuyệt đối**
- ❌ Rename column / change type không-mở-rộng — phải làm qua 2 release (add-new → backfill → deprecate old)

**Thêm column mới:** khai báo trong `models.py` + thêm 1 dòng vào `_INCREMENTAL_COLUMNS`. Xong, không cần Alembic.

**Production guard:** Nếu bot detect đang chạy trên Railway/Fly/Render/Kubernetes (qua env vars như `RAILWAY_ENVIRONMENT`), nó **từ chối start** khi `DATABASE_URL` không set hoặc trỏ vào SQLite — vì SQLite trong container không có volume = mất data mỗi lần redeploy. Cảnh báo này có thể bypass bằng `ALLOW_SQLITE_IN_PROD=true` (chỉ dùng khi bạn cố ý mount volume).

Khởi động bot, dòng log đầu tiên hiển thị DB đang kết nối:
```
INFO akim-bot.db: Connecting to DB: dialect=postgresql url=postgresql+asyncpg://postgres:***@postgres.railway.internal:5432/railway
INFO akim-bot.db: DB ready (additive schema only — see policy in session.py)
```
Nếu thấy `dialect=sqlite` trên Railway log → biến `DATABASE_URL` chưa link đúng tới Postgres service.

## Phân quyền admin (audit)

Tất cả slash command quản trị đều có `default_permissions(manage_guild=True)` — Discord chỉ cho user có **Manage Server** thấy/gọi:

| Command | Visible to user thường? |
|---|---|
| `/baodanh moi`, `/capnhat`, `/info` | ✅ Có (đúng intent) |
| `/lfg *` | ✅ Có — permission filter trong code qua `/setup-lfg role1..5` |
| `/baodanh pending`, `/baodanh resend` | ❌ Chỉ Manage Server |
| `/setup-lfg`, `/setup-baodanh` | ❌ Chỉ Manage Server |
| `/weapon add\|remove\|list` | ❌ Chỉ Manage Server |
| `/member add\|delete\|list` | ❌ **Chỉ chủ server** (owner-only, hard-check runtime) |
| `/server add\|edit\|delete\|list` | ❌ Chỉ Manage Server |
| `/sync` | ✅ Có (mọi user nhưng chỉ sync được UID của chính mình; admin sync được UID người khác) |
| `/setup-sync` | ❌ Chỉ Manage Server |
| `/topic set\|clear` | ❌ Chỉ Manage Server |
| `/quiz add\|list\|remove\|setup\|start` | ❌ Chỉ Manage Server |
| `/sohoc setup` | ❌ Chỉ Manage Server |
| `/sohoc start` | ✅ Có (cần creator role đã setup hoặc Manage Server) |
| `/wallet`, `/leaderboard`, `/history` | ✅ Có (mọi user) |
| `/economy *` | ❌ Chỉ owner / Manage Server / configured manager role |

Server admin có thể override per-role qua **Server Settings → Apps → AkimBot → Permissions** nếu muốn giao quyền cho mod role cụ thể. Discord enforce gate này ở phía gateway — không cần runtime check thêm.

## Lưu ý

- Discord giới hạn 25 options/select. Nếu cần > 25 vũ khí (hoặc server) mỗi slot, sau này cần pagination hoặc autocomplete.
- Persistent views dùng `discord.ui.DynamicItem` (discord.py 2.4+), nên buttons sống sót sau khi bot restart.
- Nickname max 32 ký tự — nếu `ingame_name + " - " + icon + server_name > 32` bot sẽ tự trim phần `ingame_name`.
- Mọi command đều `guild_only` — bot không hoạt động trong DM.
