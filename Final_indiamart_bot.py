import os
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from playwright.async_api import async_playwright, Page
import requests
from contextlib import asynccontextmanager
import uvicorn

BOT_TOKEN = "7590291851:AAF8ydq6rqcmvUWBCv0BdnEOx0n5ZlSc-2Q"
CHAT_IDS = [859768186, -1002860729071]
SEEN_FILE = "seen_titles_v2.txt"
TARGET_COUNTRIES = [
    "USA", "UK", "France", "Australia", "China", "Korea", "Russia", "Italy",
    "New Zealand", "Turkey", "Taiwan", "Mexico", "Canada", "Thailand", "Malaysia"
]

page_global = {}
click_in_progress = asyncio.Lock()

# ------------------ Utility Functions ------------------

def normalize_title(title: str):
    return title.strip().lower()

def save_seen_title(title):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()}|{normalize_title(title)}\n")

def load_seen_titles(hours=24):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    ts_str, title = line.strip().split("|", 1)
                    ts = datetime.fromisoformat(ts_str)
                    if ts >= cutoff:
                        seen.add(title.strip())
                except ValueError:
                    continue
    return seen

async def cleanup_seen_titles(days=7):
    while True:
        await asyncio.sleep(86400)
        cutoff = datetime.utcnow() - timedelta(days=days)
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            with open(SEEN_FILE, "w", encoding="utf-8") as f:
                for line in lines:
                    try:
                        ts_str, _ = line.strip().split("|", 1)
                        if datetime.fromisoformat(ts_str) >= cutoff:
                            f.write(line)
                    except ValueError:
                        continue
        print("🧹 Cleaned up old seen titles.")

def send_telegram_message_with_button(title, message, source):
    for chat_id in CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "📞 Contact Buyer", "callback_data": f"contact::{source}::{normalize_title(title)}"}
                ]]
            }
        }
        try:
            res = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
            print(f"{'✅ Sent' if res.ok else '❌ Failed'} to {chat_id}")
        except Exception as e:
            print(f"❌ Telegram error: {e}")

def notify_telegram(chat_id, title, success, description=None):
    msg = f"✅ Contacted: {title}\nDescription: {description}" if success and description else f"✅ Contacted: {title}" if success else f"❌ Failed to contact: {title}"
    for chat_id in CHAT_IDS:
        try:
            response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg})
            if not response.ok:
                print(f"❌ Telegram notification failed for chat_id {chat_id}: {response.text}")
        except Exception as e:
            print(f"❌ Telegram notification error: {e}")

# ------------------ FastAPI App ------------------

app = FastAPI()

@app.post("/telegram")
async def telegram_webhook(req: Request, background_tasks: BackgroundTasks):
    data = await req.json()
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["from"]["id"]
        callback_id = cb["id"]
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id})
        if cb["data"].startswith("contact::"):
            parts = cb["data"].split("::", 2)
            if len(parts) == 3:
                source, title = parts[1], parts[2]
                background_tasks.add_task(trigger_click, chat_id, title, source)
    return {"ok": True}

# ------------------ Trigger Buyer Click ------------------

async def trigger_click(chat_id, norm_title, source):
    async with click_in_progress:
        try:
            click_page = page_global.get(f"{source}_click_page")
            await click_page.reload()

            prev_height = 0
            for _ in range(5):
                await click_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)
                new_height = await click_page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    break
                prev_height = new_height

            cards = await click_page.locator("div.lstNw").all()
            print(f"🔍 Searching [{source}] for: {norm_title} in {len(cards)} cards")

            for card in cards:
                try:
                    title = await card.locator("h2").inner_text()
                    if normalize_title(title) == norm_title:
                        print(f"📌 Found: {title}")
                        await card.scroll_into_view_if_needed()
                        await card.hover()
                        bl_grid = card.locator("xpath=ancestor::div[contains(@class, 'bl_grid')]").first
                        if await bl_grid.count() == 0:
                            notify_telegram(chat_id, title, False)
                            continue
                        btn = bl_grid.locator("div.btnCBN.btnCBN1").first
                        if await btn.count() > 0:
                            description = await btn.get_attribute("title") or await btn.inner_text()
                            await asyncio.sleep(1)
                            await btn.click(force=True)
                            notify_telegram(chat_id, title, True, description)
                            await asyncio.sleep(5)
                            await click_page.reload()
                            return
                        else:
                            notify_telegram(chat_id, title, False)
                except Exception as e:
                    print(f"⚠️ Card error: {e}")
                    notify_telegram(chat_id, title, False)
            notify_telegram(chat_id, norm_title, False)
        except Exception as e:
            print(f"❌ trigger_click error: {e}")
            notify_telegram(chat_id, norm_title, False)

# ------------------ Scan Loop ------------------

async def scan_loop(page: Page, label: str):
    source = label.replace("_scan", "")
    print(f"🚀 Started scanning loop for [{label}]...")
    while True:
        try:
            seen = load_seen_titles()
            await page.reload()
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            cards = await page.locator("div.lstNw").all()
            print(f"📆 Found {len(cards)} cards on [{label}]")
            for idx, card in enumerate(cards):
                try:
                    raw = await card.inner_text()
                    if any(c.lower() in raw.lower() for c in TARGET_COUNTRIES):
                        title = await card.locator("h2").inner_text()
                        norm_title = normalize_title(title)
                        if norm_title not in seen:
                            msg = f"🌍 New Lead ({label}): {title}\n\n{raw[:300]}..."
                            send_telegram_message_with_button(title, msg, source)
                            save_seen_title(title)
                except Exception as e:
                    print(f"⚠️ Error parsing card #{idx+1}: {e}")
        except Exception as e:
            print(f"❌ Scan error on [{label}]: {e}")
        await asyncio.sleep(5)

# ------------------ Refresh Loop ------------------

async def refresh_loop():
    while True:
        if not click_in_progress.locked():
            for key in ["recent_click_page", "relevant_click_page"]:
                if page := page_global.get(key):
                    print(f"🔄 Refreshing {key}")
                    await page.reload()
        await asyncio.sleep(300)

# ------------------ Lifespan ------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context("userdata", headless=False)
        recent_scan = await browser.new_page()
        relevant_scan = await browser.new_page()
        recent_click_page = await browser.new_page()
        relevant_click_page = await browser.new_page()

        page_global.update({
            "recent_scan": recent_scan,
            "relevant_scan": relevant_scan,
            "recent_click_page": recent_click_page,
            "relevant_click_page": relevant_click_page
        })

        await recent_scan.goto("https://seller.indiamart.com/bltxn/?pref=recent")
        await relevant_scan.goto("https://seller.indiamart.com/bltxn/?pref=relevant")
        await recent_click_page.goto("https://seller.indiamart.com/bltxn/?pref=recent")
        await relevant_click_page.goto("https://seller.indiamart.com/bltxn/?pref=relevant")

        input("➡️ Login to all 4 pages, then press Enter...")

        asyncio.create_task(scan_loop(recent_scan, "recent_scan"))
        asyncio.create_task(scan_loop(relevant_scan, "relevant_scan"))
        asyncio.create_task(refresh_loop())
        asyncio.create_task(cleanup_seen_titles())  # Daily cleanup
        yield

app.router.lifespan_context = lifespan

# ------------------ Entry ------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
