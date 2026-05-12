import os
import re
import time
import logging
import sqlite3
import fcntl

from telegram.error import NetworkError, TimedOut, RetryAfter

from thefuzz import process as fuzz_process
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from keep_alive import keep_alive

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ADMIN_ID   = int(os.environ["ADMIN_ID"])
DB_PATH    = os.environ.get("DB_PATH", "movies.db")
LOCK_FILE  = "/tmp/telegram_bot.lock"
FUZZY_THRESHOLD = int(os.environ.get("FUZZY_THRESHOLD", "70"))
CHANNEL_ID_2   = -1003975532379   # Secondary channel — dual force-subscribe

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lock helpers (prevent duplicate instances)
# ---------------------------------------------------------------------------
_lock_fd = None

def acquire_lock():
    global _lock_fd
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Stale lock from a crashed process — remove and retry once
        _lock_fd.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
        _lock_fd = open(LOCK_FILE, "w")
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.error("Another bot instance is already running. Exiting.")
            raise SystemExit(1)

def release_lock():
    global _lock_fd
    if _lock_fd:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  INTEGER UNIQUE,
                title       TEXT NOT NULL,
                raw_text    TEXT,
                posted_at   TEXT,
                categories  TEXT DEFAULT '',
                file_id     TEXT DEFAULT '',
                file_type   TEXT DEFAULT ''
            )
        """)
        # Migrate: add any missing columns without touching existing data
        cols = {r[1] for r in conn.execute("PRAGMA table_info(movies)").fetchall()}
        for col, definition in [
            ("categories", "TEXT DEFAULT ''"),
            ("file_id",    "TEXT DEFAULT ''"),
            ("file_type",  "TEXT DEFAULT ''"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE movies ADD COLUMN {col} {definition}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  INTEGER UNIQUE,
                raw_text    TEXT,
                posted_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query       TEXT NOT NULL,
                user_id     INTEGER,
                username    TEXT,
                searched_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)

def extract_title(text: str) -> str | None:
    """Best-effort title extraction from channel post text."""
    if not text:
        return None
    # Take first non-empty line, strip common noise
    for line in text.splitlines():
        line = line.strip()
        if line:
            # Remove leading emoji / bullet chars
            line = re.sub(r"^[\U00010000-\U0010ffff\u2000-\u206f\u2700-\u27bf]+", "", line).strip()
            # Remove trailing year/quality tags like (2024) [1080p]
            line = re.sub(r"[\(\[]\d{4}[\)\]].*$", "", line).strip()
            if len(line) >= 2:
                return line
    return None

def extract_media(msg) -> tuple[str, str]:
    """Return (file_id, file_type) from a Telegram message object, or ('', '')."""
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.animation:
        return msg.animation.file_id, "animation"
    if msg.document:
        return msg.document.file_id, "document"
    return "", ""

def add_movie(message_id: int, title: str, raw_text: str, posted_at: str,
              file_id: str = "", file_type: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO movies
               (message_id, title, raw_text, posted_at, file_id, file_type)
               VALUES (?,?,?,?,?,?)""",
            (message_id, title, raw_text, posted_at, file_id, file_type),
        )
        conn.commit()

def delete_movie_by_id(message_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM movies WHERE message_id=?", (message_id,))
        conn.commit()
        return cur.rowcount > 0

def delete_movie_by_title(query: str) -> tuple[bool, str]:
    """Fuzzy-find the closest title and delete it. Returns (deleted, matched_title)."""
    titles_ids = get_all_titles()
    if not titles_ids:
        return False, ""
    titles = [t for t, _ in titles_ids]
    result = fuzz_process.extractOne(query, titles, score_cutoff=80)
    if not result:
        return False, ""
    matched = result[0]
    with get_conn() as conn:
        conn.execute("DELETE FROM movies WHERE title=?", (matched,))
        conn.commit()
    return True, matched

def add_pending(message_id: int, raw_text: str, posted_at: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pending (message_id, raw_text, posted_at) VALUES (?,?,?)",
            (message_id, raw_text, posted_at),
        )
        conn.commit()

def get_all_titles() -> list[tuple[str, int]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT title, message_id FROM movies").fetchall()
    return [(r["title"], r["message_id"]) for r in rows]

def search_movies(query: str) -> list[dict]:
    titles_ids = get_all_titles()
    if not titles_ids:
        return []
    titles = [t for t, _ in titles_ids]
    results = fuzz_process.extractBests(query, titles, score_cutoff=FUZZY_THRESHOLD, limit=5)
    if not results:
        return []
    matched_titles = {r[0] for r in results}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(matched_titles))
        rows = conn.execute(
            f"SELECT title, message_id, posted_at FROM movies WHERE title IN ({placeholders})",
            list(matched_titles),
        ).fetchall()
    return [dict(r) for r in rows]

def movie_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

def recent_movies(limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, message_id, posted_at FROM movies ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def pending_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

def resolve_pending(message_id: int, title: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending WHERE message_id=?", (message_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO movies (message_id, title, raw_text, posted_at) VALUES (?,?,?,?)",
            (message_id, title, row["raw_text"], row["posted_at"]),
        )
        conn.execute("DELETE FROM pending WHERE message_id=?", (message_id,))
        conn.commit()
    return True

# ---------------------------------------------------------------------------
# Search log helpers
# ---------------------------------------------------------------------------
def log_search(query: str, user_id: int, username: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_logs (query, user_id, username) VALUES (?,?,?)",
            (query.strip().lower(), user_id, username),
        )
        conn.commit()

def top_searches(limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT query, COUNT(*) AS total
            FROM search_logs
            GROUP BY query
            ORDER BY total DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------
CATEGORY_SEP = ","

def _parse_cats(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(CATEGORY_SEP) if c.strip()]

def get_all_categories() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT categories FROM movies WHERE categories != ''").fetchall()
    cats: set[str] = set()
    for r in rows:
        cats.update(_parse_cats(r["categories"]))
    return sorted(cats)

def movies_by_category(cat: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, message_id FROM movies WHERE ',' || categories || ',' LIKE ?",
            (f"%,{cat.strip()},%",),
        ).fetchall()
    return [dict(r) for r in rows]

def tag_movie(message_id: int, category: str) -> bool:
    """Add a category tag to a movie. Returns False if movie not found."""
    category = category.strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT categories FROM movies WHERE message_id=?", (message_id,)
        ).fetchone()
        if not row:
            return False
        existing = _parse_cats(row["categories"])
        if category not in existing:
            existing.append(category)
        conn.execute(
            "UPDATE movies SET categories=? WHERE message_id=?",
            (CATEGORY_SEP.join(existing), message_id),
        )
        conn.commit()
    return True

# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
def channel_link(message_id: int) -> str:
    cid = str(CHANNEL_ID)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"

# ---------------------------------------------------------------------------
# Force-subscribe helpers
# ---------------------------------------------------------------------------
_SUBSCRIBED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

async def _check_one_channel(bot, user_id: int, channel_id: int) -> bool:
    """Return True if user_id is a member/admin/owner of the given channel."""
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        return member.status in _SUBSCRIBED_STATUSES
    except Exception:
        return True   # can't check → allow through

async def is_subscribed(bot, user_id: int) -> bool:
    """Return True only if user is a member of BOTH required channels."""
    ch1 = await _check_one_channel(bot, user_id, CHANNEL_ID)
    ch2 = await _check_one_channel(bot, user_id, CHANNEL_ID_2)
    return ch1 and ch2

async def _get_invite_link(bot, channel_id: int) -> str:
    """Return a join URL for the given channel."""
    try:
        chat = await bot.get_chat(channel_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
        if chat.invite_link:
            return chat.invite_link
        return await bot.export_chat_invite_link(channel_id)
    except Exception:
        return "https://t.me"

async def send_subscribe_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tell the user they must join BOTH channels, with join buttons for each."""
    bot = context.bot
    link1 = await _get_invite_link(bot, CHANNEL_ID)
    link2 = await _get_invite_link(bot, CHANNEL_ID_2)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة الأولى",  url=link1)],
        [InlineKeyboardButton("📢 اشترك في القناة الثانية", url=link2)],
        [InlineKeyboardButton("✅ تحققت من اشتراكي", callback_data="check_subscription")],
    ])
    await update.message.reply_text(
        "⚠️ يجب عليك الاشتراك في القناتين أولاً للوصول إلى البوت.\n\n"
        "1️⃣ اشترك في القناة الأولى.\n"
        "2️⃣ اشترك في القناة الثانية.\n"
        "3️⃣ ثم اضغط على 'تحققت من اشتراكي'.",
        reply_markup=keyboard,
    )

async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 'Check Again' inline button — re-verify membership in both channels."""
    query = update.callback_query
    await query.answer()
    bot    = context.bot
    user_id = query.from_user.id
    ch1 = await _check_one_channel(bot, user_id, CHANNEL_ID)
    ch2 = await _check_one_channel(bot, user_id, CHANNEL_ID_2)
    if ch1 and ch2:
        await query.edit_message_text(
            "✅ تم التحقق من اشتراكك في القناتين! يمكنك الآن البحث عن أي فيلم."
        )
    else:
        link1 = await _get_invite_link(bot, CHANNEL_ID)
        link2 = await _get_invite_link(bot, CHANNEL_ID_2)
        missing = []
        if not ch1:
            missing.append("القناة الأولى")
        if not ch2:
            missing.append("القناة الثانية")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 اشترك في القناة الأولى",  url=link1)],
            [InlineKeyboardButton("📢 اشترك في القناة الثانية", url=link2)],
            [InlineKeyboardButton("✅ تحققت من اشتراكي", callback_data="check_subscription")],
        ])
        await query.edit_message_text(
            f"❌ لم يتم التحقق من اشتراكك في: {' و'.join(missing)}.\n\n"
            "تأكد من انضمامك ثم اضغط الزر مجدداً.",
            reply_markup=keyboard,
        )

# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً! أنا بوت للبحث عن الأفلام.\n\n"
        "الأوامر المتاحة:\n"
        "/search <اسم الفيلم> – البحث عن فيلم\n"
        "/recent – آخر 10 أفلام مضافة\n"
        "/count – إجمالي الأفلام المفهرسة\n\n"
        "أو اكتب اسم الفيلم مباشرةً للبحث الفوري."
    )

async def count_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Movies indexed: {movie_count()}")

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movies = recent_movies()
    if not movies:
        await update.message.reply_text("No movies indexed yet.")
        return
    lines = [f"• [{m['title']}]({channel_link(m['message_id'])})" for m in movies]
    await update.message.reply_text(
        "Recent movies:\n" + "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID and not await is_subscribed(context.bot, user_id):
        await send_subscribe_prompt(update, context)
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /search <title>")
        return
    await _do_search(update, context, query)

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Admin only.")
        return
    count = pending_count()
    await update.message.reply_text(f"Pending (unrecognised) posts: {count}")

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or post.chat.id != CHANNEL_ID:
        return
    text = post.text or post.caption or ""
    posted_at = post.date.isoformat() if post.date else ""
    file_id, file_type = extract_media(post)
    title = extract_title(text)
    if title:
        add_movie(post.message_id, title, text, posted_at, file_id, file_type)
        logger.info("Indexed: %s (msg %s, media=%s)", title, post.message_id, file_type or "none")
    else:
        add_pending(post.message_id, text, posted_at)
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ Could not extract title from post {post.message_id}.\n\n"
            f"Reply to this message with the correct title to index it.",
        )
        logger.warning("Pending: msg %s", post.message_id)

async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin replies to a 'movie not found' notification with the correct title."""
    msg = update.message
    if not msg.reply_to_message:
        return
    # Extract the message_id from the notification text
    notif_text = msg.reply_to_message.text or ""
    m = re.search(r"post (\d+)", notif_text)
    if not m:
        await msg.reply_text("Could not find the post ID in the original notification.")
        return
    original_msg_id = int(m.group(1))
    title = msg.text.strip()
    if resolve_pending(original_msg_id, title):
        await msg.reply_text(f"Indexed as: {title}")
    else:
        await msg.reply_text("Post not found in pending list (maybe already resolved).")

async def backfill_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Index forwards from the channel sent to the bot's private chat (admin only)."""
    msg = update.message

    # Restrict to admin only
    if update.effective_user.id != ADMIN_ID:
        await msg.reply_text("Only the admin can backfill posts.")
        return

    # ── Detect the origin of the forward ──────────────────────────────────
    # python-telegram-bot 21.x exposes forward_origin (MessageOriginChannel)
    # Older fallback: forward_from_chat / forward_from_message_id
    origin_chat_id  = None
    origin_msg_id   = None
    origin_date     = None

    if msg.forward_origin is not None:
        from telegram.constants import MessageOriginType
        if msg.forward_origin.type == MessageOriginType.CHANNEL:
            origin_chat_id = msg.forward_origin.chat.id
            origin_msg_id  = msg.forward_origin.message_id
            origin_date    = msg.forward_origin.date
    elif msg.forward_from_chat is not None:
        origin_chat_id = msg.forward_from_chat.id
        origin_msg_id  = msg.forward_from_message_id
        origin_date    = msg.forward_date

    if origin_chat_id is None:
        await msg.reply_text(
            "Could not read the origin of this forward. Make sure you are forwarding a channel post directly to me."
        )
        return

    # ── Try to extract and index (admin may forward from any channel) ──────
    text      = msg.text or msg.caption or ""
    posted_at = origin_date.isoformat() if origin_date else ""
    file_id, file_type = extract_media(msg)
    title     = extract_title(text)

    if title:
        add_movie(origin_msg_id, title, text, posted_at, file_id, file_type)
        logger.info("Backfilled: %s (msg %s from chat %s, media=%s)",
                    title, origin_msg_id, origin_chat_id, file_type or "none")
        await msg.reply_text(
            f"✅ Backfilled successfully!\n\n"
            f"Title: {title}\n"
            f"Media: {file_type or 'none (text only)'}\n"
            f"Total indexed: {movie_count()}"
        )
    else:
        add_pending(origin_msg_id, text, posted_at)
        logger.warning("Backfill pending: msg %s — title unclear", origin_msg_id)
        await msg.reply_text(
            f"⚠️ Could not extract a title from message {origin_msg_id}.\n\n"
            f"Reply to THIS message with the correct title to index it."
        )

async def text_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID and not await is_subscribed(context.bot, user_id):
        await send_subscribe_prompt(update, context)
        return
    query = (update.message.text or "").strip()
    if query:
        await _do_search(update, context, query)

async def _do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Fuzzy-search the database and reply with inline keyboard results."""
    user = update.effective_user
    log_search(query, user.id, user.username or "")

    titles_ids = get_all_titles()
    if not titles_ids:
        await update.message.reply_text("لا توجد أفلام مفهرسة بعد.")
        return

    titles = [t for t, _ in titles_ids]
    raw_results = fuzz_process.extractBests(
        query, titles, score_cutoff=FUZZY_THRESHOLD, limit=5
    )

    if not raw_results:
        await update.message.reply_text(
            "عذراً، هذا الفيلم غير متوفر الآن. سيتم توفيره في غضون 24 ساعة من تاريخ رسالتك."
        )
        user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
        await context.bot.send_message(
            ADMIN_ID,
            f"🔍 طلب فيلم غير موجود\n\n"
            f"المستخدم: {user_info}\n"
            f"الفيلم المطلوب: {query}",
        )
        return

    # Build score map and fetch full records (including media) in one query
    score_map = {title: score for title, score, *_ in raw_results}
    matched_titles = list(score_map.keys())
    with get_conn() as conn:
        placeholders = ",".join("?" * len(matched_titles))
        rows = conn.execute(
            f"SELECT title, message_id, raw_text, file_id, file_type "
            f"FROM movies WHERE title IN ({placeholders})",
            matched_titles,
        ).fetchall()

    # Sort by descending match score
    rows_sorted = sorted(rows, key=lambda r: score_map.get(r["title"], 0), reverse=True)

    count = len(rows_sorted)
    await update.message.reply_text(f"🔍 وجدنا {count} نتيجة لـ «{query}»:")

    chat_id = update.effective_chat.id
    bot = context.bot

    for r in rows_sorted:
        watch_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ شاهد الآن", url=channel_link(r["message_id"]))
        ]])
        caption = (r["raw_text"] or r["title"])[:1024]
        fid  = r["file_id"]   or ""
        ftype = r["file_type"] or ""

        try:
            if ftype == "photo" and fid:
                await bot.send_photo(chat_id, fid, caption=caption, reply_markup=watch_btn)
            elif ftype == "video" and fid:
                await bot.send_video(chat_id, fid, caption=caption, reply_markup=watch_btn)
            elif ftype == "animation" and fid:
                await bot.send_animation(chat_id, fid, caption=caption, reply_markup=watch_btn)
            elif ftype == "document" and fid:
                await bot.send_document(chat_id, fid, caption=caption, reply_markup=watch_btn)
            else:
                # No media saved — try copy_message, fall back to button
                await bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=CHANNEL_ID,
                    message_id=r["message_id"],
                    reply_markup=watch_btn,
                )
        except Exception:
            # Last resort: plain button with title
            await bot.send_message(
                chat_id,
                f"🎬 {r['title']}",
                reply_markup=watch_btn,
            )

# ---------------------------------------------------------------------------
# Stats command
# ---------------------------------------------------------------------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Admin only.")
        return
    rows = top_searches(10)
    if not rows:
        await update.message.reply_text("No searches recorded yet.")
        return
    lines = [f"{i+1}. {r['query']}  —  {r['total']} مرة" for i, r in enumerate(rows)]
    await update.message.reply_text(
        f"📊 أكثر 10 أفلام بحثاً:\n\n" + "\n".join(lines)
    )

# ---------------------------------------------------------------------------
# Categories commands & callbacks
# ---------------------------------------------------------------------------
async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID and not await is_subscribed(context.bot, user_id):
        await send_subscribe_prompt(update, context)
        return
    cats = get_all_categories()
    if not cats:
        await update.message.reply_text(
            "لا توجد تصنيفات بعد.\n"
            "يمكن للمسؤول إضافة تصنيف بالأمر:\n/tag <message_id> <category>"
        )
        return
    # One button per category, 2 per row
    rows = [
        [InlineKeyboardButton(f"🏷 {c}", callback_data=f"cat:{c}")]
        for c in cats
    ]
    # Group into pairs
    paired = [rows[i] + (rows[i+1] if i+1 < len(rows) else []) for i in range(0, len(rows), 2)]
    await update.message.reply_text(
        "🎭 اختر تصنيفاً لاستعراض الأفلام:",
        reply_markup=InlineKeyboardMarkup(paired),
    )

async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":", 1)[1]
    movies = movies_by_category(cat)
    if not movies:
        await query.edit_message_text(f"لا توجد أفلام في تصنيف «{cat}» بعد.")
        return
    buttons = [
        [InlineKeyboardButton(f"🎬 {m['title']}", url=channel_link(m["message_id"]))]
        for m in movies
    ]
    buttons.append([InlineKeyboardButton("🔙 العودة للتصنيفات", callback_data="cats_back")])
    await query.edit_message_text(
        f"🏷 أفلام تصنيف «{cat}» ({len(movies)}):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def categories_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cats = get_all_categories()
    if not cats:
        await query.edit_message_text("لا توجد تصنيفات بعد.")
        return
    rows = [
        [InlineKeyboardButton(f"🏷 {c}", callback_data=f"cat:{c}")]
        for c in cats
    ]
    paired = [rows[i] + (rows[i+1] if i+1 < len(rows) else []) for i in range(0, len(rows), 2)]
    await query.edit_message_text(
        "🎭 اختر تصنيفاً لاستعراض الأفلام:",
        reply_markup=InlineKeyboardMarkup(paired),
    )

# ---------------------------------------------------------------------------
# Tag command (admin)
# ---------------------------------------------------------------------------
async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /tag <message_id> <category>")
        return
    try:
        msg_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("message_id must be a number.")
        return
    category = " ".join(context.args[1:]).strip()
    if tag_movie(msg_id, category):
        await update.message.reply_text(f"✅ Tagged message {msg_id} as «{category}».")
    else:
        await update.message.reply_text(f"Movie with message_id {msg_id} not found.")

# ---------------------------------------------------------------------------
# Delete command (admin)
# ---------------------------------------------------------------------------
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "  /delete <message_id>  — delete by exact message ID\n"
            "  /delete <title>       — delete by fuzzy title match"
        )
        return
    arg = " ".join(context.args).strip()
    try:
        msg_id = int(arg)
        if delete_movie_by_id(msg_id):
            await update.message.reply_text(f"✅ Deleted movie with message_id {msg_id}.")
        else:
            await update.message.reply_text(f"No movie found with message_id {msg_id}.")
    except ValueError:
        ok, matched = delete_movie_by_title(arg)
        if ok:
            await update.message.reply_text(f"✅ Deleted «{matched}» (matched from «{arg}»).")
        else:
            await update.message.reply_text(
                f"No movie found matching «{arg}» (need ≥80% similarity)."
            )

# ---------------------------------------------------------------------------
def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",      start_command))
    app.add_handler(CommandHandler("count",      count_command))
    app.add_handler(CommandHandler("recent",     recent_command))
    app.add_handler(CommandHandler("search",     search_command))
    app.add_handler(CommandHandler("pending",    pending_command))
    app.add_handler(CommandHandler("stats",      stats_command))
    app.add_handler(CommandHandler("categories", categories_command))
    app.add_handler(CommandHandler("tag",        tag_command))
    app.add_handler(CommandHandler("delete",     delete_command))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    app.add_handler(MessageHandler(
        filters.Chat(ADMIN_ID) & filters.TEXT & filters.REPLY & ~filters.COMMAND,
        admin_reply_handler,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.FORWARDED,
        backfill_handler,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED,
        text_search_handler,
    ))

    # Callback query handlers — most specific patterns first
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_subscription$"))
    app.add_handler(CallbackQueryHandler(categories_back_callback,    pattern="^cats_back$"))
    app.add_handler(CallbackQueryHandler(category_callback,           pattern="^cat:"))

    return app


def run_bot_with_retry():
    """Run polling with automatic restart on any crash or network error."""
    BASE_DELAY     = 5
    MAX_DELAY      = 60
    delay          = BASE_DELAY

    while True:
        try:
            logger.info("Starting bot — channel_id=%s", CHANNEL_ID)
            app = build_app()
            app.run_polling(
                allowed_updates=["channel_post", "message", "callback_query"],
                drop_pending_updates=True,
            )
            # run_polling only returns on a clean shutdown (e.g. SIGINT)
            logger.info("Bot stopped cleanly.")
            break

        except RetryAfter as exc:
            # Telegram asked us to wait a specific number of seconds
            wait = exc.retry_after + 1
            logger.warning("Telegram rate-limit hit — waiting %ds before retry", wait)
            time.sleep(wait)
            # Don't increase delay — this is expected throttling, not a crash

        except TimedOut:
            logger.warning("Telegram request timed out — reconnecting in %ds", delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        except NetworkError as exc:
            logger.error("Network error: %s — reconnecting in %ds", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        except Exception as exc:
            logger.error("Unexpected crash: %s — restarting in %ds", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)

        else:
            # Successful polling loop ended — reset back-off
            delay = BASE_DELAY


def main():
    acquire_lock()
    try:
        init_db()
        keep_alive(movie_count_fn=movie_count)
        run_bot_with_retry()
    finally:
        release_lock()


if __name__ == "__main__":
    main()
