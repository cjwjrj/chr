import asyncio
import ipaddress
import logging
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote_plus

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv
import aiosqlite
import yt_dlp

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "8"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
CRAWL_LIMIT = int(os.getenv("CRAWL_LIMIT", "35"))
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=18, connect=8)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video-bot")

if not BOT_TOKEN:
    raise RuntimeError("ضع BOT_TOKEN داخل ملف .env")
if not ADMIN_IDS:
    raise RuntimeError("ضع رقم حسابك في ADMIN_IDS داخل ملف .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


class AddSite(StatesGroup):
    waiting_url = State()


class Search(StatesGroup):
    waiting_query = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def home_keyboard(user_id: int):
    rows = [
        [InlineKeyboardButton(text="🔎 البحث عن فيديو", callback_data="search")],
        [InlineKeyboardButton(text="🌐 المواقع المضافة", callback_data="sites")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="➕ إضافة موقع", callback_data="add_site")])
        rows.append([InlineKeyboardButton(text="🗑 حذف موقع", callback_data="delete_site_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            added_by INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()


def normalize_site_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("الرابط فارغ.")
    if "://" not in raw:
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("أرسل رابطاً صحيحاً يبدأ بـ http أو https.")
    if p.username or p.password:
        raise ValueError("الروابط التي تحتوي بيانات دخول غير مسموحة.")
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/') or '/'}"


async def assert_public_host(url: str):
    """Block localhost/private/reserved IPs before making outbound requests."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("رابط غير صالح.")
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("هذا النطاق غير مسموح.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                                              type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("تعذر العثور على عنوان الموقع.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("الموقع يشير إلى عنوان خاص أو غير عام، وتم رفضه.")


async def safe_get(session: aiohttp.ClientSession, url: str):
    current = url
    for _ in range(5):
        await assert_public_host(current)
        async with session.get(current, allow_redirects=False,
                               headers={"User-Agent": "Mozilla/5.0 (compatible; PublicVideoSearchBot/1.0)"}) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    return current, resp.status, "", resp.headers
                current = urljoin(current, loc)
                continue
            if resp.status >= 400:
                return current, resp.status, "", resp.headers
            ctype = resp.headers.get("Content-Type", "").lower()
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                return current, resp.status, "", resp.headers
            body = await resp.text(errors="ignore")
            return current, resp.status, body, resp.headers
    raise ValueError("الموقع يحوّل الرابط مرات كثيرة.")


async def get_sites():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, url FROM sites ORDER BY id DESC")
        return await cur.fetchall()


async def add_site(url: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO sites(url, added_by) VALUES(?, ?)", (url, user_id))
        await db.commit()


async def remove_site(site_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sites WHERE id=?", (site_id,))
        await db.commit()


VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|m4v|mov|mkv|m3u8)(?:$|[?#])", re.I)


def collect_video_candidates(soup: BeautifulSoup, page_url: str):
    found = []
    # Native HTML5 video players and source elements.
    for tag in soup.find_all(["video", "source"]):
        for attr in ("src", "data-src", "data-video", "data-url"):
            value = tag.get(attr)
            if value:
                found.append(urljoin(page_url, value))
        if tag.name == "video":
            for source in tag.find_all("source"):
                value = source.get("src") or source.get("data-src")
                if value:
                    found.append(urljoin(page_url, value))

    # Common social/video metadata and embedded players.
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
            value = tag.get("content")
            if value:
                found.append(urljoin(page_url, value))

    for tag in soup.find_all(["iframe", "embed"]):
        value = tag.get("src") or tag.get("data-src")
        if value:
            found.append(urljoin(page_url, value))

    # Direct video links, including links hosted on a media CDN.
    for tag in soup.find_all(["a", "link"], href=True):
        href = urljoin(page_url, tag["href"])
        if VIDEO_EXT_RE.search(urlparse(href).path + (("?" + urlparse(href).query) if urlparse(href).query else "")):
            found.append(href)

    # Lazy-loaded player attributes used by many sites.
    for tag in soup.find_all(True):
        for attr in ("data-src", "data-video", "data-url", "data-file", "data-hls"):
            value = tag.get(attr)
            if isinstance(value, str) and VIDEO_EXT_RE.search(value):
                found.append(urljoin(page_url, value))

    return list(dict.fromkeys(u for u in found if urlparse(u).scheme in ("http", "https")))


async def yt_dlp_can_extract(page_url: str):
    """Check whether yt-dlp recognizes a page as a playable video without downloading it."""
    def work():
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": False, "socket_timeout": 12, "playlistend": 1}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            if not info:
                return None
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                info = next((entry for entry in entries if entry), None)
            if not info:
                return None
            title = info.get("title") or info.get("id") or page_url
            # Keep the original page URL; yt-dlp will resolve the stream at download time.
            if info.get("formats") or info.get("url") or info.get("requested_formats"):
                return str(title)[:180]
            return None
    try:
        return await asyncio.to_thread(work)
    except Exception:
        return None



async def crawl_xnxx_arabic_search(site_url: str, query: str):
    """Search the site's public results page and list video-detail links."""
    parsed = urlparse(site_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    term = quote_plus(query.strip()).replace("+", "-")
    search_url = f"{base}/search/{term}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    results, seen = [], set()

    async with aiohttp.ClientSession(
        timeout=REQUEST_TIMEOUT,
        connector=aiohttp.TCPConnector(limit=6),
        headers=headers,
    ) as session:
        final_url, status, html, _ = await safe_get(session, search_url)
        if not html:
            raise ValueError(
                f"الموقع لم يرجع صفحة نتائج قابلة للقراءة (HTTP {status}). "
                "قد تكون الصفحة معتمدة على JavaScript أو تمنع الطلبات الآلية."
            )

        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(final_url, a["href"].split("#")[0])
            p = urlparse(href)
            if p.scheme not in ("http", "https") or p.hostname != parsed.hostname:
                continue

            path = p.path.lower()
            if "/search/" in path or not re.search(r"/video[^/]*", path):
                continue
            if href in seen:
                continue

            title = (
                a.get("title") or a.get("aria-label")
                or a.get_text(" ", strip=True)
            ).strip()
            if not title:
                img = a.find("img")
                if img:
                    title = (img.get("alt") or img.get("title") or "").strip()
            if not title:
                card = a.find_parent(
                    ["div", "article", "li"],
                    class_=re.compile(r"(thumb|video|mozaique|thumb-block)", re.I),
                )
                if card:
                    title = card.get_text(" ", strip=True)
            if not title:
                continue

            seen.add(href)
            results.append({
                "title": re.sub(r"\s+", " ", title)[:180],
                "page": href,
                "videos": [href],
                "score": 1,
            })
            if len(results) >= MAX_RESULTS:
                break

    return results


async def crawl_search(site_url: str, query: str):
    """Find playable videos on a site; do not return ordinary article/page results."""
    parsed_site = urlparse(site_url)
    if (parsed_site.hostname or "").lower().removeprefix("www.") == "xnxx-arabic.com":
        return await crawl_xnxx_arabic_search(site_url, query)
    parsed_root = urlparse(site_url)
    root_host = parsed_root.hostname
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PublicVideoSearchBot/1.0)"}
    connector = aiohttp.TCPConnector(limit=8)
    qwords = [w.lower() for w in re.findall(r"[\w\u0600-\u06ff]+", query) if len(w) > 1]
    results = []
    seen_video_keys = set()

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector, headers=headers) as session:
        first_url, status, html, _ = await safe_get(session, site_url)
        if not html:
            raise ValueError(f"تعذر قراءة الصفحة الرئيسية (HTTP {status}).")

        queue = [first_url]
        seen_pages = set()
        while queue and len(seen_pages) < CRAWL_LIMIT and len(results) < MAX_RESULTS:
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)

            try:
                final_url, code, page_html, _ = await safe_get(session, page_url)
            except Exception:
                continue
            if not page_html:
                continue

            soup = BeautifulSoup(page_html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            text = soup.get_text(" ", strip=True)
            haystack = (title + " " + text[:16000] + " " + final_url).lower()
            score = sum(1 for word in qwords if word in haystack)

            # Only consider pages matching the user's search terms as video candidates.
            if score:
                page_video_urls = collect_video_candidates(soup, final_url)
                for video_url in page_video_urls:
                    key = video_url.split("#")[0]
                    if key in seen_video_keys:
                        continue
                    seen_video_keys.add(key)
                    results.append({
                        "title": title or "فيديو من الموقع",
                        "page": final_url,
                        "videos": [video_url],
                        "score": score
                    })
                    if len(results) >= MAX_RESULTS:
                        break

                # If no direct stream is exposed in HTML, try supported yt-dlp extractors.
                if not page_video_urls and len(results) < MAX_RESULTS:
                    extracted_title = await yt_dlp_can_extract(final_url)
                    if extracted_title:
                        key = "page:" + final_url
                        if key not in seen_video_keys:
                            seen_video_keys.add(key)
                            results.append({
                                "title": extracted_title,
                                "page": final_url,
                                "videos": [final_url],
                                "score": score
                            })

            # Discover same-site pages; prioritize query matches in anchor text and URL.
            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(final_url, a["href"].split("#")[0])
                parsed = urlparse(href)
                if parsed.scheme not in ("http", "https") or parsed.hostname != root_host:
                    continue
                if href in seen_pages or href in queue:
                    continue
                if any(part in parsed.path.lower() for part in
                       ("/login", "/register", "/account", "/cart", "/logout", "/privacy", "/terms")):
                    continue
                label = (a.get_text(" ", strip=True) + " " + href).lower()
                priority = sum(1 for word in qwords if word in label)
                links.append((priority, href))
            links.sort(key=lambda item: item[0], reverse=True)
            queue.extend(href for _, href in links[:15])

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:MAX_RESULTS]
def results_keyboard(results):
    rows = []
    for i, item in enumerate(results):
        label = item["title"][:48] or f"نتيجة {i+1}"
        rows.append([InlineKeyboardButton(text=f"🎬 {label}", callback_data=f"result:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def download_video(url: str, folder: str):
    # yt-dlp handles common video pages and direct media links.
    outtmpl = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": f"best[filesize<{MAX_UPLOAD_MB}M]/best",
        "max_filesize": MAX_UPLOAD_MB * 1024 * 1024,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 20,
    }
    def work():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    return await asyncio.to_thread(work)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "أهلاً بك في بوت البحث عن الفيديوهات 🎬\n"
        "اختر البحث، ثم اختر الموقع واكتب عبارة البحث.\n"
        "ملاحظة: نجاح البحث يعتمد على إمكانية قراءة صفحات الموقع.",
        reply_markup=home_keyboard(message.from_user.id)
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "طريقة الاستخدام:\n"
        "1) اضغط البحث عن فيديو.\n"
        "2) اختر موقعاً من القائمة.\n"
        "3) اكتب اسم الفيديو.\n"
        "4) اختر نتيجة لإرسال الفيديو أو فتح صفحته.\n\n"
        "إضافة وحذف المواقع متاحان للمدير فقط."
    )


@dp.callback_query(F.data == "add_site")
async def add_site_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("هذه الخاصية للمدير فقط.", show_alert=True)
        return
    await state.set_state(AddSite.waiting_url)
    await call.message.answer("أرسل رابط الموقع الذي تريد إضافته:")
    await call.answer()


@dp.message(AddSite.waiting_url)
async def add_site_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        url = normalize_site_url(message.text or "")
        await assert_public_host(url)
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            _, status, html, _ = await safe_get(session, url)
        if not html:
            await message.answer(f"لم أستطع قراءة الموقع (HTTP {status}). تأكد أن الصفحة عامة.")
            return
        await add_site(url, message.from_user.id)
        await message.answer("✅ تمت إضافة الموقع وحفظه.", reply_markup=home_keyboard(message.from_user.id))
        await state.clear()
    except aiosqlite.IntegrityError:
        await message.answer("هذا الموقع مضاف مسبقاً.")
        await state.clear()
    except Exception as e:
        await message.answer(f"تعذرت إضافة الموقع: {str(e)[:250]}")
        await state.clear()


@dp.callback_query(F.data == "sites")
async def show_sites(call: CallbackQuery):
    sites = await get_sites()
    if not sites:
        await call.message.answer("لا توجد مواقع مضافة حالياً.")
    else:
        body = "\n".join(f"{i}. {url}" for i, url in sites)
        await call.message.answer("🌐 المواقع المضافة:\n" + body[:3500])
    await call.answer()


@dp.callback_query(F.data == "delete_site_menu")
async def delete_site_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("هذه الخاصية للمدير فقط.", show_alert=True)
        return
    sites = await get_sites()
    if not sites:
        await call.answer("لا توجد مواقع لحذفها.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=f"حذف: {url[:38]}", callback_data=f"delete:{sid}")]
            for sid, url in sites[:30]]
    await call.message.answer("اختر الموقع الذي تريد حذفه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("delete:"))
async def delete_site_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("للمدير فقط.", show_alert=True)
        return
    sid = int(call.data.split(":")[1])
    await remove_site(sid)
    await call.answer("تم حذف الموقع.")
    await call.message.answer("🗑 تم حذف الموقع.")


@dp.callback_query(F.data == "search")
async def search_start(call: CallbackQuery, state: FSMContext):
    sites = await get_sites()
    if not sites:
        await call.answer("لا توجد مواقع مضافة بعد.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=url[:48], callback_data=f"choose:{sid}")]
            for sid, url in sites[:30]]
    await call.message.answer("اختر الموقع الذي تريد البحث داخله:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("choose:"))
async def choose_site(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[1])
    sites = dict(await get_sites())
    if sid not in sites:
        await call.answer("الموقع لم يعد موجوداً.", show_alert=True)
        return
    await state.set_state(Search.waiting_query)
    await state.update_data(site_url=sites[sid])
    await call.message.answer("اكتب اسم الفيديو أو كلمات البحث:")
    await call.answer()


@dp.message(Search.waiting_query)
async def search_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("اكتب كلمتين أو حرفين على الأقل.")
        return
    await message.answer("🔎 جارٍ البحث داخل الموقع... قد يستغرق بعض الوقت.")
    try:
        results = await crawl_search(data["site_url"], query)
        if not results:
            await message.answer(
                "لم أعثر على صفحات مطابقة.\n"
                "قد يكون الموقع يعتمد على JavaScript أو يتطلب تسجيل دخول أو يستخدم بحثاً داخلياً خاصاً."
            )
            await state.clear()
            return
        await state.update_data(results=results)
        await message.answer("اختر نتيجة:", reply_markup=results_keyboard(results))
        await state.set_state(None)  # keep results in FSM storage for result buttons
    except Exception as e:
        await message.answer(f"حدث خطأ أثناء البحث: {str(e)[:250]}")
        await state.clear()


@dp.callback_query(F.data.startswith("result:"))
async def send_result(call: CallbackQuery, state: FSMContext):
    # Results are cached per user to avoid cross-user result confusion.
    # Telegram callback data only contains the result index; state may expire.
    data = await state.get_data()
    results = data.get("results")
    idx = int(call.data.split(":")[1])
    if not results or idx >= len(results):
        await call.answer("انتهت صلاحية النتائج. أعد البحث من فضلك.", show_alert=True)
        return
    item = results[idx]
    urls = item.get("videos") or [item["page"]]
    await call.answer("أحاول تجهيز الفيديو...")
    await call.message.answer(f"⏬ جاري تجهيز: {item['title'][:150]}")
    with tempfile.TemporaryDirectory() as tmp:
        for url in urls[:3]:
            try:
                await assert_public_host(url)
                path = await download_video(url, tmp)
                if not path.exists() or path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                    continue
                await call.message.answer_video(
                    video=FSInputFile(str(path)),
                    caption=item["title"][:900],
                    supports_streaming=True
                )
                return
            except Exception as e:
                log.info("Download failed for %s: %s", url, e)
        await call.message.answer(
            "لم أتمكن من تنزيل الفيديو تلقائياً. قد يكون الرابط محمياً أو غير مباشر أو أكبر من الحد المسموح.\n"
            f"يمكنك فتح صفحة النتيجة: {item['page']}"
        )


async def main():
    await init_db()
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
