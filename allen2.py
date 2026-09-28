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
import socket
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

def allen_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": "https://allen.in",
        "Referer": "https://allen.in/",
        "X-Client-Type": "web",
        "X-Locale": "en",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

def fetch_student_info(token):
    """Fetch student profile + enrolled courses/batches from Allen Digital."""
    r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                     headers=allen_headers(token), timeout=25)
    if r.status_code == 401:
        raise ValueError("Session expire ho gaya. /login ya /token se dobara login karo.")
    data = r.json()
    if data.get("status") != 200 or not data.get("data"):
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    return data["data"]

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

def _host_resolves(url):
    try:
        host = url.split('//', 1)[1].split('/')[0]
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


def allen_login_idpass(username, password):
    """Authenticate via ALLEN Digital API (api.allen-live.in).

    Endpoint: POST /api/v1/auth/username
    Requires a DeviceID (uuid) in both header and payload.
    Success: {"status":200,"data":{"access_token":...,"refresh_token":...}}
    """
    username = str(username).strip()
    password = str(password).strip()
    if not username or not password:
        raise ValueError("Username and password cannot be empty")

    device_id = str(uuid.uuid4())
    url = "https://api.allen-live.in/api/v1/auth/username"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://allen.in",
        "Referer": "https://allen.in/",
        "X-Device-Id": device_id,
    }
    payload = {
        "username": username,
        "password": password,
        "persona_type": "STUDENT",
        "identity_type": "FORM_ID",
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=20)
    except Exception as e:
        raise ValueError(f"Login failed (network error): {e}")
    try:
        data = res.json()
    except Exception:
        data = {}
    inner = data.get("data") or {}
    token = (data.get("access_token") or data.get("token")
             or inner.get("access_token") or inner.get("token") or inner.get("accessToken")
             or res.headers.get("X-ACCESS-TOKEN") or res.headers.get("x-access-token"))
    refresh = (data.get("refresh_token") or inner.get("refresh_token") or inner.get("refreshToken")
               or res.headers.get("X-REFRESH-TOKEN") or res.headers.get("x-refresh-token") or "")
    if res.status_code == 200 and token:
        global ALLEN_BASE_URL
        ALLEN_BASE_URL = "https://api.allen-live.in/api/v1"
        session = {"access_token": token, "token": token,
                   "refresh_token": refresh,
                   "username": username, "host": "api.allen-live.in",
                   "login_at": int(time.time())}
        save_allen_session(session)
        return token
    reason = data.get("reason") or data.get("message") or res.text[:200]
    raise ValueError(f"Login failed (HTTP {res.status_code}): {reason}")



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

    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or "*" not in args[1]:
        await message.reply_text("<blockquote><i>⚠️ Format incorrect!\nUsage: <code>/login username*password</code></i></blockquote>")
        return

    username, password = (part.strip() for part in args[1].strip().split("*", 1))
    if not username or not password:
        await message.reply_text("<blockquote><i>⚠️ Username and password cannot be empty.</i></blockquote>")
        return
    status_msg = await message.reply_text("<blockquote><i>🔑 Authenticating directly with Allen Servers...</i></blockquote>")

    try:
        token = await asyncio.to_thread(allen_login_idpass, username, password)
        await status_msg.edit_text("<blockquote><i>🎉 <b>Login Successful! Session Saved.</b>\n\nNow run: <code>/mybatches</code> to see your batches</i></blockquote>")
    except Exception as e:
        logger.error(f"Login pipeline failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Login Failed:</b>\n<code>{str(e)}</code></i></blockquote>")


@app.on_message(filters.command("token") & (filters.group | filters.channel | filters.private))
async def handle_token(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    logger.info(f"/token triggered by {user_id}")

    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied. Contact Admin.</i></blockquote>")
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/token &lt;ALLEN_JWT_TOKEN&gt;</code></i></blockquote>")
        return

    token = args[1].strip()
    if token.count(".") != 2:
        await message.reply_text("<blockquote><i>⚠️ Ye valid JWT token nahi lagta. Poora token paste karo.</i></blockquote>")
        return

    try:
        # validate token against Allen API before saving
        def _check():
            r = requests.put(
                "https://api.allen-live.in/api/v1/user/profile",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={}, timeout=20,
            )
            return r
        r = await asyncio.to_thread(_check)
        if r.status_code == 401:
            await message.reply_text("<blockquote><i>❌ Token invalid ya expire ho chuka hai. Naya token nikaalo.</i></blockquote>")
            return

        session = {"access_token": token, "token": token, "username": "token-login",
                   "refresh_token": "", "host": "api.allen-live.in"}
        save_allen_session(session)
        await message.reply_text("<blockquote><i>🎉 <b>Token Saved! Session Active.</b>\n\nNow run: <code>/mybatches</code> to see your batches</i></blockquote>")
    except Exception as e:
        logger.error(f"Token save failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Token Error:</b> <code>{str(e)}</code></i></blockquote>")


@app.on_message(filters.command("mybatches") & (filters.group | filters.channel | filters.private))
async def handle_mybatches(client: Client, message: Message):
    logger.info(f"/mybatches triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    token = get_allen_token()
    if not token:
        await message.reply_text("<blockquote><i>⚠️ <b>No Active Session!</b>\n\nPlease run <code>/login username*password</code> first.</i></blockquote>")
        return

    status_msg = await message.reply_text("<blockquote><i>🔄 <b>Fetching your purchased batches...</b></i></blockquote>")
    try:
        info = await asyncio.to_thread(fetch_student_info, token)
        student = info.get("student_detail") or {}
        courses = info.get("course_details") or []
        if not courses:
            await status_msg.edit_text("<blockquote><i>❌ Is account me koi course/batch nahi mila.</i></blockquote>")
            return
        lines = [f"👤 <b>{student.get('first_name','')} {student.get('last_name','')}</b> ({student.get('stream_display_name','')})\n"]
        for c in courses:
            lines.append(f"📚 <b>{c.get('course_name','Course')}</b> (Course ID: <code>{c.get('course_id')}</code>)")
            lines.append(f"   🗓 {c.get('start_date','?')} → {c.get('end_date','?')} | Session: {c.get('session','?')}")
            for b in c.get("enrolled_batches") or []:
                lines.append(f"   ✅ Batch ID: <code>{b}</code>")
            for b in c.get("unenrolled_batches") or []:
                lines.append(f"   ➖ (unenrolled) <code>{b}</code>")
            lines.append("")
        lines.append("Download ke liye: <code>/batch &lt;BATCH_ID&gt;</code>")
        text = "\n".join(lines)
        if len(text) > MAX_TG_MSG_LEN:
            text = text[:MAX_TG_MSG_LEN] + "\n... (truncated)"
        await status_msg.edit_text(f"<blockquote>{text}</blockquote>")
    except Exception as e:
        logger.error(f"/mybatches failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Error:</b> <code>{str(e)}</code></i></blockquote>")

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
    # validate runtime environment and fail fast if missing
    missing = []
    if not TG_API_ID or TG_API_ID == 0:
        missing.append("TG_API_ID")
    if not TG_API_HASH:
        missing.append("TG_API_HASH")
    if not TG_BOT_TOKEN:
        missing.append("TG_BOT_TOKEN")
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    logger.info("Initializing workspace cleanup...")
    cleanup_workspace()
    logger.info("Workspace clean. Booting Pyrogram engine...")
    app.run()

if __name__ == "__main__":
    main()
