import os
import sys
import json
import time
import gc
import shutil
import asyncio
import requests
import subprocess
import logging
import uuid
from pyrogram import Client, filters, enums
from pyrogram.types import Message

# ==========================================
# LOGGING & ENVIRONMENT CONFIGURATION
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "6789039689"))

if not TG_BOT_TOKEN:
    logger.warning("TG_BOT_TOKEN is empty! Pyrogram will hang in CMD waiting for manual input.")

AUTH_FILE = "authorized_users.json"
SESSION_FILE = "allen_session.json"
DOWNLOAD_DIR = "./downloads"

def load_authorized_users():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                return set(int(u) for u in json.load(f))
        except Exception as e:
            logger.error(f"Error loading auth file: {e}")
    return {OWNER_ID}

def save_authorized_users(users_set):
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(list(users_set), f)
    except Exception as e:
        logger.error(f"Error saving auth file: {e}")

AUTHORIZED_USERS = load_authorized_users()

def is_user_authorized(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in AUTHORIZED_USERS

# ==========================================
# SESSION MANAGEMENT (TOKEN & CREDS)
# ==========================================
def save_allen_session(data):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving session: {e}")

def get_allen_session():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading session: {e}")
    return {}

def get_allen_token():
    session = get_allen_session()
    return session.get("access_token") or session.get("token")

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=16,
    parse_mode=enums.ParseMode.HTML
)

ACTIVE_JOBS = {}
MAX_TG_MSG_LEN = 4000
ALLEN_BASE_URL = "https://api.allen-live.in/api/v1"

def cleanup_workspace():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for item in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, item)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"Cleanup lock on {item} (ignoring): {e}")

# ==========================================
# ID * PASS RECON & AUTHENTICATION ENGINE
# ==========================================

def allen_login_idpass(username, password):
    """Direct ID*PASS Authentication Flow with multiple DeviceID strategies and debugging."""
    login_endpoints = [
        "https://api.allen-live.in/api/v1/auth/username",
        "https://api.allen.in/v1/auth/login",
        "https://api.allen.ac.in/v1/auth/login"
    ]

    device_id = str(uuid.uuid4())

    # Payload variants (include multiple key spellings)
    payload = {
        "username": username,
        "password": password,
        "grant_type": "password",
        "DeviceID": device_id,
        "deviceId": device_id,
        "device_id": device_id
    }

    # Header base + multiple DeviceID forms (HTTP header names are case-insensitive but some servers check specific keys)
    base_headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://api.allen.in",
        "Referer": "https://api.allen.in/"
    }
    device_header_variants = [
        ("DeviceID", device_id),
        ("device-id", device_id),
        ("deviceId", device_id),
        ("deviceid", device_id),
        ("X-Device-Id", device_id),
        ("x-device-id", device_id),
        ("X-DeviceID", device_id),
    ]

    error_log = []

    for endpoint in login_endpoints:
        logger.info(f"Attempting auth via {endpoint} (device_id={device_id})...")
        # 1) Try JSON + common device headers (add a few variants)
        headers = base_headers.copy()
        # attach a couple of common variants simultaneously
        for k, v in device_header_variants[:3]:
            headers[k] = v
        try:
            res = requests.post(endpoint, json=payload, headers=headers, timeout=15)
            # Log what we sent (debug)
            try:
                logger.debug(f"Request to {endpoint} headers: {res.request.headers}")
                body = res.request.body
n                try:
                    logger.debug(f"Request body: {body.decode() if isinstance(body, bytes) else body}")
                except Exception:
                    logger.debug("Request body could not be decoded for logging.")
            except Exception:
                logger.debug("Could not log request details")

            if res.status_code == 200:
                data = res.json()
                token = data.get("access_token") or data.get("token") or data.get("data", {}).get("token")
                if token:
                    save_allen_session({"username": username, "token": token, "login_time": time.time()})
                    logger.info(f"Auth successful on {endpoint} (json + headers)")
                    return token
            else:
                err_msg = f"[{endpoint.split('//')[1].split('/')[0]}] HTTP {res.status_code}: {res.text[:200]}"
                error_log.append(err_msg)
                logger.warning(err_msg)

            # 2) If server returned 400 DeviceID empty, try form-encoded
            if res.status_code == 400 and "DeviceID" in (res.text or ""):
                headers2 = base_headers.copy()
                headers2["Content-Type"] = "application/x-www-form-urlencoded"
                for k, v in device_header_variants[:4]:
                    headers2[k] = v
                res2 = requests.post(endpoint, data=payload, headers=headers2, timeout=15)
                try:
                    logger.debug(f"Form request to {endpoint} headers: {res2.request.headers}")
                    logger.debug(f"Form request body: {res2.request.body}")
                except Exception:
                    logger.debug("Form request body could not be decoded for logging.")
                if res2.status_code == 200:
                    data = res2.json()
                    token = data.get("access_token") or data.get("token") or data.get("data", {}).get("token")
                    if token:
                        save_allen_session({"username": username, "token": token, "login_time": time.time()})
                        logger.info(f"Auth successful on {endpoint} (form + headers)")
                        return token
                else:
                    err_msg = f"[{endpoint.split('//')[1].split('/')[0]}] Form HTTP {res2.status_code}: {res2.text[:200]}"
                    error_log.append(err_msg)
                    logger.warning(err_msg)

            # 3) If still failing, try passing device id as a query param
            params = {"device_id": device_id}
            res3 = requests.post(endpoint, json=payload, headers=base_headers, params=params, timeout=15)
            try:
                logger.debug(f"Query-param request to {endpoint} url: {res3.url}")
            except Exception:
                pass
            if res3.status_code == 200:
                data = res3.json()
                token = data.get("access_token") or data.get("token") or data.get("data", {}).get("token")
                if token:
                    save_allen_session({"username": username, "token": token, "login_time": time.time()})
                    logger.info(f"Auth successful on {endpoint} (json + params)")
                    return token
            else:
                err_msg = f"[{endpoint.split('//')[1].split('/')[0]}] Params HTTP {res3.status_code}: {res3.text[:200]}"
                error_log.append(err_msg)
                logger.warning(err_msg)

        except Exception as e:
            err_msg = f"[{endpoint.split('//')[1].split('/')[0]}] Request failed: {e}"
            error_log.append(err_msg)
            logger.warning(err_msg)

    combined_errors = "\n".join(error_log)
    raise ValueError(f"Login failed across all endpoints:\n{combined_errors}")

def fetch_batch_contents(batch_id, token):
    url = f"{ALLEN_BASE_URL}/batch/{batch_id}/contents"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    
    res = requests.get(url, headers=headers, timeout=25)
    res.raise_for_status()
    data = res.json()
    
    data_items = []
    raw_list = data if isinstance(data, list) else data.get("data", data.get("items", []))
    
    for item in raw_list:
        title = item.get("title") or item.get("topic_name") or item.get("name") or "Lecture"
        m3u8_url = item.get("url") or item.get("m3u8_url") or item.get("stream_url") or item.get("video_url")
        pdf_url = item.get("pdf_url") or item.get("document_url")
        is_new_chapter = item.get("is_new_chapter", False)
        
        if m3u8_url or pdf_url:
            data_items.append({
                "title": title,
                "url": m3u8_url,
                "pdf": pdf_url,
                "is_new_chapter": is_new_chapter
            })
            
    return data_items

# ==========================================
# HIGH SPEED DOWNLOAD & UPLOAD ENGINE
# ==========================================

def download_m3u8(m3u8_url, output_name, bearer_token=None):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    cmd = [
        "N_m3u8DL-RE",
        m3u8_url,
        "--save-name", output_name,
        "--save-dir", DOWNLOAD_DIR,
        "--auto-select",
        "--thread-count", "16",
        "--download-retry-count", "10",
        "--no-log"
    ]
    if bearer_token:
        cmd.extend(["--header", f"Authorization: Bearer {bearer_token}"])

    subprocess.run(cmd, check=True)
    output_path = os.path.join(DOWNLOAD_DIR, f"{output_name}.mp4")
    
    if not os.path.exists(output_path):
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(output_name):
                return os.path.join(DOWNLOAD_DIR, file)
    return output_path

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None):
    return await asyncio.to_thread(download_m3u8, m3u8_url, output_name, bearer_token)

def upload_to_telegram(app_client, target_chat_id, file_path, caption):
    return app_client.send_video(
        chat_id=target_chat_id,
        video=file_path,
        caption=caption,
        supports_streaming=True
    )

async def async_upload_to_telegram(app_client, target_chat_id, file_path, caption):
    return await asyncio.to_thread(upload_to_telegram, app_client, target_chat_id, file_path, caption)

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

@app.on_message(filters.command("auth") & filters.private)
async def handle_auth(client: Client, message: Message):
    logger.info(f"/auth triggered by {message.from_user.id}")
    if message.from_user.id != OWNER_ID:
        await message.reply_text("<blockquote><i>🚫 Owner-only command.</i></blockquote>")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/auth add &lt;user_id&gt;</code> or <code>/auth remove &lt;user_id&gt;</code></i></blockquote>")
        return

    action, uid_str = args[1].lower(), args[2]
    try:
        target_uid = int(uid_str)
        if action == "add":
            AUTHORIZED_USERS.add(target_uid)
            save_authorized_users(AUTHORIZED_USERS)
            await message.reply_text(f"<blockquote><i>✅ User <code>{target_uid}</code> Authorized!</i></blockquote>")
        elif action == "remove":
            AUTHORIZED_USERS.discard(target_uid)
            save_authorized_users(AUTHORIZED_USERS)
            await message.reply_text(f"<blockquote><i>❌ User <code>{target_uid}</code> Revoked!</i></blockquote>")
    except ValueError:
        await message.reply_text("<blockquote><i>⚠️ Invalid User ID.</i></blockquote>")

@app.on_message(filters.command("login") & (filters.group | filters.channel | filters.private))
async def handle_login(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    logger.info(f"/login triggered by {user_id}")

    if message.from_user and not is_user_authorized(message.from_user.id):
        logger.warning(f"Unauthorized access attempt by {user_id}")
        await message.reply_text("<blockquote><i>🚫 Access Denied. Contact Admin.</i></blockquote>")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or "*" not in args[1]:
        await message.reply_text("<blockquote><i>⚠️ Format incorrect!\nUsage: <code>/login username*password</code></i></blockquote>")
        return

    username, password = args[1].strip().split("*", 1)
    status_msg = await message.reply_text("<blockquote><i>🔑 Authenticating directly with Allen Servers...</i></blockquote>")

    try:
        token = await asyncio.to_thread(allen_login_idpass, username, password)
        await status_msg.edit_text("<blockquote><i>🎉 <b>Login Successful! Session Saved.</b>\n\nNow run: <code>/batch &lt;BATCH_ID&gt;</code></i></blockquote>")
    except Exception as e:
        logger.error(f"Login pipeline failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Login Failed:</b>\n<code>{str(e)}</code></i></blockquote>")

@app.on_message(filters.command("batch") & (filters.group | filters.channel | filters.private))
async def handle_batch(client: Client, message: Message):
    logger.info(f"/batch triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    token = get_allen_token()
    if not token:
        await message.reply_text("<blockquote><i>⚠️ <b>No Active Session!</b>\n\nPlease run <code>/login username*password</code> first.</i></blockquote>")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/batch &lt;BATCH_ID&gt;</code></i></blockquote>")
        return

    batch_id = args[1].strip()
    target_chat_id = message.chat.id

    status_msg = await message.reply_text("<blockquote><i>🔄 <b>Fetching Full Batch Tree from Allen Server...</b></i></blockquote>")

    try:
        data_items = await asyncio.to_thread(fetch_batch_contents, batch_id, token)
        if not data_items:
            await status_msg.edit_text("<blockquote><i>❌ No items found in this Batch ID.</i></blockquote>")
            return

        ACTIVE_JOBS[target_chat_id] = {"running": True}
        await status_msg.edit_text(f"<blockquote><i>🚀 <b>Processing {len(data_items)} Content Items... High-Speed Engine Active!</b></i></blockquote>")

        for idx, item in enumerate(data_items, start=1):
            if not ACTIVE_JOBS.get(target_chat_id, {}).get("running", False):
                await message.reply_text("<blockquote><i>🛑 Download Job Cancelled.</i></blockquote>")
                break

            title = item.get("title", f"Lecture_{idx}")
            clean_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).rstrip()

            if item.get("url"):
                video_path = await async_download_m3u8(item["url"], clean_title, token)
                caption = f"<blockquote><i><b>{title}</b>\n\nAllen High-Speed Auto-Downloader</i></blockquote>"
                
                await async_upload_to_telegram(app, target_chat_id, video_path, caption)
                
                if os.path.exists(video_path):
                    os.remove(video_path)
                gc.collect()

        await message.reply_text("<blockquote><i>✅ <b>Batch Execution Finished Completely!</b></i></blockquote>")

    except Exception as e:
        logger.error(f"Batch execution failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Batch Error:</b> <code>{str(e)}</code></i></blockquote>")
    finally:
        ACTIVE_JOBS[target_chat_id] = {"running": False}

@app.on_message(filters.command("stop"))
async def handle_stop(client: Client, message: Message):
    chat_id = message.chat.id
    if ACTIVE_JOBS.get(chat_id, {}).get("running"):
        ACTIVE_JOBS[chat_id]["running"] = False
        await message.reply_text("<blockquote><i>🛑 Stopping active download task...</i></blockquote>")
    else:
        await message.reply_text("<blockquote><i>⚠️ No active task running in this chat.</i></blockquote>")

@app.on_message(filters.command("id"))
async def show_id(client: Client, message: Message):
    await message.reply_text(f"<blockquote><i>🆔 Chat ID: <code>{message.chat.id}</code></i></blockquote>")

def main():
    logger.info("Initializing workspace cleanup...")
    cleanup_workspace()
    logger.info("Workspace clean. Booting Pyrogram engine...")
    app.run()

if __name__ == "__main__":
    main()
