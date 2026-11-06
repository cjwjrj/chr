
import os
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE = 49 * 1024 * 1024
MAX_RESULTS = 8

if not TOKEN:
    raise RuntimeError("ضع BOT_TOKEN في متغيرات البيئة أولاً")

bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# نتائج البحث لكل مستخدم
user_results = {}


async def fetch_json(session, url, params=None):
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


def has_allowed_license(metadata):
    rights = str(metadata.get("rights", "")).lower()
    license_url = str(metadata.get("licenseurl", "")).lower()

    allowed = (
        "creativecommons.org/licenses/by/",
        "creativecommons.org/licenses/by-sa/",
        "creativecommons.org/publicdomain/",
        "creativecommons.org/publicdomain/zero/",
        "public domain",
    )

    return any(
        item in rights or item in license_url
        for item in allowed
    )


async def inspect_item(session, identifier):
    url = f"https://archive.org/metadata/{quote(identifier)}"

    try:
        data = await fetch_json(session, url)
        metadata = data.get("metadata", {})

        if not has_allowed_license(metadata):
            return None

        video_files = []

        for file in data.get("files", []):
            name = file.get("name", "")
            size = int(file.get("size", 0) or 0)

            if (
                name.lower().endswith((".mp4", ".webm", ".ogv"))
                and 0 < size <= MAX_SIZE
            ):
                video_files.append((name, size))

        if not video_files:
            return None

        # نفضل MP4
        video_files.sort(
            key=lambda item: (
                not item[0].lower().endswith(".mp4"),
                item[1]
            )
        )

        filename, size = video_files[0]

        return {
            "id": identifier,
            "title": str(metadata.get("title", identifier)),
            "filename": filename,
            "size": size,
        }

    except Exception as error:
        print("Item error:", error)
        return None


async def search_videos(query):
    url = "https://archive.org/advancedsearch.php"

    params = [
        ("q", f'mediatype:movies AND title:("{query}")'),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("rows", "30"),
        ("page", "1"),
        ("output", "json"),
    ]

    headers = {
        "User-Agent": "TelegramVideoSearchBot/1.0"
    }

    timeout = aiohttp.ClientTimeout(total=40)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        try:
            data = await fetch_json(session, url, params)
            docs = data.get("response", {}).get("docs", [])

            identifiers = [
                item["identifier"]
                for item in docs
                if item.get("identifier")
            ]

            tasks = [
                inspect_item(session, identifier)
                for identifier in identifiers
            ]

            checked = await asyncio.gather(*tasks)

            return [item for item in checked if item][:MAX_RESULTS]

        except Exception as error:
            print("Search error:", error)
            return []


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎬 أهلاً بيك ببوت البحث عن الفيديوهات.\n\n"
        "اكتب اسم الفيديو حتى أبحث عنه.\n"
        "راح تظهر النتائج كأزرار، اضغط على أي فيديو حتى أرسله إلك."
    )


@dp.message(F.text)
async def search_handler(message: Message):
    query = message.text.strip()

    if not query or query.startswith("/"):
        return

    status = await message.answer("🔎 جاري البحث عن الفيديوهات...")

    results = await search_videos(query)

    if not results:
        await status.edit_text(
            "ما حصلت فيديوهات مطابقة بترخيص تنزيل مناسب.\n"
            "جرّب كلمة بحث ثانية."
        )
        return

    user_id = message.from_user.id
    user_results[user_id] = results

    buttons = []

    for index, item in enumerate(results):
        title = item["title"][:55]

        buttons.append([
            InlineKeyboardButton(
                text=f"🎬 {title}",
                callback_data=f"video:{index}"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await status.edit_text(
        f"وجدت {len(results)} فيديوهات.\n"
        "اضغط على اسم الفيديو لإرساله:",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("video:"))
async def send_video(callback: CallbackQuery):
    results = user_results.get(callback.from_user.id, [])

    try:
        index = int(callback.data.split(":")[1])
        item = results[index]
    except (ValueError, IndexError):
        await callback.answer(
            "انتهت نتائج البحث. ابحث مرة ثانية.",
            show_alert=True
        )
        return

    await callback.answer("جاري تجهيز الفيديو...")

    status = await callback.message.answer("⬇️ جاري تنزيل الفيديو...")

    identifier = item["id"]
    filename = item["filename"]

    url = (
        f"https://archive.org/download/{quote(identifier)}/"
        f"{quote(filename, safe='/')}"
    )

    path = None

    try:
        timeout = aiohttp.ClientTimeout(total=600)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_SIZE:
                    raise ValueError("حجم الفيديو أكبر من الحد المسموح")

                suffix = Path(filename).suffix or ".mp4"

                with tempfile.NamedTemporaryFile(
                    suffix=suffix,
                    delete=False
                ) as temp:
                    path = temp.name
                    total = 0

                    async for chunk in response.content.iter_chunked(256 * 1024):
                        total += len(chunk)

                        if total > MAX_SIZE:
                            raise ValueError("حجم الفيديو أكبر من الحد المسموح")

                        temp.write(chunk)

        await status.edit_text("📤 جاري إرسال الفيديو...")

        await callback.message.answer_video(
            FSInputFile(path),
            caption=item["title"][:900]
        )

        await status.delete()

    except Exception as error:
        print("Download/send error:", error)

        await status.edit_text(
            "تعذر تنزيل أو إرسال هذا الفيديو. جرّب نتيجة ثانية."
        )

    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def main():
    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
