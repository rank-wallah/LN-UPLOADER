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
ALLEN_USERNAME = os.getenv("ALLEN_USERNAME", "").strip()
ALLEN_PASSWORD = os.getenv("ALLEN_PASSWORD", "").strip()
ALLEN_ACCESS_TOKEN = os.getenv("ALLEN_ACCESS_TOKEN", "").strip()

if not TG_BOT_TOKEN:
    logger.warning("TG_BOT_TOKEN is empty! Pyrogram will hang in CMD waiting for manual input.")

AUTH_FILE = "authorized_users.json"
SESSION_FILE = "allen_session.json"
DOWNLOAD_DIR = "./downloads"
RUNTIME_ALLEN_TOKEN = ""

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
    if RUNTIME_ALLEN_TOKEN:
        return RUNTIME_ALLEN_TOKEN
    session = get_allen_session()
    return session.get("access_token") or session.get("token") or ALLEN_ACCESS_TOKEN

def get_allen_device_id():
    """Use one stable device identity for login and every authenticated request."""
    session = get_allen_session()
    device_id = session.get("device_id") or os.getenv("ALLEN_DEVICE_ID", "").strip()
    if not device_id:
        # Stable across Heroku deploys without exposing the bot token itself.
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"allen-bot:{TG_BOT_TOKEN}"))
        session["device_id"] = device_id
        save_allen_session(session)
    return device_id

def allen_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": get_allen_device_id(),
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Origin": "https://allen.in",
        "Referer": "https://allen.in/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": 'Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": "Windows",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "X-Client-Type": "web",
        "X-Locale": "en",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    }

def allen_content_headers(token):
    """Match Allen web's content-request fingerprint exactly.

    X-Device-Id belongs to login/session requests. Sending it to pages/getPage
    can make that endpoint return HTTP 200 with an empty widgets list.
    """
    headers = allen_headers(token).copy()
    headers.pop("X-Device-Id", None)
    headers.pop("Accept", None)
    headers.pop("Accept-Language", None)
    return headers

def fetch_student_info(token):
    """Fetch student profile + enrolled courses/batches from Allen Digital."""
    r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                     headers=allen_headers(token), timeout=25)
    if r.status_code == 401:
        if ALLEN_USERNAME and ALLEN_PASSWORD:
            token = allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
            r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_headers(token), timeout=25)
        if r.status_code == 401:
            raise ValueError("Allen session expire hai aur automatic login configure nahi hai.")
    data = r.json()
    if data.get("status") != 200 or not data.get("data"):
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    return data["data"]

def get_or_login_allen_token(force_login=False):
    """Restore a configured token, or sign in automatically after a restart/expiry."""
    token = None if force_login else get_allen_token()
    if token:
        check = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_headers(token), timeout=25)
        if check.status_code != 401:
            return token
    if ALLEN_USERNAME and ALLEN_PASSWORD:
        return allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
    raise ValueError(
        "Allen session saved nahi hai. Permanent login ke liye Heroku Config Vars me "
        "ALLEN_USERNAME aur ALLEN_PASSWORD set karo, phir redeploy karo.")

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=4,
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

    device_id = get_allen_device_id()
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
        global ALLEN_BASE_URL, RUNTIME_ALLEN_TOKEN
        ALLEN_BASE_URL = "https://api.allen-live.in/api/v1"
        RUNTIME_ALLEN_TOKEN = token
        session = {"access_token": token, "token": token,
                   "refresh_token": refresh,
                   "device_id": device_id,
                   "username": username, "host": "api.allen-live.in",
                   "login_at": int(time.time())}
        save_allen_session(session)
        return token
    reason = data.get("reason") or data.get("message") or res.text[:200]
    raise ValueError(f"Login failed (HTTP {res.status_code}): {reason}")



# ==========================================
# BATCH CONTENT DISCOVERY (pages/getPage API)
# ==========================================
from urllib.parse import urlencode

ALLEN_PAGE_URL = "https://api.allen-live.in/api/v1/pages/getPage"
ALLEN_TAXONOMY = os.getenv("ALLEN_TAXONOMY", "1739171216OJ")
ALLEN_SUBJECTS = [("Physics", "1160"), ("Chemistry", "746"), ("Mathematics", "1264")]
SUBJECT_ALIASES = {
    "p": "Physics", "phy": "Physics", "physics": "Physics",
    "c": "Chemistry", "chem": "Chemistry", "chemistry": "Chemistry",
    "m": "Mathematics", "math": "Mathematics", "maths": "Mathematics",
    "mathematics": "Mathematics",
}
# speed tuning (env-overridable)
DL_THREADS = os.getenv("DL_THREADS", "48")
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3"))


def normalize_subject(raw):
    """'phy' -> 'Physics'; 'all'/None -> None (= all subjects)."""
    if not raw:
        return None
    key = str(raw).strip().lower()
    if key in ("all", "*", "sab"):
        return None
    if key not in SUBJECT_ALIASES:
        raise ValueError("Subject galat hai. Use: physics / chemistry / maths / all")
    return SUBJECT_ALIASES[key]
DONE_FILE = "uploaded_contents.json"


def load_done():
    if os.path.exists(DONE_FILE):
        try:
            with open(DONE_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def mark_done(content_id, done_set):
    done_set.add(content_id)
    try:
        with open(DONE_FILE, "w") as f:
            json.dump(list(done_set), f)
    except Exception as e:
        logger.warning(f"Could not persist progress: {e}")


def allen_get_page(page_url, token):
    r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                      headers=allen_content_headers(token), timeout=30)
    if r.status_code == 401:
        if ALLEN_USERNAME and ALLEN_PASSWORD:
            token = allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
            r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                              headers=allen_content_headers(token), timeout=30)
        if r.status_code == 401:
            raise ValueError("Allen session expire hai aur automatic login configure nahi hai.")
    try:
        data = r.json()
    except Exception:
        raise ValueError(f"Allen page error (HTTP {r.status_code}): {r.text[:150]}")
    if data.get("status") != 200:
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    page_data = data.get("data") or {}
    widgets = ((page_data.get("page_content") or {}).get("widgets")
               if isinstance(page_data, dict) else None)
    if not widgets:
        logger.warning(
            "Allen empty page: http=%s reason=%s path=%s data_keys=%s",
            r.status_code, data.get("reason"), page_url.split("?", 1)[0],
            list(page_data.keys()) if isinstance(page_data, dict) else [])
    return page_data


def _walk_page(node, contents, chapters):
    """Recursively collect playable contents and chapter/topic links from a page tree."""
    if isinstance(node, dict):
        for key in ("content_action", "card_action"):
            action = node.get(key)
            if isinstance(action, dict):
                d = action.get("data") or {}
                uri, title = d.get("uri"), d.get("title")
                if uri and title and str(uri).startswith("http"):
                    subtitle = node.get("subtitle") or ""
                    name = f"{str(subtitle)[:10]} {title}".strip() if subtitle else title
                    contents.append({"id": d.get("content_id") or uri, "title": name, "url": uri})
        action = node.get("action")
        if isinstance(action, dict):
            q = (action.get("data") or {}).get("query") or {}
            cur = (action.get("tracking_params") or {}).get("current") or {}
            if q.get("topic_id"):
                chapters.append({"topic_id": q["topic_id"],
                                 "topic_name": cur.get("topic_name") or "Topic",
                                 "subject_id": cur.get("subject_id")})
        for value in node.values():
            _walk_page(value, contents, chapters)
    elif isinstance(node, list):
        for value in node:
            _walk_page(value, contents, chapters)


def _course_params(batch_ids, selected_batches, course_id, stream, subject_id,
                   topic_id=None, taxonomy_id=None):
    params = {
        "batch_id": ",".join(batch_ids),
        "selected_batch_list": ",".join(selected_batches),
        "selected_course_id": course_id,
        "stream": stream,
        "subject_id": subject_id,
        "taxonomy_id": taxonomy_id or ALLEN_TAXONOMY,
    }
    if topic_id:
        params["topic_id"] = topic_id
    return urlencode(params)


def _taxonomy_candidates(info, course):
    """Return account/course taxonomy IDs, with the known web value as fallback."""
    found = []

    def scan(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower().replace("_", "") in ("taxonomy", "taxonomyid"):
                    if child and isinstance(child, (str, int)):
                        found.append(str(child))
                elif isinstance(child, (dict, list)):
                    scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)

    scan(course)
    scan(info.get("student_detail") or {})
    found.extend([ALLEN_TAXONOMY, "1739171216OJ"])
    return list(dict.fromkeys(v for v in found if v))


def fetch_batch_contents(batch_id=None, token=None, subject=None):
    """All downloadable items of one batch, or of ALL purchased batches when batch_id is None/'all'."""
    if not token:
        raise ValueError("No token. /login ya /token pehle karo.")
    info = fetch_student_info(token)
    student = info.get("student_detail") or {}
    student_stream = student.get("stream") or ""
    courses = info.get("course_details") or []

    wants_all = (not batch_id) or str(batch_id).strip().lower() in ("all", "*")
    if not wants_all:
        matched = [c for c in courses if batch_id in
                   (list(c.get("enrolled_batches") or []) + list(c.get("unenrolled_batches") or []))]
        if not matched:
            known = sum((list(c.get("enrolled_batches") or []) for c in courses), [])
            raise ValueError(
                "Batch ID account ke enrolled batches me nahi mila. /mybatches se ✅ wala exact ID copy karo. "
                f"Found {len(known)} enrolled batch(es).")
        courses = matched

    items, seen = [], set()

    def _add(prefix, found):
        for it in found:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append({"id": it["id"], "title": f"{prefix} {it['title']}".strip(), "url": it["url"]})

    for course in courses:
        selected_batches = list(course.get("enrolled_batches") or []) + list(course.get("unenrolled_batches") or [])
        if not selected_batches:
            continue
        # Allen's web client normally sends the complete course batch list in BOTH
        # fields.  Some accounts accept only the chosen batch, so that is a fallback.
        batch_variants = [(selected_batches, selected_batches)]
        if not wants_all:
            chosen = [str(batch_id)]
            batch_variants.append((chosen, selected_batches))
            batch_variants.append((chosen, chosen))
        cname = course.get("course_name") or "Course"
        cid = course.get("course_id") or ""
        # Current studentInfo responses can expose the API enum on the student
        # and a display value on the course. Try both rather than assuming one.
        stream_variants = list(dict.fromkeys(str(v) for v in (
            student_stream, course.get("stream"), course.get("stream_name")
        ) if v))
        if not stream_variants:
            stream_variants = [""]
        for sname, sid in ALLEN_SUBJECTS:
            if subject and sname != subject:
                continue
            contents, chapters = [], []
            working_context = None
            last_error = None
            for taxonomy_id in _taxonomy_candidates(info, course):
                for stream in stream_variants:
                    for batch_ids, selected_list in batch_variants:
                        try:
                            page = allen_get_page("/subject-details?" + _course_params(
                                batch_ids, selected_list, cid, stream, sid,
                                taxonomy_id=taxonomy_id), token)
                            trial_contents, trial_chapters = [], []
                            _walk_page(page, trial_contents, trial_chapters)
                            if trial_contents or trial_chapters:
                                contents, chapters = trial_contents, trial_chapters
                                working_context = (batch_ids, selected_list, taxonomy_id, stream)
                                break
                        except Exception as e:
                            last_error = e
                    if working_context:
                        break
                if working_context:
                    break
            if not working_context:
                logger.warning(f"subject {sname} returned no chapters/content: {last_error or 'empty page'}")
                continue
            _add(f"[{cname} | {sname}]", contents)
            batch_ids, selected_list, taxonomy_id, stream = working_context
            for ch in chapters:
                try:
                    tpage = allen_get_page("/topic-details?" + _course_params(
                        batch_ids, selected_list, cid, stream,
                        ch.get("subject_id") or sid, ch["topic_id"], taxonomy_id), token)
                except Exception as e:
                    logger.warning(f"topic {ch.get('topic_name')} skipped: {e}")
                    continue
                tcontents, tch = [], []
                _walk_page(tpage, tcontents, tch)
                _add(f"[{sname} | {ch.get('topic_name')}]", tcontents)
    return items


def download_file(url, output_path):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return output_path


def download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    work_dir = work_dir or DOWNLOAD_DIR
    os.makedirs(work_dir, exist_ok=True)
    cmd = [
        "N_m3u8DL-RE",
        m3u8_url,
        "--save-name", output_name,
        "--save-dir", work_dir,
        "--tmp-dir", work_dir,
        "--auto-select",
        "--thread-count", DL_THREADS,
        "--download-retry-count", "10",
        "--no-log"
    ]
    if bearer_token:
        cmd.extend(["--header", f"Authorization: Bearer {bearer_token}"])

    subprocess.run(cmd, check=True)
    output_path = os.path.join(work_dir, f"{output_name}.mp4")

    if not os.path.exists(output_path):
        for file in os.listdir(work_dir):
            if file.startswith(output_name):
                return os.path.join(work_dir, file)
    return output_path

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    return await asyncio.to_thread(download_m3u8, m3u8_url, output_name, bearer_token, work_dir)

async def async_upload_to_telegram(app_client, target_chat_id, file_path, caption):
    """Native async upload (tgcrypto) with FloodWait auto-retry."""
    from pyrogram.errors import FloodWait
    is_video = file_path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov"))
    for attempt in range(5):
        try:
            if is_video:
                return await app_client.send_video(chat_id=target_chat_id, video=file_path,
                                                   caption=caption, supports_streaming=True)
            return await app_client.send_document(chat_id=target_chat_id, document=file_path,
                                                  caption=caption)
        except FloodWait as fw:
            wait = int(getattr(fw, "value", 10) or 10)
            logger.warning(f"FloodWait {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait + 1)
    raise RuntimeError("Telegram FloodWait: upload baar baar ruk raha hai")

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
        await status_msg.edit_text("<blockquote><i>🔎 Login successful. Ab actual lectures verify ho rahe hain...</i></blockquote>")
        sample = await asyncio.to_thread(fetch_batch_contents, None, token, "Physics")
        if not sample:
            await status_msg.edit_text(
                "<blockquote><i>⚠️ <b>Login valid hai, lekin Allen ne lecture page empty bheja.</b>\n"
                "Session save ho gaya; dobara login karne ki zarurat nahi. "
                "Course mapping abhi match nahi hui.</i></blockquote>")
            return
        await status_msg.edit_text(
            f"<blockquote><i>🎉 <b>Login + Lecture Test Successful!</b>\n\n"
            f"✅ {len(sample)} Physics items mile. Session saved hai.\n"
            "Ab <code>/mybatches</code> ya <code>/batch &lt;ID&gt; physics</code> chalao.</i></blockquote>")
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
                   "refresh_token": "", "device_id": get_allen_device_id(),
                   "host": "api.allen-live.in"}
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

    status_msg = await message.reply_text("<blockquote><i>🔄 <b>Fetching your purchased batches...</b></i></blockquote>")
    try:
        token = await asyncio.to_thread(get_or_login_allen_token)
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
        lines.append("Poora batch: <code>/batch &lt;BATCH_ID&gt;</code>\nSubject-wise: <code>/batch &lt;BATCH_ID&gt; physics</code>\nSab ek saath: <code>/downloadall</code>\nHelp: <code>/subjects</code>")
        text = "\n".join(lines)
        if len(text) > MAX_TG_MSG_LEN:
            text = text[:MAX_TG_MSG_LEN] + "\n... (truncated)"
        await status_msg.edit_text(f"<blockquote>{text}</blockquote>")
    except Exception as e:
        logger.error(f"/mybatches failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Error:</b> <code>{str(e)}</code></i></blockquote>")

def _job_key(chat_id, batch_id, subject):
    return f"{chat_id}|{batch_id or 'ALL'}|{subject or 'ALL'}"


async def run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg):
    """Pipeline: parallel downloads feeding a single uploader -> max speed per job.
    Multiple jobs (different batches / channels) can run at the same time."""
    key = _job_key(target_chat_id, batch_id, subject)
    ACTIVE_JOBS[key] = {"running": True, "chat": message.chat.id}
    work_dir = os.path.join(DOWNLOAD_DIR, str(abs(hash(key)) % 10**8))
    os.makedirs(work_dir, exist_ok=True)
    done = load_done()
    ok = failed = 0

    try:
        items = await asyncio.to_thread(fetch_batch_contents, batch_id, token, subject)
        pending = [i for i in items if i["id"] not in done]
        if not items:
            await status_msg.edit_text(
                "<blockquote><i>❌ Allen ne is course/batch ke liye empty page bheja. "
                "Pehle <code>/login</code> dobara karein, phir <code>/mybatches</code> se ✅ enrolled "
                "Batch ID copy karke <code>/batch &lt;ID&gt; physics</code> chalayein.</i></blockquote>")
            return
        if not pending:
            await status_msg.edit_text("<blockquote><i>✅ Ye sab pehle hi upload ho chuka hai.</i></blockquote>")
            return

        total = len(pending)
        await status_msg.edit_text(
            f"<blockquote><i>🚀 <b>{total} new items</b> (total {len(items)}) — turbo mode ON "
            f"({MAX_PARALLEL_DOWNLOADS} parallel downloads, {DL_THREADS} threads each)</i></blockquote>")

        queue = asyncio.Queue(maxsize=MAX_PARALLEL_DOWNLOADS + 1)
        index_iter = iter(list(enumerate(pending, start=1)))
        lock = asyncio.Lock()

        def running():
            return ACTIVE_JOBS.get(key, {}).get("running", False)

        async def downloader():
            nonlocal failed
            while running():
                async with lock:
                    nxt = next(index_iter, None)
                if nxt is None:
                    return
                idx, item = nxt
                title = item.get("title") or f"Lecture_{idx}"
                clean = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip()[:70] or f"item_{idx}"
                clean = f"{idx}_{clean}"
                url = item.get("url")
                if not url:
                    continue
                try:
                    if ".m3u8" in url:
                        path = await async_download_m3u8(url, clean, token, work_dir)
                    else:
                        ext = os.path.splitext(url.split("?")[0])[1] or ".pdf"
                        path = await asyncio.to_thread(download_file, url, os.path.join(work_dir, clean + ext))
                    await queue.put((idx, item, title, path))
                except Exception as e:
                    failed += 1
                    logger.error(f"Download failed ({title}): {e}")
                    await message.reply_text(
                        f"<blockquote><i>⚠️ Skipped: <b>{title}</b>\n<code>{str(e)[:150]}</code></i></blockquote>")

        async def uploader():
            nonlocal ok, failed
            finished = 0
            while finished < total and running():
                try:
                    idx, item, title, path = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    if all(d.done() for d in downloaders) and queue.empty():
                        return
                    continue
                try:
                    caption = f"<blockquote><i><b>{title}</b>\n\nAllen Auto-Downloader ({idx}/{total})</i></blockquote>"
                    await async_upload_to_telegram(app, target_chat_id, path, caption)
                    mark_done(item["id"], done)
                    ok += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Upload failed ({title}): {e}")
                    await message.reply_text(
                        f"<blockquote><i>⚠️ Upload fail: <b>{title}</b>\n<code>{str(e)[:150]}</code></i></blockquote>")
                finally:
                    finished += 1
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                    gc.collect()

        downloaders = [asyncio.create_task(downloader()) for _ in range(MAX_PARALLEL_DOWNLOADS)]
        up = asyncio.create_task(uploader())
        await asyncio.gather(*downloaders, return_exceptions=True)
        await up

        await message.reply_text(
            f"<blockquote><i>✅ <b>Done!</b> Uploaded: {ok} | Failed: {failed}\n"
            f"Scope: {batch_id or 'ALL batches'} | {subject or 'All subjects'}</i></blockquote>")
    except Exception as e:
        logger.error(f"Batch job failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Batch Error:</b> <code>{str(e)}</code></i></blockquote>")
    finally:
        ACTIVE_JOBS.pop(key, None)
        shutil.rmtree(work_dir, ignore_errors=True)


@app.on_message(filters.command(["batch", "downloadall"]) & (filters.group | filters.channel | filters.private))
async def handle_batch(client: Client, message: Message):
    logger.info(f"/batch triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return

    try:
        token = await asyncio.to_thread(get_or_login_allen_token)
    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <b>Session Error:</b> <code>{str(e)}</code></i></blockquote>")
        return

    # /batch <BATCH_ID> [subject] [-c <channel_id>]     (BATCH_ID optional = all batches)
    parts = (message.text or "").split()[1:]
    target_chat_id = message.chat.id
    if "-c" in parts:
        i = parts.index("-c")
        try:
            target_chat_id = int(parts[i + 1])
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
            return
        parts = parts[:i] + parts[i + 2:]

    batch_id, subject_raw = None, None
    for token_arg in parts:
        low = token_arg.lower()
        if low in SUBJECT_ALIASES or low in ("all", "*", "sab"):
            if subject_raw is None and not (low in ("all", "*") and batch_id is None):
                subject_raw = low
            elif batch_id is None:
                batch_id = None
        elif batch_id is None:
            batch_id = token_arg

    try:
        subject = normalize_subject(subject_raw)
    except ValueError as e:
        await message.reply_text(f"<blockquote><i>⚠️ {e}</i></blockquote>")
        return

    key = _job_key(target_chat_id, batch_id, subject)
    if ACTIVE_JOBS.get(key, {}).get("running"):
        await message.reply_text("<blockquote><i>⚠️ Ye job already chal raha hai.</i></blockquote>")
        return

    scope = f"{batch_id or 'ALL batches'} | {subject or 'All subjects'}"
    dest = "yahin" if target_chat_id == message.chat.id else f"<code>{target_chat_id}</code>"
    status_msg = await message.reply_text(
        f"<blockquote><i>🔄 <b>Fetching:</b> {scope}\nUpload → {dest}</i></blockquote>")

    # fire-and-forget so multiple batches/channels download simultaneously
    asyncio.create_task(run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg))


@app.on_message(filters.command("subjects") & (filters.group | filters.channel | filters.private))
async def handle_subjects(client: Client, message: Message):
    await message.reply_text(
        "<blockquote><i>📚 <b>Subject-wise download</b>\n\n"
        "<code>/batch &lt;BATCH_ID&gt; physics</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; chemistry</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; maths</code>\n"
        "<code>/batch &lt;BATCH_ID&gt; all</code>  (poora batch)\n"
        "<code>/downloadall</code>  (saare batches, saare subjects)\n"
        "<code>/downloadall physics</code>\n\n"
        "Kisi channel me bhejna ho:\n"
        "<code>/batch &lt;BATCH_ID&gt; physics -c -100xxxxxxxxxx</code>\n"
        "(Bot ko us channel me admin banao. Alag-alag batches alag channels me ek saath chal sakte hain.)\n\n"
        "<code>/jobs</code> — chal rahe kaam dekho | <code>/stop</code> — sab rok do</i></blockquote>")


@app.on_message(filters.command("debug") & (filters.group | filters.channel | filters.private))
async def handle_debug(client: Client, message: Message):
    """Dump raw Allen responses into a file so the content format can be fixed."""
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    token = get_allen_token()
    if not token:
        await message.reply_text("<blockquote><i>⚠️ Pehle /login ya /token karo.</i></blockquote>")
        return
    parts = (message.text or "").split()[1:]
    want_batch = parts[0] if parts else None
    status = await message.reply_text("<blockquote><i>🔍 Debug data collect ho raha hai...</i></blockquote>")

    def _collect():
        out = {"student_info": None, "pages": []}
        r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo", headers=allen_headers(token), timeout=25)
        try:
            info = r.json()
        except Exception:
            info = {"raw": r.text[:2000]}
        out["student_info"] = {"http": r.status_code, "body": info}
        data = (info or {}).get("data") or {}
        stream = (data.get("student_detail") or {}).get("stream") or ""
        courses = data.get("course_details") or []
        if want_batch:
            courses = [c for c in courses if want_batch in (c.get("enrolled_batches") or []) + (c.get("unenrolled_batches") or [])] or courses
        for c in courses[:1]:
            selected_batches = list(c.get("enrolled_batches") or []) + list(c.get("unenrolled_batches") or [])
            batch_ids = [want_batch] if want_batch else selected_batches
            for sname, sid in ALLEN_SUBJECTS:
                page_url = "/subject-details?" + _course_params(
                    batch_ids, selected_batches, c.get("course_id") or "", stream, sid)
                rr = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url}, headers=allen_content_headers(token), timeout=30)
                try:
                    body = rr.json()
                except Exception:
                    body = {"raw": rr.text[:3000]}
                out["pages"].append({"subject": sname, "page_url": page_url, "http": rr.status_code, "body": body})
        return out

    try:
        out = await asyncio.to_thread(_collect)
        txt = json.dumps(out, indent=1, ensure_ascii=False).replace(token, "<TOKEN>")
        # strip personal info
        for f in ("email", "phone", "dob"):
            txt = __import__("re").sub(rf'"{f}": "[^"]*"', f'"{f}": "***"', txt)
        path = os.path.join(DOWNLOAD_DIR, "allen_debug.json")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(txt)
        summary = "\n".join(f"{pg['subject']}: HTTP {pg['http']} | {str((pg['body'] or {}).get('reason',''))[:60]}" for pg in out["pages"]) or "No course found"
        await message.reply_document(path, caption=f"<blockquote><i>🧪 Debug file\n{summary}\n\nYe file Lovable chat me bhejo.</i></blockquote>")
        await status.delete()
        os.remove(path)
    except Exception as e:
        await status.edit_text(f"<blockquote><i>❌ Debug error: <code>{str(e)[:300]}</code></i></blockquote>")


@app.on_message(filters.command("jobs"))
async def handle_jobs(client: Client, message: Message):
    mine = [k for k, v in ACTIVE_JOBS.items() if v.get("running")]
    if not mine:
        await message.reply_text("<blockquote><i>💤 Koi job nahi chal raha.</i></blockquote>")
        return
    await message.reply_text("<blockquote><i>⚙️ <b>Running jobs:</b>\n" +
                             "\n".join(f"• <code>{k}</code>" for k in mine) + "</i></blockquote>")


@app.on_message(filters.command("stop"))
async def handle_stop(client: Client, message: Message):
    stopped = 0
    for k, v in list(ACTIVE_JOBS.items()):
        if v.get("running") and (v.get("chat") == message.chat.id or message.from_user and message.from_user.id == OWNER_ID):
            v["running"] = False
            stopped += 1
    if stopped:
        await message.reply_text(f"<blockquote><i>🛑 {stopped} job(s) rok diye.</i></blockquote>")
    else:
        await message.reply_text("<blockquote><i>⚠️ Koi active task nahi hai.</i></blockquote>")


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
    if ALLEN_USERNAME and ALLEN_PASSWORD:
        try:
            token = get_or_login_allen_token()
            sample = fetch_batch_contents(None, token, "Physics")
            logger.info("Allen startup check passed: %s Physics items found", len(sample))
        except Exception as e:
            logger.error("Allen startup check failed: %s", e)
    logger.info("Workspace clean. Booting Pyrogram engine...")
    app.run()

if __name__ == "__main__":
    main()
