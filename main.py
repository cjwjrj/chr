
import os
import asyncio
import tempfile
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE = 49 * 1024 * 1024
MAX_RESULTS = 8

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# نخزن نتائج البحث لكل مستخدم
user_results = {}


def allowed_license(metadata):
    """السماح فقط بالتراخيص المعروفة التي تسمح بالتنزيل."""
    rights = str(metadata.get("rights", "")).lower()
    license_url = str(metadata.get("licenseurl", "")).lower()

    allowed = [
        "creativecommons.org/licenses/by/",
        "creativecommons.org/licenses/by-sa/",
        "creativecommons.org/publicdomain/",
        "creativecommons.org/publicdomain/zero/",
        "public domain",
        "public-domain",
    ]

    return any(
        item in rights or item in license_url
        for item in allowed
    )


async def get_json(session, url, params=None):
    async with session.get(
        url,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30)
    ) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def get_video_info(session, identifier):
    url = (
        "https://archive.org/metadata/"
        + quote(identifier, safe="")
    )

    try:
        data = await get_json(session, url)
        metadata = data.get("metadata", {})

        if not allowed_license(metadata):
            return None

        files = data.get("files", [])
        candidates = []

        for file in files:
            name = file.get("name", "")
            size = int(file.get("size", 0) or 0)
            lower_name = name.lower()

            if not lower_name.endswith((".mp4", ".webm", ".ogv")):
                continue

            if size <= 0 or size > MAX_SIZE:
                continue

            candidates.append((name, size))

        if not candidates:
            return None

        # نفضل MP4
        candidates.sort(
            key=lambda item: (
                not item[0].lower().endswith(".mp4"),
                item[1]
            )
        )

        filename, size = candidates[0]

        return {
            "id": identifier,
            "title": str(metadata.get("title", identifier)),
            "filename": filename,
            "size": size
        }

    except Exception:
        return None


async def search_videos(query):
    url = "https://archive.org/advancedsearch.php"

    params = {
        "q": f'mediatype:movies AND title:("{query}")',
        "fl[]": ["identifier", "title"],
        "rows": 30,
        "page": 1,
        "output": "json"
    }

    headers = {
        "User-Agent": "TelegramVideoSearchBot/1.0"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            data = await get_json(session, url, params)
            docs = data.get("response", {}).get("docs", [])

            tasks = [
                get_video_info(session, item["identifier"])
                for item in docs
                if item.get("identifier")
            ]

            results = await asyncio.gather(*tasks)

            return [
                item for item in results if item
            ][:MAX_RESULTS]

        except Exception as error:
            print("Search error:", error)
            return []


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎬 أهلاً بيك!\n\n"
        "اكتب اسم الفيديو اللي تريد تبحث عنه.\n"
        "راح أعرضلك الفيديوهات المتاحة كأزرار."
    )


@dp.message(F.text)
async def search_handler(message: Message):
    query = message.text.strip()

    if not query or query.startswith("/"):
        return

    status = await message.answer("🔎 دا أبحث عن الفيديوهات...")

    results = await search_videos(query)

    if not results:
        await status.edit_text(
            "ما لكيت فيديوهات مناسبة بهذا البحث.\n"
            "جرّب كلمة بحث ثانية."
        )
        return

    user_results[message.from_user.id] = results

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
        f"لقيت {len(results)} فيديوهات.\n"
        "اضغط على اسم الفيديو حتى أرسله إلك:",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("video:"))
async def send_video(callback: CallbackQuery):
    user_id = callback.from_user.id

    results = user_results.get(user_id, [])

    try:
        index = int(callback.data.split(":")[1])
        item = results[index]
    except (ValueError, IndexError):
        await callback.answer(
            "نتائج البحث انتهت، ابحث مرة ثانية.",
            show_alert=True
        )
        return

    await callback.answer("جاري تجهيز الفيديو...")

    status = await callback.message.answer(
        "⬇️ جاري تنزيل الفيديو..."
    )

    filename = item["filename"]
    identifier = item["id"]

    url = (
        "https://archive.org/download/"
        + quote(identifier, safe="")
        + "/"
        + quote(filename, safe="/")
    )

    path = None

    try:
        timeout = aiohttp.ClientTimeout(total=600)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(url) as response:
                response.raise_for_status()

                content_length = response.headers.get(
                    "Content-Length"
                )

                if content_length and int(content_length) > MAX_SIZE:
                    raise ValueError("الفيديو أكبر من الحد المسموح.")

                with tempfile.NamedTemporaryFile(
                    suffix=os.path.splitext(filename)[1],
                    delete=False
                ) as temp:
                    path = temp.name

                    total = 0

                    async for chunk in response.content.iter_chunked(
                        1024 * 256
                    ):
                        total += len(chunk)

                        if total > MAX_SIZE:
                            raise ValueError(
                                "حجم الفيديو أكبر من الحد المسموح."
                            )

                        temp.write(chunk)

        await status.edit_text("📤 جاري إرسال الفيديو...")

        await callback.message.answer_video(
            video=FSInputFile(path),
            caption=item["title"][:900]
        )

        await status.delete()

    except Exception as error:
        print("Download error:", error)

        await status.edit_text(
            "ما كدرت أنزّل أو أرسل هذا الفيديو.\n"
            "جرّب فيديو ثاني."
        )

    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def main():
    if not TOKEN:
        raise RuntimeError(
            "لازم تضيف BOT_TOKEN بمتغيرات البيئة."
        )

    print("Bot is running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    
