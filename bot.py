import asyncio
import re
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ======================
# CONFIG
# ======================

import os
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi.")


if not BOT_TOKEN or "BU_YERGA" in BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN ni bot.py ichida to‘g‘ri qo‘ying.")


# ======================
# DB
# ======================

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS movies (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'link' | 'telegram'
    payload TEXT NOT NULL,       -- url yoki video file_id
    created_at INTEGER NOT NULL  -- unix time
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def upsert_movie(code: str, title: str, kind: str, payload: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO movies(code, title, kind, payload, created_at)
            VALUES(?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(code) DO UPDATE SET
                title=excluded.title,
                kind=excluded.kind,
                payload=excluded.payload,
                created_at=strftime('%s','now')
            """,
            (code, title, kind, payload),
        )
        await db.commit()

async def get_movie(code: str) -> Optional[Tuple[str, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT title, kind, payload FROM movies WHERE code = ?",
            (code,),
        )
        row = await cur.fetchone()
        return row if row else None

async def list_movies() -> list[Tuple[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT code, title FROM movies ORDER BY code ASC")
        return await cur.fetchall()


# ======================
# UI
# ======================

def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Barcha kinolar", callback_data="all_movies")],
            [InlineKeyboardButton(text="➕ Kino qo‘shish", callback_data="add_movie")],
        ]
    )


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

    # Telegram video file_id
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
    name = message.from_user.first_name if message.from_user else "Foydalanuvchi"
    await message.answer(
        f'Salom, "{name}" kod yuborishingiz mumkin.',
        reply_markup=main_kb()
    )


@dp.callback_query(F.data == "all_movies")
async def all_movies_cb(call: CallbackQuery):
    rows = await list_movies()
    if not rows:
        await call.message.answer("Hali kino qo‘shilmagan.")
        await call.answer()
        return

    lines = [f"{code} — {title}" for code, title in rows]

    # ko‘p bo‘lsa bo‘lib yuboradi
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


@dp.callback_query(F.data == "add_movie")
async def add_movie_button(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddMovie.code)
    await call.message.answer("Kino kodini yuboring (masalan: A12 yoki kino_7):")
    await call.answer()


# Istalgan odam /kino bilan kino qo‘shadi
@dp.message(Command("kino"))
async def add_movie_cmd(message: Message, state: FSMContext):
    await state.set_state(AddMovie.code)
    await message.answer("Kino kodini yuboring (masalan: A12 yoki kino_7):")


@dp.message(AddMovie.code)
async def add_movie_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not CODE_RE.match(code):
        await message.answer("Kod noto‘g‘ri. Faqat harf/raqam/_/- ishlating, 1–32 belgi.")
        return

    await state.update_data(code=code)
    await state.set_state(AddMovie.title)
    await message.answer("Kino nomini yuboring (masalan: Fast & Furious 7):")


@dp.message(AddMovie.title)
async def add_movie_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Nom bo‘sh bo‘lmasin.")
        return

    await state.update_data(title=title)
    await state.set_state(AddMovie.content)
    await message.answer(
        "Endi kino <b>link</b> yuboring yoki kinoni Telegramga <b>video</b> qilib tashlang.\n"
        "✅ Link bo‘lsa: https://... yuboring\n"
        "✅ Video bo‘lsa: videoni shu chatga yuboring.",
        parse_mode="HTML"
    )


@dp.message(AddMovie.content)
async def add_movie_content(message: Message, state: FSMContext):
    data = await state.get_data()
    code = data["code"]
    title = data["title"]

    # 1) Link
    text = (message.text or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        await upsert_movie(code, title, "link", text)
        await state.clear()
        await message.answer(f"✅ Saqlandi!\nKod: {code}\nNomi: {title}\nTuri: link", reply_markup=main_kb())
        return

    # 2) Telegram video
    if message.video:
        file_id = message.video.file_id
        await upsert_movie(code, title, "telegram", file_id)
        await state.clear()
        await message.answer(f"✅ Saqlandi!\nKod: {code}\nNomi: {title}\nTuri: telegram(video)", reply_markup=main_kb())
        return

    await message.answer("Link (https://...) yoki video yuboring.")


# Foydalanuvchi kod yuborsa kino chiqadi
@dp.message()
async def handle_codes(message: Message):
    text = (message.text or "").strip()
    if not text:
        return

    row = await get_movie(text)
    if not row:
        return  # topilmasa jim turadi
    title, kind, payload = row
    await send_movie(message, title, kind, payload)


# ======================
# MAIN (UZILSA HAM QAYTA ULANDI)
# ======================

async def main():
    await init_db()

    # Polling/webhook konflikt bo‘lmasin:
    await bot.delete_webhook(drop_pending_updates=True)

    while True:
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except TelegramNetworkError as e:
            print(f"[NET] {e} -> 5 soniyada qayta ulanaman...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[ERR] {e} -> 5 soniyada qayta ishga tushaman...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
