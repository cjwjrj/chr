import logging
import os
from html import escape
from typing import Any

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "").strip()
BRAVE_VIDEO_ENDPOINT = "https://api.search.brave.com/res/v1/videos/search"
MAX_RESULTS = 10
TIMEOUT = aiohttp.ClientTimeout(total=25)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("video-search-bot")


def adult_confirmed(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("adult_confirmed"))


async def brave_video_search(query: str) -> list[dict[str, Any]]:
    """Search video pages through Brave Search API; does not download media."""
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {
        "q": query,
        "count": MAX_RESULTS,
        "safesearch": "off",
    }

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(
            BRAVE_VIDEO_ENDPOINT, headers=headers, params=params
        ) as response:
            body = await response.text()
            if response.status != 200:
                log.error("Brave API error %s: %s", response.status, body[:500])
                raise RuntimeError(
                    f"خدمة البحث رجّعت HTTP {response.status}. "
                    "تأكد من مفتاح Brave وصلاحية Video Search."
                )
            data = await response.json(content_type=None)

    results = data.get("results", [])
    clean = []
    for item in results[:MAX_RESULTS]:
        url = item.get("url") or item.get("profile", {}).get("url")
        title = item.get("title") or "فيديو بدون عنوان"
        description = item.get("description") or ""
        thumbnail = item.get("thumbnail", {})
        if isinstance(thumbnail, dict):
            thumbnail = thumbnail.get("src") or thumbnail.get("url")
        if url and url.startswith(("https://", "http://")):
            clean.append({
                "title": title,
                "url": url,
                "description": description,
                "thumbnail": thumbnail if isinstance(thumbnail, str) else None,
                "duration": item.get("duration"),
                "views": item.get("views"),
                "creator": item.get("creator"),
            })
    return clean


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["adult_confirmed"] = False
    keyboard = [[
        InlineKeyboardButton("عمري 18 سنة أو أكثر — متابعة", callback_data="adult_yes")
    ]]
    await update.message.reply_text(
        "أهلاً! هذا بوت بحث فيديوهات.\n\n"
        "المحتوى المخصص للبالغين قد يظهر في نتائج البحث. "
        "لا تتابع إلا إذا كان عمرك 18 سنة أو أكثر، "
        "والتزم بالقوانين وحقوق أصحاب المحتوى.\n\n"
        "اضغط للتأكيد:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_adult(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "adult_yes":
        context.user_data["adult_confirmed"] = True
        await query.edit_message_text(
            "تم التأكيد. أرسل كلمة البحث الآن.\n"
            "مثال: اكتب عبارة البحث التي تريدها."
        )


async def do_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, search_text: str
) -> None:
    if not adult_confirmed(context):
        await update.effective_message.reply_text(
            "اكتب /start أولاً وأكّد أن عمرك 18 سنة أو أكثر."
        )
        return

    search_text = search_text.strip()
    if not search_text:
        await update.effective_message.reply_text(
            "اكتب كلمة البحث بعد الأمر، مثال:\n/search your keywords"
        )
        return
    if len(search_text) > 180:
        await update.effective_message.reply_text("كلمة البحث طويلة جداً (الحد 180 حرفاً).")
        return

    status = await update.effective_message.reply_text("🔎 جاري البحث عن الفيديوهات...")
    try:
        results = await brave_video_search(search_text)
    except Exception as exc:
        log.exception("Search failed")
        await status.edit_text(f"تعذر إكمال البحث:\n{escape(str(exc))}")
        return

    if not results:
        await status.edit_text("ما حصلت نتائج. جرّب كلمة بحث ثانية.")
        return

    await status.edit_text(f"لقيت {len(results)} نتيجة (الحد الأقصى {MAX_RESULTS}):")
    for index, item in enumerate(results, start=1):
        title = escape(item["title"][:250])
        description = escape((item["description"] or "")[:450])
        details = []
        if item.get("duration"):
            details.append(f"المدة: {escape(str(item['duration']))}")
        if item.get("creator"):
            details.append(f"الناشر: {escape(str(item['creator'])[:100])}")
        if item.get("views"):
            details.append(f"المشاهدات: {escape(str(item['views']))}")
        suffix = "\n" + " · ".join(details) if details else ""
        text = f"<b>{index}. {title}</b>"
        if description:
            text += f"\n{description}"
        text += suffix
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ فتح الفيديو في المصدر", url=item["url"])
        ]])
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_search(update, context, " ".join(context.args))


async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_search(update, context, update.effective_message.text or "")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "طريقة الاستخدام:\n"
        "1) /start ثم تأكيد العمر.\n"
        "2) أرسل كلمة البحث مباشرة، أو استخدم /search كلمة البحث.\n\n"
        "يعرض البوت حتى 10 نتائج فيديو مع روابط المصدر. "
        "لا ينزّل الفيديوهات ولا يتجاوز قيود المواقع أو حقوق النشر."
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود في ملف .env")
    if not BRAVE_API_KEY:
        raise SystemExit("BRAVE_API_KEY غير موجود في ملف .env")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CallbackQueryHandler(confirm_adult, pattern=r"^adult_yes$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_search))

    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
