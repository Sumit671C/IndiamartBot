import os
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from playwright.async_api import async_playwright, Page
import requests
from contextlib import asynccontextmanager
import uvicorn

#API to enable
#https://api.telegram.org/bot7590291851:AAF8ydq6rqcmvUWBCv0BdnEOx0n5ZlSc-2Q/setWebhook?url=https://c6a7-49-36-41-247.ngrok-free.app/telegram

BOT_TOKEN = "7590291851:AAF8ydq6rqcmvUWBCv0BdnEOx0n5ZlSc-2Q"
CHAT_IDS = [859768186, -1002860729071]
SEEN_FILE = "seen_titles.txt"
TARGET_COUNTRIES = [
    "USA", "UK", "France", "Australia", "China", "Korea", "Russia",
    "New Zealand", "Turkey", "Taiwan", "Mexico", "Canada", "Thailand", "Malaysia"
]

page_global = {}
click_in_progress = asyncio.Lock()  # Lock to prevent refresh during clicks

# ------------------ Utility Functions ------------------

def load_seen_titles():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f.readlines())
    return set()

def save_seen_title(title):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(title.strip() + "\n")

def normalize_title(title: str):
    return title.strip().lower()

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
    try:
        for chat_id in CHAT_IDS:
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
    async with click_in_progress:  # Acquire lock during click
        try:
            click_page = page_global.get(f"{source}_click_page")
            await click_page.reload()

            # Scroll to load all cards (infinite scroll)
            prev_height = 0
            for _ in range(5):  # Try 5 times max
                await click_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)
                new_height = await click_page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    break
                prev_height = new_height

            # Get all cards
            cards = await click_page.locator("div.lstNw").all()
            print(f"🔍 Searching [{source}] for: {norm_title} in {len(cards)} cards")

            for card in cards:
                try:
                    title = await card.locator("h2").inner_text()
                    if normalize_title(title) == norm_title:
                        print(f"📌 Found: {title}")
                        await card.scroll_into_view_if_needed()
                        await card.hover()

                        # Traverse up to the bl_grid parent
                        bl_grid = card.locator("xpath=ancestor::div[contains(@class, 'bl_grid')]").first
                        if await bl_grid.count() == 0:
                            print(f"⚠️ bl_grid parent not found for: {title}")
                            notify_telegram(chat_id, title, False)
                            continue

                        # Extract the Contact Buyer Now button description
                        btn = bl_grid.locator("div.btnCBN.btnCBN1").first
                        if await btn.count() > 0:
                            description = await btn.get_attribute("title") or await btn.inner_text()
                            await asyncio.sleep(1)
                            await btn.click(force=True)
                            print(f"✅ Clicked Contact Buyer Now for: {title}, Description: {description}")
                            # Notify immediately after click
                            notify_telegram(chat_id, title, True, description)
                            # Wait for 5 seconds and reload to remove popup
                            await asyncio.sleep(5)
                            await click_page.reload()
                            return
                        else:
                            print(f"⚠️ Contact Buyer Now button not found in: {title}")
                            notify_telegram(chat_id, title, False)
                except Exception as e:
                    print(f"⚠️ Card error: {e}")
                    notify_telegram(chat_id, title, False)
                    continue

            print(f"❌ Not found: {norm_title} on [{source}]")
            notify_telegram(chat_id, norm_title, False)

        except Exception as e:
            print(f"❌ trigger_click error: {e}")
            notify_telegram(chat_id, norm_title, False)

# ------------------ Scan Loop ------------------

async def scan_loop(page: Page, label: str):
    seen = load_seen_titles()
    source = label.replace("_scan", "")
    print(f"🚀 Started scanning loop for [{label}]...")
    while True:
        try:
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
                            seen.add(norm_title)
                            save_seen_title(norm_title)
                except Exception as e:
                    print(f"⚠️ Error parsing card #{idx+1}: {e}")
        except Exception as e:
            print(f"❌ Scan error on [{label}]: {e}")
        await asyncio.sleep(5)

# ------------------ Refresh Loop ------------------

async def refresh_loop():
    while True:
        if not click_in_progress.locked():  # Only refresh if no click is in progress
            for key in ["recent_click_page", "relevant_click_page"]:
                if page := page_global.get(key):
                    print(f"🔄 Refreshing {key}")
                    await page.reload()
        await asyncio.sleep(300)  # Refresh every 5 minutes (300 seconds)

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
        asyncio.create_task(refresh_loop())  # Start refresh loop
        yield

app.router.lifespan_context = lifespan

# ------------------ Entry ------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)