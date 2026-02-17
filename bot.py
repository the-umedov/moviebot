import os
import re
import asyncio
from typing import Optional, Tuple, List

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


# ======================
# CONFIG (Railway Variables)
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Siz bergan kanal invite link
CHANNEL_INVITE = os.getenv("CHANNEL_INVITE", "https://t.me/+JBZQtaUKyRFiYmQy").strip()

# Private kanal bo‘lsa: -100... formatda bo‘ladi
# Public kanal bo‘lsa: @kanalusername ham bo‘lishi mumkin
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables ga BOT_TOKEN qo‘ying.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL topilmadi. Railway Postgres plugin qo‘shing va DATABASE_URL ni Variables ga ulang.")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID topilmadi. Railway Variables ga CHANNEL_ID qo‘ying (masalan: -100...).")

# kanal ID ni int yoki str ko‘rinishda ishlatamiz
try:
    CHANNEL_ID_CAST = int(CHANNEL_ID)
except ValueError:
    CHANNEL_ID_CAST = CHANNEL_ID  # masalan "@mychannel"


# ======================
# DB (Postgres)
# ======================
pool: Optional[asyncpg.Pool] = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS movies (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'link' | 'telegram'
    payload TEXT NOT NULL,       -- url yoki video file_id
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(CREATE_SQL)

async def close_db():
    global pool
    if pool is not None:
        await pool.close()
        pool = None

async def upsert_movie(code: str, title: str, kind: str, payload: str):
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO movies(code, title, kind, payload)
            VALUES($1, $2, $3, $4)
            ON CONFLICT(code) DO UPDATE SET
                title = EXCLUDED.title,
                kind = EXCLUDED.kind,
                payload = EXCLUDED.payload,
                created_at = NOW();
            """,
            code, title, kind, payload
        )

async def get_movie(code: str) -> Optional[Tuple[str, str, str]]:
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, kind, payload FROM movies WHERE code = $1",
            code
        )
        if not row:
            return None
        return row["title"], row["kind"], row["payload"]

async def list_movies() -> List[Tuple[str, str]]:
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, title FROM movies ORDER BY code ASC")
        return [(r["code"], r["title"]) for r in rows]


# ======================
# UI
# ======================
def main_kb() -> InlineKeyboardMarkup:
    # Siz “Kino qo‘shish” tugmasini o‘chirgansiz — shu holatda qoldirdim.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Barcha kinolar", callback_data="all_movies")],
        ]
    )

def join_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Kanalga a’zo bo‘lish", url=CHANNEL_INVITE)],
            [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")],
        ]
    )


# ======================
# SUBSCRIPTION CHECK
# ======================
async def is_subscribed(user_id: int) -> bool:
    """
    Private kanal bo‘lsa: bot kanalda ADMIN bo‘lishi shart.
    Aks holda getChatMember ishlamasligi mumkin.
    """
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID_CAST, user_id=user_id)
        # status: creator/administrator/member/left/kicked/restricted
        return member.status in ("creator", "administrator", "member")
    except Exception:
        # bot kanalda admin emas / chat_id noto‘g‘ri / telegram error
        return False

async def require_subscribed(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if not user_id:
        return False
    ok = await is_subscribed(user_id)
    if not ok:
        await message.answer(
            "❗ Botdan foydalanish uchun avval kanalga a’zo bo‘ling, so‘ng ✅ Tekshirish bosing.",
            reply_markup=join_kb()
        )
    return ok


# ======================
# FSM
# ======================
class AddMovie(StatesGroup):
    code = State()
    title = State()
    content = State()

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


async def send_movie(message: Message, title: str, kind: str, payload: str):
    if kind == "link":
        await message.answer(f"🎬 <b>{title}</b>\n🔗 {payload}", parse_mode="HTML")
        return

    await message.answer(f"🎬 <b>{title}</b>\n✅ Kino topildi, yuboryapman...", parse_mode="HTML")
    try:
        await message.answer_video(payload, caption=title)
    except Exception:
        await message.answer(f"📎 File ID:\n<code>{payload}</code>", parse_mode="HTML")


# ======================
# BOT
# ======================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_cmd(message: Message):
    # start ham subscription talab qiladi
    user_id = message.from_user.id if message.from_user else 0
    if user_id and not await is_subscribed(user_id):
        await message.answer(
            "❗ Botdan foydalanish uchun avval kanalga a’zo bo‘ling.",
            reply_markup=join_kb()
        )
        return

    name = message.from_user.first_name if message.from_user else "Foydalanuvchi"
    await message.answer(
        f'Salom, "{name}" kod yuborishingiz mumkin.',
        reply_markup=main_kb()
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery):
    user_id = call.from_user.id if call.from_user else 0
    if not user_id:
        await call.answer("Xatolik", show_alert=True)
        return

    if await is_subscribed(user_id):
        await call.message.answer("✅ A’zo bo‘ldingiz. Endi botdan foydalanishingiz mumkin.", reply_markup=main_kb())
        await call.answer()
    else:
        await call.answer("❌ Hali a’zo emassiz. Kanalga a’zo bo‘lib qayta tekshiring.", show_alert=True)


@dp.callback_query(F.data == "all_movies")
async def all_movies_cb(call: CallbackQuery):
    # subscription check
    if not await is_subscribed(call.from_user.id):
        await call.message.answer(
            "❗ Botdan foydalanish uchun avval kanalga a’zo bo‘ling.",
            reply_markup=join_kb()
        )
        await call.answer()
        return

    rows = await list_movies()
    if not rows:
        await call.message.answer("Hali kino qo‘shilmagan.")
        await call.answer()
        return

    lines = [f"{code} — {title}" for code, title in rows]

    # Ko‘p bo‘lsa bo‘lib yuboramiz
    chunk = []
    max_lines = 60
    for line in lines:
        chunk.append(line)
        if len(chunk) >= max_lines:
            await call.message.answer("📃 <b>Kinolar ro‘yxati:</b>\n" + "\n".join(chunk), parse_mode="HTML")
            chunk = []

    if chunk:
        await call.message.answer("📃 <b>Kinolar ro‘yxati:</b>\n" + "\n".join(chunk), parse_mode="HTML")

    await call.answer()


# Hamma /kino bilan kino qo‘shishi mumkin, lekin kanalga a’zo bo‘lish shart
@dp.message(Command("kino"))
async def add_movie_cmd(message: Message, state: FSMContext):
    if not await require_subscribed(message):
        return

    await state.set_state(AddMovie.code)
    await message.answer("Kino kodini yuboring (masalan: A12 yoki kino_7):")


@dp.message(AddMovie.code)
async def add_movie_code(message: Message, state: FSMContext):
    if not await require_subscribed(message):
        return

    code = (message.text or "").strip()
    if not CODE_RE.match(code):
        await message.answer("Kod noto‘g‘ri. Faqat harf/raqam/_/- ishlating, 1–32 belgi.")
        return

    await state.update_data(code=code)
    await state.set_state(AddMovie.title)
    await message.answer("Kino nomini yuboring (masalan: Fast & Furious 7):")


@dp.message(AddMovie.title)
async def add_movie_title(message: Message, state: FSMContext):
    if not await require_subscribed(message):
        return

    title = (message.text or "").strip()
    if not title:
        await message.answer("Nom bo‘sh bo‘lmasin.")
        return

    await state.update_data(title=title)
    await state.set_state(AddMovie.content)
    await message.answer(
        "Endi kino <b>link</b> yuboring yoki kinoni Telegramga <b>video</b> qilib tashlang.\n"
        "✅ Link: https://...\n"
        "✅ Video: shu chatga video yuboring",
        parse_mode="HTML"
    )


@dp.message(AddMovie.content)
async def add_movie_content(message: Message, state: FSMContext):
    if not await require_subscribed(message):
        return

    data = await state.get_data()
    code = data["code"]
    title = data["title"]

    text = (message.text or "").strip()

    # Link
    if text.startswith("http://") or text.startswith("https://"):
        await upsert_movie(code, title, "link", text)
        await state.clear()
        await message.answer(f"✅ Saqlandi!\nKod: {code}\nNomi: {title}\nTuri: link", reply_markup=main_kb())
        return

    # Video
    if message.video:
        file_id = message.video.file_id
        await upsert_movie(code, title, "telegram", file_id)
        await state.clear()
        await message.answer(f"✅ Saqlandi!\nKod: {code}\nNomi: {title}\nTuri: telegram(video)", reply_markup=main_kb())
        return

    await message.answer("Link (https://...) yoki video yuboring.")


# Kod yuborsa kino chiqaradi (lekin kanalga a’zo bo‘lish shart)
@dp.message()
async def handle_codes(message: Message):
    if not await require_subscribed(message):
        return

    text = (message.text or "").strip()
    if not text:
        return

    row = await get_movie(text)
    if not row:
        return  # topilmasa jim turadi
    title, kind, payload = row
    await send_movie(message, title, kind, payload)


# Kanal ID olish uchun yordamchi buyruq (kanaldan post forward qiling, keyin /channelid yozing)
@dp.message(Command("channelid"))
async def channel_id_help(message: Message):
    # forward qilingan postni tekshiradi
    if message.forward_from_chat:
        await message.answer(f"Kanal ID: <code>{message.forward_from_chat.id}</code>", parse_mode="HTML")
    else:
        await message.answer("Kanal postini botga FORWARD qiling, keyin /channelid yozing.")


# ======================
# MAIN
# ======================
async def main():
    await init_db()

    # polling/webhook konflikt bo‘lmasin
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[WARN] delete_webhook ishlamadi: {e}")

    try:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            except TelegramNetworkError as e:
                print(f"[NET] {e} -> 5 soniyada qayta ulanaman...")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ERR] {e} -> 5 soniyada qayta ishga tushaman...")
                await asyncio.sleep(5)
    finally:
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
