import os
import asyncio
import json
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from playwright.async_api import async_playwright, Page
import requests
import subprocess
from contextlib import asynccontextmanager
import uvicorn

CHAT_IDS = [-1002860729071]
LOCAL_URL = "http://localhost:8000"
BOT_TOKEN = "7590291851:AAF8ydq6rqcmvUWBCv0BdnEOx0n5ZlSc-2Q"
SEEN_FILE = r"C:\Users\schandwani\Desktop\IndiaMartBot\app\seen_titles.txt"
COOKIES_PATH = r"C:\Users\schandwani\Desktop\IndiaMartBot\app\novasys_cookies.json"
CLOUDFLARED_PATH = r"C:\Users\schandwani\Desktop\cloudflared-windows-amd64.exe"
TARGET_COUNTRIES = [
    "USA", "UK", "France", "Australia", "China", "Korea", "Russia", "Italy", "Philippines",
    "New Zealand", "Turkey", "Taiwan", "Mexico", "Canada", "Thailand", "Malaysia", "Saudi Arabia"
]

page_global = {}
click_in_progress = asyncio.Lock()
current_tunnel_url = None
webhook_set = asyncio.Event()

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

async def set_telegram_webhook(public_url, browser_context, max_retries=5, retry_delay=30):
    global current_tunnel_url
    webhook_url = f"{public_url}/telegram"
    if current_tunnel_url == public_url:
        print(f"ℹ️ Skipping webhook setup for unchanged URL: {webhook_url}")
        webhook_set.set()
        return True
    webhook_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    print(f"🌐 Setting Telegram webhook to: {webhook_url}")
    # Initial delay to allow DNS propagation
    print(f"⏳ Waiting 10 seconds for DNS propagation...")
    await asyncio.sleep(10)
    
    for attempt in range(1, max_retries + 1):
        try:
            # Verify URL accessibility
            print(f"🔍 Verifying URL accessibility (attempt {attempt}/{max_retries}): {public_url}")
            response = requests.get(public_url, timeout=15)
            if response.ok:
                print(f"✅ URL {public_url} is accessible")
            else:
                print(f"⚠️ URL {public_url} returned status code: {response.status_code}")
        except Exception as e:
            print(f"⚠️ Failed to verify URL {public_url}: {e}")

        try:
            # Open webhook URL in browser
            print(f"🌐 Opening webhook URL in browser (attempt {attempt}/{max_retries}): {webhook_api_url}")
            page = await browser_context.new_page()
            await page.goto(webhook_api_url, timeout=60000)
            content = await page.content()
            print(f"📄 Browser response: {content[:200]}...")
            # Keep tab open for 5 seconds to allow manual inspection
            print(f"⏳ Keeping browser tab open for 5 seconds to inspect response...")
            await asyncio.sleep(5)
            await page.close()
            # Check webhook status
            status_response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=15)
            if status_response.ok and status_response.json().get("result", {}).get("url") == webhook_url:
                print(f"✅ Successfully set Telegram webhook to {webhook_url} via browser")
                current_tunnel_url = public_url
                webhook_set.set()
                return True
            else:
                print(f"❌ Webhook not set via browser (attempt {attempt}/{max_retries}): {status_response.text}")
                if status_response.status_code == 429:
                    retry_after = status_response.json().get("parameters", {}).get("retry_after", 1)
                    print(f"⏳ Rate limited by Telegram API, waiting {retry_after} seconds...")
                    await asyncio.sleep(retry_after)
        except Exception as e:
            print(f"❌ Error setting webhook via browser (attempt {attempt}/{max_retries}): {e}")
            await page.close()

        if attempt < max_retries:
            print(f"⏳ Retrying in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
    print(f"❌ Failed to set Telegram webhook after {max_retries} attempts")
    current_tunnel_url = None
    return False

async def start_tunnel(browser_context):
    global current_tunnel_url
    print(f"🚀 Starting Cloudflare tunnel with {CLOUDFLARED_PATH}")
    error_count = 0
    max_errors = 3
    while True:
        try:
            process = subprocess.Popen(
                [CLOUDFLARED_PATH, "tunnel", "--url", LOCAL_URL],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            print("✅ Cloudflare tunnel process started")

            # Stream output and capture public URL
            url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
            async for line in async_iter_lines(process):
                print(f"📜 Tunnel output: {line.strip()}")
                if "context canceled" in line.lower():
                    error_count += 1
                    print(f"⚠️ Detected tunnel error #{error_count}/{max_errors}: context canceled")
                    if error_count >= max_errors:
                        print("❌ Too many tunnel errors, restarting tunnel")
                        process.terminate()
                        error_count = 0
                        break
                match = url_pattern.search(line)
                if match:
                    public_url = match.group(0)
                    print(f"🌐 Detected public URL: {public_url}")
                    success = await set_telegram_webhook(public_url, browser_context)
                    if not success:
                        print("❌ Webhook setup failed, restarting tunnel")
                        process.terminate()
                        break
            # Wait for process to complete or handle restart
            return_code = await asyncio.get_event_loop().run_in_executor(None, process.wait)
            print(f"❌ Tunnel stopped with code {return_code}, restarting in 5 seconds...")
            current_tunnel_url = None
            webhook_set.clear()
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ Tunnel error: {e}")
            current_tunnel_url = None
            webhook_set.clear()
            await asyncio.sleep(5)

async def async_iter_lines(process):
    print("🔄 Starting async output iterator for tunnel")
    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    def enqueue_output():
        try:
            for line in iter(process.stdout.readline, ''):
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, f"Error: {e}")

    loop.run_in_executor(None, enqueue_output)
    while True:
        line = await queue.get()
        if line is None or line.startswith("Error:"):
            print(f"🔄 Tunnel output iterator closed: {line if line else 'EOF'}")
            break
        yield line

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

async def trigger_click(chat_id, norm_title, source):
    async with click_in_progress:  # concurrency-safe
        try:
            click_page = page_global.get(f"{source}_click_page")
            await click_page.reload()
            await asyncio.sleep(0.3)  # short wait for DOM

            async def find_and_click(cards):
                # Parallelize fetching titles
                titles = await asyncio.gather(
                    *[card.locator("h2").inner_text() for card in cards]
                )

                for title_text, card in zip(titles, cards):
                    if normalize_title(title_text) == norm_title:
                        print(f"📌 Found: {title_text}")
                        await card.scroll_into_view_if_needed()
                        await card.hover()

                        bl_grid = card.locator("xpath=ancestor::div[contains(@class, 'bl_grid')]").first
                        if await bl_grid.count() == 0:
                            print(f"⚠️ bl_grid parent not found for: {title_text}")
                            notify_telegram(chat_id, title_text, False)
                            return True  # stop search

                        btn = bl_grid.locator("div.btnCBN.btnCBN1").first
                        if await btn.count() > 0:
                            description = await btn.get_attribute("title") or await btn.inner_text()
                            await btn.click(force=True)  # ✅ use proper browser click
                            print(f"✅ Clicked Contact Buyer Now for: {title_text}, Description: {description}")
                            await asyncio.sleep(3)  # shorter wait for popup
                            await click_page.reload()
                            notify_telegram(chat_id, title_text, True, description)
                            return True
                        else:
                            print(f"⚠️ Contact Buyer Now button not found in: {title_text}")
                            notify_telegram(chat_id, title_text, False)
                            return True
                return False  # not found in current cards

            # 🔍 First try without scrolling
            cards = await click_page.locator("div.lstNw").all()
            print(f"🔍 Initial search [{source}] for: {norm_title} in {len(cards)} cards")
            if await find_and_click(cards):
                return

            # 🔍 Then scroll up to 5 times
            prev_height = 0
            for scroll_attempt in range(5):
                await click_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)  # shorter delay
                new_height = await click_page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    print(f"ℹ️ No more content to scroll after {scroll_attempt+1} attempts.")
                    break
                prev_height = new_height

                cards = await click_page.locator("div.lstNw").all()
                print(f"🔍 Scroll attempt {scroll_attempt+1}, searching {len(cards)} cards")
                if await find_and_click(cards):
                    return

            # ❌ Still not found
            print(f"❌ Not found: {norm_title} on [{source}]")
            notify_telegram(chat_id, norm_title, False)

        except Exception as e:
            print(f"❌ trigger_click error: {e}")
            notify_telegram(chat_id, norm_title, False)

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

async def refresh_loop():
    while True:
        if not click_in_progress.locked():
            for key in ["recent_click_page", "relevant_click_page"]:
                if page := page_global.get(key):
                    print(f"🔄 Refreshing {key}")
                    await page.reload()
        await asyncio.sleep(300)

async def set_cookies_from_file(context, cookie_path):
    with open(cookie_path, "r", encoding="utf-8") as f:
        cookies = json.load(f)
        await context.add_cookies(cookies)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1280,800"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )

        # Inject stealth JS
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        await set_cookies_from_file(context, COOKIES_PATH)

        recent_scan = await context.new_page()
        relevant_scan = await context.new_page()
        recent_click_page = await context.new_page()
        relevant_click_page = await context.new_page()

        page_global.update({
            "recent_scan": recent_scan,
            "relevant_scan": relevant_scan,
            "recent_click_page": recent_click_page,
            "relevant_click_page": relevant_click_page
        })

        await recent_scan.goto("https://seller.indiamart.com/bltxn/?pref=recent", timeout=60000)
        await relevant_scan.goto("https://seller.indiamart.com/bltxn/?pref=relevant", timeout=60000)
        await recent_click_page.goto("https://seller.indiamart.com/bltxn/?pref=recent", timeout=60000)
        await relevant_click_page.goto("https://seller.indiamart.com/bltxn/?pref=relevant", timeout=60000)

        print("🔄 Starting background tasks")
        asyncio.create_task(start_tunnel(context))
        asyncio.create_task(scan_loop(recent_scan, "recent_scan"))
        asyncio.create_task(scan_loop(relevant_scan, "relevant_scan"))
        asyncio.create_task(refresh_loop())
        asyncio.create_task(cleanup_seen_titles())

        yield

app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
