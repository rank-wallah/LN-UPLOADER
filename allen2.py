import glob
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
import re
import html
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
def _load_session_store():
    """Session store: {"default": {...}, "chats": {"<chat_id>": {...}}}.
    Old flat single-session files are migrated automatically."""
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and ("default" in data or "chats" in data):
                data.setdefault("default", {})
                data.setdefault("chats", {})
                return data
            if isinstance(data, dict) and data:
                return {"default": data, "chats": {}}
        except Exception as e:
            logger.error(f"Error reading session: {e}")
    try:  # redeploy ke baad DB se logins wapas
        raw = kv_get("allen_sessions")
        if raw:
            data = json.loads(raw)
            with open(SESSION_FILE, "w") as f:
                json.dump(data, f)
            data.setdefault("default", {})
            data.setdefault("chats", {})
            return data
    except Exception:
        pass
    return {"default": {}, "chats": {}}

def _save_session_store(store):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(store, f)
    except Exception as e:
        logger.error(f"Error saving session: {e}")
    try:
        kv_set("allen_sessions", json.dumps(store))
    except Exception:
        pass

def save_allen_session(data, chat_id=None):
    store = _load_session_store()
    if chat_id is None:
        store["default"] = data
    else:
        store["chats"][str(chat_id)] = data
    _save_session_store(store)

def get_allen_session(chat_id=None):
    """Session for one channel/chat, falling back to the default session."""
    store = _load_session_store()
    if chat_id is not None:
        sess = store["chats"].get(str(chat_id))
        if sess:
            return sess
    return store.get("default") or {}

def get_allen_token(chat_id=None):
    if chat_id is None and RUNTIME_ALLEN_TOKEN:
        return RUNTIME_ALLEN_TOKEN
    session = get_allen_session(chat_id)
    return session.get("access_token") or session.get("token") or (ALLEN_ACCESS_TOKEN if chat_id is None else "")

def get_allen_device_id(chat_id=None):
    """One stable device identity per login session (per channel)."""
    session = get_allen_session(chat_id)
    device_id = session.get("device_id") or os.getenv("ALLEN_DEVICE_ID", "").strip()
    if not device_id:
        # Stable across Heroku deploys without exposing the bot token itself.
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"allen-bot:{TG_BOT_TOKEN}:{chat_id}"))
        session["device_id"] = device_id
        if session:
            save_allen_session(session, chat_id)
    return device_id

def allen_headers(token, chat_id=None):
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": get_allen_device_id(chat_id),
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

def allen_content_headers(token, chat_id=None):
    """Match Allen web's content-request fingerprint exactly.

    X-Device-Id belongs to login/session requests. Sending it to pages/getPage
    can make that endpoint return HTTP 200 with an empty widgets list.
    """
    headers = allen_headers(token, chat_id).copy()
    headers.pop("X-Device-Id", None)
    # Allen's navigation renderer content-negotiates these headers. Removing
    # Accept/Accept-Language makes /library-web return a misleading 404 even
    # with a valid token, so preserve the browser fingerprint exactly.
    return headers

def fetch_student_info(token, chat_id=None):
    """Fetch student profile + enrolled courses/batches from Allen Digital."""
    r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                     headers=allen_content_headers(token, chat_id), timeout=25)
    if r.status_code == 401:
        _t = relogin_for_chat(chat_id)
        if _t:
            token = _t
            r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_content_headers(token, chat_id), timeout=25)
        if r.status_code == 401:
            raise ValueError("Allen session expire hai aur automatic login configure nahi hai.")
    data = r.json()
    if data.get("status") != 200 or not data.get("data"):
        raise ValueError(f"Allen error: {data.get('reason') or r.text[:150]}")
    return data["data"]

def get_or_login_allen_token(force_login=False, chat_id=None):
    """Restore a configured token, or sign in automatically after a restart/expiry."""
    token = None if force_login else get_allen_token(chat_id)
    if token:
        check = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo",
                             headers=allen_content_headers(token, chat_id), timeout=25)
        if check.status_code != 401:
            return token
    if chat_id is None and ALLEN_USERNAME and ALLEN_PASSWORD:
        return allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD)
    raise ValueError(
        "Is channel ka Allen session saved nahi hai. Pehle "
        "<code>/login username*password -c &lt;channel_id&gt;</code> karo, "
        "ya default login ke liye Heroku Config Vars me ALLEN_USERNAME/ALLEN_PASSWORD set karo.")

# Pyrogram bug: restart ke baad bot ka session channel bhool jata hai ("Peer id invalid").
# Channel ko access_hash=0 se fetch karke cache me daalo, phir dobara resolve karo.
from pyrogram import raw as _raw, utils as _putils
_orig_resolve_peer = Client.resolve_peer


async def _resolve_peer_fixed(self, peer_id):
    try:
        return await _orig_resolve_peer(self, peer_id)
    except Exception as e:
        if not isinstance(peer_id, int) or "peer id invalid" not in str(e).lower():
            raise
        if _putils.get_peer_type(peer_id) != "channel":
            raise
        r = await self.invoke(_raw.functions.channels.GetChannels(
            id=[_raw.types.InputChannel(channel_id=_putils.get_channel_id(peer_id), access_hash=0)]))
        await self.fetch_peers(getattr(r, "chats", []))
        return await _orig_resolve_peer(self, peer_id)


Client.resolve_peer = _resolve_peer_fixed

app = Client(
    "allen_downloader_bot",
    api_id=TG_API_ID,
    api_hash=TG_API_HASH,
    bot_token=TG_BOT_TOKEN,
    workers=32,
    max_concurrent_transmissions=16,
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


def allen_login_idpass(username, password, chat_id=None):
    """Authenticate via ALLEN Digital API (api.allen-live.in).

    Endpoint: POST /api/v1/auth/username
    Requires a DeviceID (uuid) in both header and payload.
    Success: {"status":200,"data":{"access_token":...,"refresh_token":...}}
    """
    username = str(username).strip()
    password = str(password).strip()
    if not username or not password:
        raise ValueError("Username and password cannot be empty")

    device_id = get_allen_device_id(chat_id)
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
        if chat_id is None:
            RUNTIME_ALLEN_TOKEN = token
        session = {"access_token": token, "token": token,
                   "refresh_token": refresh,
                   "device_id": device_id,
                   "username": username, "host": "api.allen-live.in",
                   "login_at": int(time.time())}
        save_allen_session(session, chat_id)
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
def _cpu_count():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        return max(1, os.cpu_count() or 1)

CPU_COUNT = _cpu_count()


def _mem_limit_gb():
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v.isdigit() and int(v) < (1 << 50):
                return int(v) / (1 << 30)
        except Exception:
            pass
    try:  # Heroku sets dyno RAM in MB
        return int(os.getenv("WEB_MEMORY") or os.getenv("MEMORY_AVAILABLE") or 0) / 1024 or 1.0
    except Exception:
        return 1.0


MEM_GB = _mem_limit_gb()
# Heroku host ke CPUs dikhata hai, dyno ke nahi -> RAM ke hisaab se limit karo (crash/restart se bachne ke liye)
CPU_COUNT = max(1, min(CPU_COUNT, int(MEM_GB * 2) or 1))
# Auto-scale with the Heroku dyno size: ~1 watermark job per 2 CPUs (min 3).
AUTO_PARALLEL = max(3, min(16, CPU_COUNT // 2))
DL_THREADS = os.getenv("DL_THREADS", "16")
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", str(AUTO_PARALLEL + 1)))
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", str(max(2, CPU_COUNT // AUTO_PARALLEL)))
# Saare channels ka total limit (pehle har channel alag 5-6 downloads chalata tha -> RAM full -> bot restart)
GLOBAL_DOWNLOADS = int(os.getenv("GLOBAL_DOWNLOADS", str(max(2, min(10, int(MEM_GB * 1.5))))))
_GLOBAL_DL = {"sem": None}


def _global_dl_sem():
    if _GLOBAL_DL["sem"] is None:
        _GLOBAL_DL["sem"] = asyncio.Semaphore(GLOBAL_DOWNLOADS)
    return _GLOBAL_DL["sem"]


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


def _load_done_all():
    if os.path.exists(DONE_FILE):
        try:
            with open(DONE_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): set(v) for k, v in data.items() if isinstance(v, list)}
        except Exception:
            pass
    return {}  # purana flat format: kis channel ka tha pata nahi, isliye ignore


def load_done(chat_id):
    """Sirf ISI channel ke uploaded IDs — dusre channel ka record mix nahi hota."""
    return set(_load_done_all().get(str(chat_id), set()))


def mark_done(content_id, done_set, chat_id):
    done_set.add(content_id)
    try:
        data = _load_done_all()
        data[str(chat_id)] = set(data.get(str(chat_id), set())) | done_set
        with open(DONE_FILE, "w") as f:
            json.dump({k: sorted(v) for k, v in data.items()}, f)
    except Exception as e:
        logger.warning(f"Could not persist progress: {e}")


# ---- Progress state: channel ke andar hi save (Heroku restart ke baad bhi resume) ----
STATE_MSG_FILE = "state_msgs.json"
STATE_TAG = "#allen_state"


def _load_state_msgs():
    if os.path.exists(STATE_MSG_FILE):
        try:
            with open(STATE_MSG_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items() if v}
        except Exception:
            pass
    return {}


def _save_state_msg_id(chat_id, msg_id):
    try:
        data = _load_state_msgs()
        data[str(chat_id)] = int(msg_id)
        with open(STATE_MSG_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"State msg id save failed: {e}")


async def load_state_from_channel(client, chat_id):
    """Channel me padi #allen_state file se pehle-uploaded IDs wapas lao."""
    msg = None
    msg_id = _load_state_msgs().get(str(chat_id))
    if msg_id:
        try:
            m = await client.get_messages(int(chat_id), msg_id)
            if m and m.document:
                msg = m
        except Exception:
            msg = None
    if msg is None:
        try:
            async for m in client.search_messages(int(chat_id), query=STATE_TAG, limit=5):
                if m.document:
                    msg = m
                    _save_state_msg_id(chat_id, m.id)
                    break
        except Exception as e:
            logger.warning(f"State search failed: {e}")
    if msg is None:
        return set()
    try:
        path = await client.download_media(msg, file_name="state_dl.json")
        with open(path, "r") as f:
            data = json.load(f)
        try:
            os.remove(path)
        except OSError:
            pass
        if isinstance(data, list):
            logger.info(f"Remote progress loaded: {len(data)} items already done")
            return set(data)
    except Exception as e:
        logger.warning(f"State load failed: {e}")
    return set()


def _title_key(title):
    """Caption/title ko normalize karo taaki channel scan se match ho sake."""
    t = str(title or "")
    m = re.match(r"^\[[^\]]+\]\s*", t)
    if m:
        t = t[m.end():]
    t = re.sub(r'[\\/:*?"<>|]+', " - ", t)
    t = os.path.splitext(t)[0]
    return re.sub(r"\s+", " ", t).strip().lower()


async def scan_channel_uploads(client, chat_id):
    """Channel ke saare video messages ke captions padh kar already-uploaded
    lecture titles ka set lao — state file na mile toh bhi resume ho."""
    titles = set()
    try:
        count = 0
        async for m in client.get_chat_history(int(chat_id)):
            if not (m.video or m.document):
                continue
            cap = m.caption or ""
            for line in cap.splitlines():
                line = line.strip()
                if line.lower().startswith("file title"):
                    _, _, val = line.partition(":")
                    key = _title_key(val)
                    if key:
                        titles.add(key)
            count += 1
        if count:
            logger.info(f"Channel scan: {count} media messages, {len(titles)} lecture titles mile")
    except Exception as e:
        logger.warning(f"Channel scan failed: {e}")
    return titles


async def save_state_to_channel(client, chat_id, done_set):
    """Uploaded IDs ki list channel me #allen_state file ke roop me save/update karo."""
    tmp = os.path.join(DOWNLOAD_DIR, "allen_state.json")
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(sorted(done_set), f)
        msg_id = _load_state_msgs().get(str(chat_id))
        edited = False
        if msg_id:
            try:
                from pyrogram.types import InputMediaDocument
                await client.edit_message_media(int(chat_id), int(msg_id),
                                                InputMediaDocument(tmp, caption=STATE_TAG))
                edited = True
            except Exception as e:
                logger.warning(f"State edit failed (naya bhejunga): {e}")
        if not edited:
            m = await client.send_document(int(chat_id), tmp, caption=STATE_TAG,
                                           disable_notification=True)
            _save_state_msg_id(chat_id, m.id)
    except Exception as e:
        logger.warning(f"State save failed: {e}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---- Uploaded-message tracking + auto-delete on new login ----
UPLOAD_LOG_FILE = "uploaded_messages.json"


def load_upload_log():
    if os.path.exists(UPLOAD_LOG_FILE):
        try:
            with open(UPLOAD_LOG_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): [int(m) for m in v if m] for k, v in data.items()
                        if isinstance(v, list)}
        except Exception:
            pass
    return {}


def save_upload_log(log):
    try:
        with open(UPLOAD_LOG_FILE, "w") as f:
            json.dump(log, f)
    except Exception as e:
        logger.warning(f"Could not persist upload log: {e}")


def log_uploaded_message(chat_id, message_id):
    if not message_id:
        return
    log = load_upload_log()
    bucket = log.setdefault(str(chat_id), [])
    if message_id not in bucket:
        bucket.append(message_id)
    save_upload_log(log)


async def purge_uploaded_messages(client):
    """DISABLED: purane lectures kabhi delete nahi karne.
    """
    return 0
    """Naya /login => pehle upload kiye saare channel messages delete (revoke=True)."""
    log = load_upload_log()
    total = 0
    for chat_id, ids in log.items():
        if not chat_id.lstrip("-").isdigit() or not ids:
            continue
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await client.delete_messages(int(chat_id), chunk, revoke=True)
                total += len(chunk)
            except Exception as e:
                logger.warning(f"Could not delete uploads in chat {chat_id}: {e}")
    for chat_id, msg_id in _load_state_msgs().items():
        try:
            await client.delete_messages(int(chat_id), int(msg_id), revoke=True)
        except Exception:
            pass
    for stale in (UPLOAD_LOG_FILE, DONE_FILE, STATE_MSG_FILE):
        try:
            os.remove(stale)
        except OSError:
            pass
    return total


def build_caption(item, file_path, duration=0):
    """Caption format:
    [🎥]Vid Id : <content_id>
    File Title : <lecture title + real extension>
    Batch Name : <course name>
    Topic Name : <Subject - Topic>
    Extracted By ➤ Courier Well"""
    raw = str(item.get("title") or "Lecture")
    m = re.match(r"^\[[^\]]+\]\s*", raw)
    file_title = raw[m.end():].strip() if m else raw.strip()
    file_title = re.sub(r'[\\/:*?"<>|]+', " - ", file_title).strip() or "Lecture"
    ext = os.path.splitext(file_path)[1] or ""
    file_title = f"{file_title}{ext}"

    vid = str(item.get("id") or "").strip()
    if vid.startswith("http"):
        vid = vid.split("/")[-1].split("?")[0] or vid

    batch = str(item.get("batch") or "Allen Batch").strip() or "Allen Batch"
    subject = str(item.get("subject") or "").strip()
    topic = str(item.get("topic") or "").strip()
    if topic and topic != subject:
        topic_line = f"{subject} - {topic}" if subject else topic
    else:
        topic_line = subject or topic or "Topic"

    lines = [f"File Title : {file_title}"]
    lines += [f"Batch Name : {batch}", f"Topic Name : {topic_line}"]
    if item.get("kind"):
        lines.append("Type : \x01" + item["kind"] + "\x02")
    if item.get("date"):
        lines.append(f"Date : {item['date']}")
    lines.append("Extracted By ➤ Courier Well")
    caption = html.escape("\n".join(lines))
    return caption.replace("\x01", "<b>").replace("\x02", "</b>")


def allen_get_page(page_url, token, chat_id=None):
    r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                      headers=allen_content_headers(token, chat_id), timeout=30)
    if r.status_code == 401:
        _t = relogin_for_chat(chat_id)
        if _t:
            token = _t
            r = requests.post(ALLEN_PAGE_URL, json={"page_url": page_url},
                              headers=allen_content_headers(token, chat_id), timeout=30)
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
            "Allen empty page: http=%s status=%s reason=%s page_url=%s data_keys=%s",
            r.status_code, data.get("status"), data.get("reason"), page_url,
            list(page_data.keys()) if isinstance(page_data, dict) else [])
    return page_data


def _content_section(action):
    """Turn Allen's card_type into a short, stable channel/index section name."""
    current = ((action.get("tracking_params") or {}).get("current") or {})
    card_type = str(current.get("card_type") or "").lower()
    if "live lecture" in card_type or "class notes" in card_type:
        return "Live Lectures & Notes"
    if "concept video" in card_type:
        return "Concept Videos"
    if "race" in card_type:
        return "RACE & Solutions"
    if "exercise" in card_type or "rpp" in card_type:
        return "Exercises & Solutions"
    if "study module" in card_type:
        return "Study Modules"
    return "Other Material"


_DATE_KEYS = ("start_time", "startTime", "start_date", "scheduled_at", "scheduled_time",
              "class_date", "date", "published_at", "created_at", "live_at")


def _fmt_date(v):
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            if n > 1e12:
                n /= 1000
            if n < 1e9:
                return ""
            import datetime as _dt
            return (_dt.datetime.utcfromtimestamp(n) + _dt.timedelta(hours=5, minutes=30)).strftime("%d-%m-%Y")
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = re.search(r"\b(\d{1,2})[-/ ](\d{1,2}|[A-Za-z]{3,9})[-/ ,]+(\d{2,4})\b", str(v))
        if m:
            return m.group(0).strip()
    except Exception:
        pass
    return ""


def _lecture_meta(node, action, d):
    """Lecture type (Live / Recorded) aur date nikalo."""
    current = ((action.get("tracking_params") or {}).get("current") or {})
    blob = " ".join(str(x) for x in (current.get("card_type"), current.get("content_type"),
                                     d.get("content_type"), d.get("type"), node.get("tag"),
                                     node.get("badge"), node.get("subtitle")) if x).lower()
    if "live" in blob and "concept" not in blob:
        kind = "Live Lecture"
    else:
        kind = "Recorded Lecture"
    date = ""
    for src in (d, node, current, action.get("data") or {}):
        for k in _DATE_KEYS:
            if isinstance(src, dict) and src.get(k):
                date = _fmt_date(src.get(k))
                if date:
                    break
        if date:
            break
    if not date:
        date = _fmt_date(node.get("subtitle") or "") or _fmt_date(node.get("description") or "")
    return kind, date


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
                    kind, ldate = _lecture_meta(node, action, d)
                    contents.append({"id": d.get("content_id") or uri, "title": name, "url": uri,
                                      "section": _content_section(action),
                                      "kind": kind, "date": ldate})
        action = node.get("action")
        if isinstance(action, dict):
            action_data = action.get("data") or {}
            q = action_data.get("query") or {}
            cur = (action.get("tracking_params") or {}).get("current") or {}
            if q.get("topic_id"):
                # Allen embeds the authoritative topic request in every chapter
                # card.  Its batch_id can differ from the request that opened the
                # subject page, so preserve and replay it instead of guessing.
                query = {str(k): str(v) for k, v in q.items() if v is not None}
                topic_uri = action_data.get("uri") or "/topic-details"
                chapters.append({"topic_id": str(q["topic_id"]),
                                 "topic_name": cur.get("topic_name") or "Topic",
                                 "subject_id": cur.get("subject_id") or q.get("subject_id"),
                                 "page_url": str(topic_uri) + "?" + urlencode(query)})
        for value in node.values():
            _walk_page(value, contents, chapters)
    elif isinstance(node, list):
        for value in node:
            _walk_page(value, contents, chapters)


def _allen_internal_urls(node, wanted_uri=None, with_labels=False):
    """Collect exact internal page URLs emitted by Allen's page renderer."""
    found = []
    if isinstance(node, dict):
        for key in ("action", "content_action", "card_action"):
            action = node.get(key)
            if not isinstance(action, dict):
                continue
            data = action.get("data") or {}
            uri = data.get("uri")
            query = data.get("query")
            if isinstance(uri, str) and uri.startswith("/"):
                if wanted_uri is None or uri.split("?", 1)[0] == wanted_uri:
                    if isinstance(query, dict) and query:
                        clean = {str(k): str(v) for k, v in query.items() if v is not None}
                        page_url = uri.split("?", 1)[0] + "?" + urlencode(clean)
                    else:
                        page_url = uri
                    if with_labels:
                        tracking = action.get("tracking_params") or {}
                        current = tracking.get("current") or {}
                        label = (current.get("subject_name") or data.get("title") or
                                 node.get("title") or node.get("name") or "")
                        found.append({"url": page_url, "label": str(label)})
                    else:
                        found.append(page_url)
        for value in node.values():
            found.extend(_allen_internal_urls(value, wanted_uri, with_labels))
    elif isinstance(node, list):
        for value in node:
            found.extend(_allen_internal_urls(value, wanted_uri, with_labels))
    if with_labels:
        unique = {}
        for item in found:
            unique[item["url"]] = item
        return list(unique.values())
    return list(dict.fromkeys(found))


def _discover_subject_urls(token, chat_id=None):
    """Ask Allen's own library pages for authoritative course/subject URLs."""
    discovered, queue, visited = [], ["/library-web", "/explore"], set()
    while queue and len(visited) < 8:
        page_url = queue.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            page = allen_get_page(page_url, token, chat_id)
        except Exception as exc:
            logger.warning("Allen navigation discovery skipped %s: %s", page_url, exc)
            continue
        discovered.extend(_allen_internal_urls(page, "/subject-details", with_labels=True))
        for next_url in _allen_internal_urls(page):
            path = next_url.split("?", 1)[0]
            if path in ("/library-web", "/library", "/explore") and next_url not in visited:
                queue.append(next_url)
    unique = {}
    for item in discovered:
        unique[item["url"]] = item
    return list(unique.values())


def _query_value(page_url, key):
    from urllib.parse import parse_qs, urlsplit
    values = parse_qs(urlsplit(page_url).query).get(key) or []
    return values[0] if values else ""


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
    # Try the known working web taxonomy before any optional account metadata.
    found = [ALLEN_TAXONOMY, "1739171216OJ"]

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
    return list(dict.fromkeys(v for v in found if v))


def fetch_batch_contents(batch_id=None, token=None, subject=None, chat_id=None):
    """All downloadable items of one batch, or of ALL purchased batches when batch_id is None/'all'."""
    if not token:
        raise ValueError("No token. /login ya /token pehle karo.")
    info = fetch_student_info(token, chat_id)
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
    # The renderer can return account-specific batch subsets that are not present
    # as a simple enrolled/unenrolled combination. Prefer those server-generated
    # URLs and keep constructed URLs only as compatibility fallbacks.
    discovered_subject_urls = _discover_subject_urls(token, chat_id)
    logger.info("Allen navigation discovery found %s subject URL(s)", len(discovered_subject_urls))

    def _add(prefix, found, batch_name, subject, topic):
        for it in found:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append({"id": it["id"], "title": f"{prefix} {it['title']}".strip(), "url": it["url"],
                          "batch": batch_name, "subject": subject, "topic": topic,
                          "section": it.get("section") or "Other Material",
                          "kind": it.get("kind") or "", "date": it.get("date") or ""})

    for course in courses:
        enrolled_batches = list(dict.fromkeys(str(v) for v in (course.get("enrolled_batches") or []) if v))
        unenrolled_batches = list(dict.fromkeys(str(v) for v in (course.get("unenrolled_batches") or []) if v))
        selected_batches = list(dict.fromkeys(enrolled_batches + unenrolled_batches))
        if not selected_batches:
            continue
        # Match Allen's working web downloader first: both fields receive the
        # complete course batch list and the stream comes from student_detail.
        # Narrower variants are fallbacks for accounts whose page configuration
        # is scoped to an enrolled or explicitly selected batch.
        batch_variants = [(selected_batches, selected_batches)]
        if enrolled_batches:
            batch_variants.append((enrolled_batches, selected_batches))
        if not wants_all:
            chosen = [str(batch_id)]
            batch_variants.append((chosen, selected_batches))
            batch_variants.append((chosen, chosen))
        # Keep order while removing duplicate request variants.
        batch_variants = list(dict.fromkeys(
            (tuple(batch_ids), tuple(selected_list))
            for batch_ids, selected_list in batch_variants
        ))
        cname = course.get("course_name") or "Course"
        cid = course.get("course_id") or ""
        # Current studentInfo responses can expose the API enum on the student
        # and a display value on the course. Try both rather than assuming one.
        stream_variants = list(dict.fromkeys(str(v) for v in (
            student_stream, course.get("stream_enum_name"),
            course.get("stream"), course.get("stream_name")
        ) if v))
        if not stream_variants:
            stream_variants = [""]
        for sname, sid in ALLEN_SUBJECTS:
            if subject and sname != subject:
                continue
            contents, chapters = [], []
            working_context = None
            last_error = None
            # Subject IDs and taxonomy IDs are account/session specific. Match
            # the server-provided subject label, then replay its exact URL.
            wanted_names = {sname.lower()}
            if sname == "Mathematics":
                wanted_names.add("maths")
            exact_urls = [item["url"] for item in discovered_subject_urls
                          if _query_value(item["url"], "selected_course_id") == str(cid)
                          and item.get("label", "").strip().lower() in wanted_names]
            if not wants_all:
                exact_urls = [url for url in exact_urls if str(batch_id) in
                              (_query_value(url, "batch_id") + "," +
                               _query_value(url, "selected_batch_list")).split(",")]
            for page_url in exact_urls:
                try:
                    page = allen_get_page(page_url, token, chat_id)
                    trial_contents, trial_chapters = [], []
                    _walk_page(page, trial_contents, trial_chapters)
                    if trial_contents or trial_chapters:
                        contents, chapters = trial_contents, trial_chapters
                        working_context = ([], [], "", "")
                        break
                except Exception as e:
                    last_error = e
            if not working_context:
                for taxonomy_id in _taxonomy_candidates(info, course):
                    for stream in stream_variants:
                        for batch_ids_tuple, selected_list_tuple in batch_variants:
                            batch_ids, selected_list = list(batch_ids_tuple), list(selected_list_tuple)
                            try:
                                page = allen_get_page("/subject-details?" + _course_params(
                                    batch_ids, selected_list, cid, stream, sid,
                                    taxonomy_id=taxonomy_id), token, chat_id)
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
            _add(f"[{cname} | {sname}]", contents, cname, sname, sname)
            batch_ids, selected_list, taxonomy_id, stream = working_context
            seen_chapters = set()
            for ch in chapters:
                chapter_key = ch.get("page_url") or ch.get("topic_id")
                if not chapter_key or chapter_key in seen_chapters:
                    continue
                seen_chapters.add(chapter_key)
                # Prefer the exact URL returned by Allen in the subject page.
                # Reconstruct only for older response shapes that omit it.
                topic_page_url = ch.get("page_url") or ("/topic-details?" + _course_params(
                    batch_ids, selected_list, cid, stream,
                    ch.get("subject_id") or sid, ch["topic_id"], taxonomy_id))
                try:
                    tpage = allen_get_page(topic_page_url, token, chat_id)
                except Exception as e:
                    logger.warning(f"topic {ch.get('topic_name')} skipped: {e}")
                    continue
                tcontents, tch = [], []
                _walk_page(tpage, tcontents, tch)
                _add(f"[{sname} | {ch.get('topic_name')}]", tcontents, cname, sname, ch.get("topic_name"))
    return items


def download_file(url, output_path):
    hdr = {"Referer": "https://voraclasses.classx.co.in/"} if "appx" in url or "classx" in url else {}
    with requests.get(url, stream=True, timeout=120, headers=hdr) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    if os.path.getsize(output_path) == 0:
        os.remove(output_path)
        raise RuntimeError("Downloaded file is empty (0 B)")
    return output_path


def _find_output(work_dir, output_name):
    best = None
    for file in os.listdir(work_dir):
        if file.startswith(output_name) and file.lower().endswith((".mp4", ".mkv", ".ts", ".m4a")):
            p = os.path.join(work_dir, file)
            if os.path.isfile(p) and os.path.getsize(p) > 0 and (not best or os.path.getsize(p) > os.path.getsize(best)):
                best = p
    return best


RUNNING_PROCS = {}  # work_dir -> set(Popen) so /stop can kill running downloads/encodes
STOPPED_DIRS = set()


def _run(cmd, timeout, work_dir):
    """subprocess.run replacement that /stop can kill instantly."""
    key = os.path.abspath(work_dir)
    if key in STOPPED_DIRS:
        raise RuntimeError("Stopped by user")
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"
    env["DOTNET_SYSTEM_CONSOLE_ALLOW_ANSI_COLOR_REDIRECTION"] = "0"
    env["NO_COLOR"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, env=env)
    RUNNING_PROCS.setdefault(key, set()).add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        RUNNING_PROCS.get(key, set()).discard(proc)
    if key in STOPPED_DIRS:
        raise RuntimeError("Stopped by user")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill_job_processes(work_dir):
    key = os.path.abspath(work_dir)
    STOPPED_DIRS.add(key)
    for proc in list(RUNNING_PROCS.get(key, set())):
        try:
            proc.kill()
        except Exception:
            pass


def _fmt_duration(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s_ = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s_:02d}"


def probe_video(path):
    """Return (duration_seconds, width, height) via ffprobe."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        dur = float((d.get("format") or {}).get("duration") or 0)
        return int(dur), int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0, 0, 0


def download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    """Allen CDN URLs are already signed (hdnts). Sending the API Bearer token
    to the CDN makes it reject the request, so it is NOT sent. Falls back to
    ffmpeg if N_m3u8DL-RE fails."""
    work_dir = work_dir or DOWNLOAD_DIR
    os.makedirs(work_dir, exist_ok=True)
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36"
    cmd = [
        "N_m3u8DL-RE", m3u8_url,
        "--save-name", output_name,
        "--save-dir", work_dir,
        "--tmp-dir", work_dir,
        "-sv", "best", "-sa", "best",
        "-M", "format=mp4",
        "--thread-count", str(DL_THREADS),
        "--download-retry-count", "10",
        "--del-after-done",
        "--no-log",
        "-H", f"User-Agent: {ua}",
        "-H", "Origin: https://allen.in",
        "-H", "Referer: https://allen.in/",
    ]
    err = ""
    for attempt in range(2):
        try:
            r = _run(cmd, 3600, work_dir)
            out = _find_output(work_dir, output_name)
            if r.returncode == 0 and out:
                return out
            err = (r.stderr or r.stdout or "")[-300:]
            logger.warning(f"N_m3u8DL-RE failed ({r.returncode}) try {attempt+1}: {err}")
        except Exception as e:
            if "Stopped by user" in str(e):
                raise
            err = str(e)
            logger.warning(f"N_m3u8DL-RE error: {e}")

    # Fallback: ffmpeg direct copy (with reconnect), 2 tries
    out_path = os.path.join(work_dir, f"{output_name}.mp4")
    fcmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10",
        "-user_agent", ua,
        "-headers", "Origin: https://allen.in\r\nReferer: https://allen.in/\r\n",
        "-i", m3u8_url,
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        out_path,
    ]
    ferr = ""
    for attempt in range(2):
        r = _run(fcmd, 3600, work_dir)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        ferr = (r.stderr or r.stdout or "").strip()
    if "403" in ferr or "Forbidden" in ferr:
        raise RuntimeError("Download failed: lecture link expired (403). Batch dobara chalao, naya link milega.")
    raise RuntimeError(f"Download failed. RE: {err[-120:]} | ffmpeg: {ferr[-200:]}")

async def async_download_m3u8(m3u8_url, output_name, bearer_token=None, work_dir=None):
    async with _global_dl_sem():
        r = await _async_download_m3u8_inner(m3u8_url, output_name, bearer_token, work_dir)
    gc.collect()
    return r


async def _async_download_m3u8_inner(m3u8_url, output_name, bearer_token=None, work_dir=None):
    return await asyncio.to_thread(download_m3u8, m3u8_url, output_name, bearer_token, work_dir)

def prepare_video_for_upload(source_path):
    """Burn the channel watermark into a Telegram-safe H.264 video."""
    stem, _ = os.path.splitext(source_path)
    video_path = stem + ".watermarked.mp4"
    thumb_path = stem + ".cover.jpg"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    # Sanitize odd dimensions, sample aspect ratio and pixel format. Limiting x264
    # threads prevents parallel uploads from exhausting a small Heroku worker.
    label = ("scale=-2:'min(720\\,trunc(ih/2)*2)',setsar=1,format=yuv420p,"
             "drawtext=fontfile=" + font + ":text=courierWell:"
             "fontcolor=white@0.80:fontsize=max(18\\,h/36):"
             "borderw=2:bordercolor=black@0.60:x=w-tw-24:y=24")

    def watermark_cmd(video_filter, preset="ultrafast"):
        return ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                "-filter_threads", "1", "-i", source_path,
                "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                "-vf", video_filter, "-fps_mode", "vfr",
                "-c:v", "libx264", "-preset", preset, "-crf", "22",
                "-pix_fmt", "yuv420p", "-threads", FFMPEG_THREADS,
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-max_muxing_queue_size", "4096", "-movflags", "+faststart",
                video_path]

    if os.getenv("WATERMARK_MODE", "burn" if CPU_COUNT >= 6 else "fast").lower() != "burn":
        # FAST mode: no re-encode (stream copy) - watermark only on thumbnail.
        try:
            result = _run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", source_path,
                           "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                           "-movflags", "+faststart", video_path], 1800, os.path.dirname(source_path))
            if result.returncode != 0 or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
                raise RuntimeError((result.stderr or "copy failed")[-300:])
            for seek in ("5", "0"):
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", seek, "-i", video_path,
                                    "-frames:v", "1", "-vf",
                                    "scale=640:-2,drawtext=text=courierWell:fontcolor=white@0.85:fontsize=28:"
                                    "borderw=2:bordercolor=black@0.6:x=w-tw-16:y=16",
                                    "-q:v", "3", thumb_path], capture_output=True, text=True, timeout=90)
                if r.returncode == 0 and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
                    return video_path, thumb_path
            return video_path, None
        except Exception as e:
            logger.warning(f"Fast mode failed, burning watermark instead: {e}")
            if os.path.exists(video_path):
                os.remove(video_path)

    try:
        result = _run(watermark_cmd(label), 7200, os.path.dirname(source_path))
        if result.returncode != 0 or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
            if os.path.exists(video_path):
                os.remove(video_path)
            # Broken/very large source metadata fallback: normalize to at most 720p.
            fallback = ("scale='min(1280\\,ceil(iw/2)*2)':'min(720\\,ceil(ih/2)*2)':"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        "setsar=1,format=yuv420p,drawtext=fontfile=" + font +
                        ":text=courierWell:fontcolor=white@0.80:fontsize=max(18\\,h/36):"
                        "borderw=2:bordercolor=black@0.60:x=w-tw-24:y=24")
            result = _run(watermark_cmd(fallback), 7200, os.path.dirname(source_path))
        if result.returncode != 0 or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
            error = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
            raise RuntimeError(f"Video watermark failed: {error[-600:]}")
        # Extract a frame from the watermarked video: cover and video match.
        for seek in ("2", "0"):
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", seek,
                 "-i", video_path, "-frames:v", "1", "-vf", "scale=640:-2",
                 "-q:v", "3", thumb_path], capture_output=True, text=True, timeout=90)
            if result.returncode == 0 and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
                return video_path, thumb_path
        raise RuntimeError(f"Video thumbnail failed: {result.stderr[-300:]}")
    except Exception:
        for path in (video_path, thumb_path):
            if os.path.exists(path):
                os.remove(path)
        raise


TG_UPLOAD_LIMIT = 1950 * 1024 * 1024  # stay safely under Telegram's 2000 MB cap


def split_video(source_path, duration=0):
    """2GB+ video: quality same rakho (no re-encode), bas time ke hisaab se parts me kaato."""
    size = os.path.getsize(source_path)
    if size <= TG_UPLOAD_LIMIT:
        return [source_path]
    if not duration:
        duration = probe_video(source_path)[0] or 0
    if not duration:
        raise RuntimeError("Video 2GB se badi hai aur duration nahi mili, split nahi ho payi.")
    n = int(size // int(TG_UPLOAD_LIMIT * 0.9)) + 1
    for _ in range(4):
        seg = max(60, int(duration / n) + 1)
        stem, _ = os.path.splitext(source_path)
        pattern = stem + ".part%02d.mp4"
        for old in glob.glob(stem + ".part*.mp4"):
            os.remove(old)
        r = _run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", source_path,
                  "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
                  "-segment_time", str(seg), "-reset_timestamps", "1",
                  "-segment_format_options", "movflags=+faststart", pattern],
                 3600, os.path.dirname(source_path))
        parts = sorted(glob.glob(stem + ".part*.mp4"))
        if r.returncode == 0 and parts and all(os.path.getsize(p) <= TG_UPLOAD_LIMIT for p in parts):
            return parts
        n += 1
    raise RuntimeError("Video split fail ho gayi.")

def reduce_video_size(source_path, duration=0):
    """Re-encode only when the file is over the Telegram limit; sized to fit."""
    if not os.path.isfile(source_path) or os.path.getsize(source_path) <= TG_UPLOAD_LIMIT:
        return source_path
    if not duration:
        duration, _, _ = probe_video(source_path)
    if not duration or duration <= 0:
        duration = 3600
    # Target total bitrate so the result lands under the limit (90% safety margin).
    target_bits = TG_UPLOAD_LIMIT * 0.90 * 8
    total_kbps = int(target_bits / duration / 1000)
    audio_kbps = 96
    video_kbps = max(total_kbps - audio_kbps, 300)
    stem, _ = os.path.splitext(source_path)
    out_path = stem + ".reduced.mp4"
    for vf in ("scale='min(1280,iw)':-2", "scale=trunc(iw/2)*2:trunc(ih/2)*2"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", source_path,
               "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
               "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
               "-movflags", "+faststart", out_path]
        result = _run(cmd, 7200, os.path.dirname(source_path))
        if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            if os.path.getsize(out_path) <= TG_UPLOAD_LIMIT:
                return out_path
    if os.path.exists(out_path):
        os.remove(out_path)
    raise RuntimeError("Video 2000MB se badi hai aur compress karke bhi fit nahi hui.")


async def async_upload_to_telegram(app_client, target_chat_id, file_path, caption, thumb_path=None, duration=0, width=0, height=0):
    """Native async upload (tgcrypto) with FloodWait auto-retry."""
    from pyrogram.errors import FloodWait
    is_video = file_path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov"))
    for attempt in range(5):
        try:
            if is_video:
                msg = await app_client.send_video(chat_id=target_chat_id, video=file_path,
                                                  caption=caption, supports_streaming=True, thumb=thumb_path,
                                                  duration=duration or 0, width=width or 0, height=height or 0)
            else:
                msg = await app_client.send_document(chat_id=target_chat_id, document=file_path,
                                                     caption=caption)
            log_uploaded_message(target_chat_id, getattr(msg, "id", None))
            return msg
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


def _schedule_delete(chat_id, message_id, delay=20):
    """Delete a message after `delay` seconds (best-effort, never raises)."""
    async def _del():
        try:
            await asyncio.sleep(delay)
            await app.delete_messages(chat_id, message_id)
        except Exception as e:
            logger.warning(f"Auto-delete msg {message_id} failed: {e}")
    try:
        asyncio.get_running_loop().create_task(_del())
    except RuntimeError:
        pass

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
        await message.reply_text("<blockquote><i>⚠️ Format incorrect!\nUsage: <code>/login username*password</code>\nKisi channel ke liye: <code>/login username*password -c &lt;channel_id&gt;</code></i></blockquote>")
        return

    creds = args[1].strip()
    session_chat_id = message.chat.id
    if " -c " in creds:
        creds, _, chan = creds.rpartition(" -c ")
        try:
            session_chat_id = int(chan.strip())
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
            return
    username, password = (part.strip() for part in creds.split("*", 1))
    if not username or not password:
        await message.reply_text("<blockquote><i>⚠️ Username and password cannot be empty.</i></blockquote>")
        return
    try:
        await message.delete()  # credentials wali message turant hatao
    except Exception as e:
        logger.warning(f"/login command delete failed: {e}")
    if (message.chat.type == enums.ChatType.PRIVATE and " -c " not in args[1]
            and await get_helper() is not None):
        await auto_channel_flow(message, username, password)
        return
    status_msg = await message.reply_text("<blockquote><i>🔑 Authenticating directly with Allen Servers...</i></blockquote>")

    try:
        token = await asyncio.to_thread(allen_login_idpass, username, password, session_chat_id)
        # Purane uploads kabhi delete nahi hote (kisi bhi channel me).
        purged_note = ""
        await status_msg.edit_text(f"<blockquote><i>🔎 Login successful. Ab actual lectures verify ho rahe hain...{purged_note}</i></blockquote>")
        sample = await asyncio.to_thread(fetch_batch_contents, None, token, "Physics", session_chat_id)
        if not sample:
            await status_msg.edit_text(
                "<blockquote><i>⚠️ <b>Login valid hai, lekin Allen ne lecture page empty bheja.</b>\n"
                "Session save ho gaya; dobara login karne ki zarurat nahi. "
                "Course mapping abhi match nahi hui.</i></blockquote>")
            _schedule_delete(status_msg.chat.id, status_msg.id)
            return
        await status_msg.edit_text(
            f"<blockquote><i>🎉 <b>Login + Lecture Test Successful!</b>\n\n"
            f"✅ {len(sample)} Physics items mile. Session saved hai ({'channel ' + str(session_chat_id) if session_chat_id != message.chat.id else 'default'}).\n"
            "Ab <code>/mybatches</code> ya <code>/batch &lt;ID&gt; physics</code> chalao.</i></blockquote>")
        _schedule_delete(status_msg.chat.id, status_msg.id)
    except Exception as e:
        logger.error(f"Login pipeline failed: {e}")
        await status_msg.edit_text(f"<blockquote><i>❌ <b>Login Failed:</b>\n<code>{str(e)}</code></i></blockquote>")
        _schedule_delete(status_msg.chat.id, status_msg.id)


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
        await message.reply_text("<blockquote><i>⚠️ Usage: <code>/token &lt;ALLEN_JWT_TOKEN&gt;</code> ya <code>/token &lt;TOKEN&gt; -c &lt;channel_id&gt;</code></i></blockquote>")
        return

    token = args[1].strip()
    session_chat_id = message.chat.id
    if " -c " in token:
        token, _, chan = token.rpartition(" -c ")
        token = token.strip()
        try:
            session_chat_id = int(chan.strip())
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
            return
    try:
        await message.delete()  # token wali message turant hatao
    except Exception as e:
        logger.warning(f"/token command delete failed: {e}")
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
                   "refresh_token": "", "device_id": get_allen_device_id(session_chat_id),
                   "host": "api.allen-live.in"}
        save_allen_session(session, session_chat_id)
        ok_msg = await message.reply_text("<blockquote><i>🎉 <b>Token Saved! Session Active.</b>\n\nNow run: <code>/mybatches</code> to see your batches</i></blockquote>")
        _schedule_delete(ok_msg.chat.id, ok_msg.id)
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
        token = await asyncio.to_thread(get_or_login_allen_token, False, message.chat.id)
        info = await asyncio.to_thread(fetch_student_info, token, message.chat.id)
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


# ==========================================
# CUSTOM THUMBNAILS (PER CHANNEL)
# ==========================================
THUMB_FILE = "channel_thumbs.json"
PENDING_THUMB = {}  # chat_id -> {"event": asyncio.Event, "file_id": str|None, "user_id": int}

def _load_thumbs():
    if os.path.exists(THUMB_FILE):
        try:
            with open(THUMB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    try:  # Heroku restart ke baad DB se wapas lao
        raw = kv_get("thumbs")
        if raw:
            data = json.loads(raw)
            with open(THUMB_FILE, "w") as f:
                json.dump(data, f)
            return data
    except Exception:
        pass
    return {}

def get_channel_thumb(chat_id):
    """Channel ka thumbnail, warna owner ka default thumbnail (sab channels ke liye)."""
    data = _load_thumbs()
    return data.get(str(chat_id)) or data.get("default")

def set_channel_thumb(chat_id, file_id):
    data = _load_thumbs()
    if file_id:
        data[str(chat_id)] = file_id
    else:
        data.pop(str(chat_id), None)
    try:
        with open(THUMB_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving thumbnails: {e}")
    try:
        kv_set("thumbs", json.dumps(data))
    except Exception:
        pass


SUBJECT_ORDER = {"physics": 0, "chemistry": 1, "maths": 2, "mathematics": 2, "math": 2, "biology": 3}


def premium_emoji(name, fallback):
    """Premium (custom) emoji: set Heroku Config Var EMOJI_<NAME>=<custom_emoji_id>.
    Not set -> normal emoji."""
    eid = os.getenv(f"EMOJI_{name.upper()}", "").strip()
    return f'<emoji id="{eid}">{fallback}</emoji>' if eid.isdigit() else fallback


def _kind_label(kind):
    return "Live Lectures" if "live" in str(kind or "").lower() else "Recorded Lectures"


def _kind_emoji(kind):
    return premium_emoji("live", "🔴") if "live" in str(kind or "").lower() else premium_emoji("recorded", "🎬")


def sort_for_index(items):
    """Subject -> Live/Recorded -> original Allen order (chapters stay in sequence)."""
    pos = {id(i): n for n, i in enumerate(items)}
    def k(i):
        subj = str(i.get("subject") or "").strip().lower()
        live = 0 if "live" in str(i.get("kind") or "").lower() else 1
        return (SUBJECT_ORDER.get(subj, 9), subj, live, pos[id(i)])
    return sorted(items, key=k)


async def run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg, quiet=False, result=None):
    result = result if result is not None else {}
    """Pipeline: parallel downloads feeding a single uploader -> max speed per job.
    Multiple jobs (different batches / channels) can run at the same time."""
    key = _job_key(target_chat_id, batch_id, subject)
    work_dir = os.path.join(DOWNLOAD_DIR, str(abs(hash(key)) % 10**8))
    STOPPED_DIRS.discard(os.path.abspath(work_dir))
    ACTIVE_JOBS[key] = {"running": True, "chat": message.chat.id, "work_dir": work_dir, "tasks": []}
    section_index = []  # (subject, kind, chapter, section, message link)
    announced_blocks = set()
    announced_sections = {}
    os.makedirs(work_dir, exist_ok=True)
    done = load_done(target_chat_id)
    try:
        remote_done = await load_state_from_channel(app, target_chat_id)
        if remote_done:
            done |= remote_done
    except Exception as e:
        logger.warning(f"Remote progress load failed: {e}")
    try:
        chan_titles = await scan_channel_uploads(app, target_chat_id)
    except Exception as e:
        logger.warning(f"Channel scan failed: {e}")
        chan_titles = set()
    ok = failed = 0
    failed_titles = []

    custom_thumb_path = None
    thumb_fid = get_channel_thumb(target_chat_id)
    if thumb_fid:
        try:
            custom_thumb_path = await app.download_media(thumb_fid, file_name=os.path.join(work_dir, "custom_thumb.jpg"))
        except Exception as e:
            logger.warning(f"Custom thumbnail download failed: {e}")
            custom_thumb_path = None

    try:
        items = await asyncio.to_thread(_fetch_items, batch_id, token, subject, target_chat_id)
        pending = [i for i in items
                   if i["id"] not in done and _title_key(i.get("title")) not in chan_titles]
        if not items:
            await status_msg.edit_text(
                "<blockquote><i>❌ Allen library me is batch/subject ka lecture link nahi mila. "
                "Login/session valid hai—dobara login mat karein. ✅ Batch ID ko <code>/mybatches</code> se check karein.</i></blockquote>")
            return
        pending = sort_for_index(pending)
        result.update(items=items, pending=len(pending), ok=0, failed=0, failed_titles=[])
        if not pending:
            await status_msg.edit_text("<blockquote><i>✅ Ye sab pehle hi upload ho chuka hai.</i></blockquote>")
            return

        total = len(pending)
        await status_msg.edit_text(
            f"<blockquote><i>🚀 <b>{total} new items</b> (total {len(items)}) — turbo mode ON "
            f"({MAX_PARALLEL_DOWNLOADS} parallel downloads, {DL_THREADS} threads each)</i></blockquote>")

        UPLOAD_PARALLELISM = int(os.getenv("UPLOAD_PARALLELISM", str(AUTO_PARALLEL)))
        queue = asyncio.Queue(maxsize=MAX_PARALLEL_DOWNLOADS + UPLOAD_PARALLELISM + 2)
        # Bound downloaded files too: a slow first lecture must not fill the disk
        # while later parallel downloads complete ahead of it.
        slots = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS + UPLOAD_PARALLELISM + 2)
        index_iter = iter(list(enumerate(pending, start=1)))
        lock = asyncio.Lock()

        def running():
            return ACTIVE_JOBS.get(key, {}).get("running", False)

        async def downloader():
            nonlocal failed
            while running():
                await slots.acquire()
                async with lock:
                    nxt = next(index_iter, None)
                if nxt is None:
                    slots.release()
                    return
                idx, item = nxt
                title = item.get("title") or f"Lecture_{idx}"
                clean = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip()[:70] or f"item_{idx}"
                clean = f"{idx}_{clean}"
                url = item.get("url")
                path = None
                err = None
                try:
                    if not url:
                        raise ValueError("Lecture URL missing")
                    if url.startswith("vora://"):
                        path = await vora_download(url, clean, work_dir, target_chat_id)
                    elif ".m3u8" in url:
                        path = await async_download_m3u8(url, clean, token, work_dir)
                    else:
                        ext = os.path.splitext(url.split("?")[0])[1] or ".pdf"
                        path = await asyncio.to_thread(download_file, url, os.path.join(work_dir, clean + ext))
                except Exception as e:
                    err = e
                if path is None and err is not None:
                    if not running():
                        slots.release()
                        return
                    # Lecture links expire after a few hours -> fetch fresh link and retry once.
                    try:
                        fresh = await refresh_url(item)
                        if fresh and fresh != url:
                            item["url"] = fresh
                            if fresh.startswith("vora://"):
                                path = await vora_download(fresh, clean, work_dir, target_chat_id)
                            elif ".m3u8" in fresh:
                                path = await async_download_m3u8(fresh, clean, token, work_dir)
                            else:
                                ext = os.path.splitext(fresh.split("?")[0])[1] or ".pdf"
                                path = await asyncio.to_thread(download_file, fresh, os.path.join(work_dir, clean + ext))
                    except Exception as e2:
                        err = e2
                if path is None and running():
                    failed += 1
                    logger.error(f"Download failed ({title}): {err}")
                    failed_titles.append(f"{title} (download)")
                # Even failures have an index, so the uploader can advance in order.
                await queue.put((idx, item, title, path))

        fresh_cache = {"at": 0, "map": {}}
        refresh_lock = asyncio.Lock()

        async def refresh_url(item):
            # Vora links (vora://) kabhi expire nahi hote; poora folder tree dobara padhna = 429 + block
            if str(batch_id or "").startswith("vora:"):
                return item.get("url")
            async with refresh_lock:
                if time.time() - fresh_cache["at"] > 600:
                    try:
                        new_items = await asyncio.to_thread(_fetch_items, batch_id, token, subject, target_chat_id)
                        fresh_cache["map"] = {i["id"]: i.get("url") for i in new_items}
                        fresh_cache["at"] = time.time()
                        logger.info(f"Refreshed {len(new_items)} lecture links")
                    except Exception as re_:
                        logger.warning(f"Link refresh failed: {re_}")
                        fresh_cache["at"] = time.time()
                return fresh_cache["map"].get(item["id"])

        def item_group(item):
            subj = str(item.get("subject") or "").strip()
            topic = str(item.get("topic") or "").strip()
            chapter = topic or subj or "Chapter"
            section = str(item.get("section") or "Other Material").strip()
            return subj, _kind_label(item.get("kind")), chapter, section

        async def announce_group(item):
            group = item_group(item)
            if group in announced_sections:
                return
            subj, kind, chapter, section = group
            batch_name = str(item.get("batch") or "Allen Batch").strip()
            block = (subj, kind)
            if block not in announced_blocks:
                announced_blocks.add(block)
                try:
                    bm = await app.send_message(
                        target_chat_id,
                        f"{premium_emoji('subject', '📘')} <b>{html.escape(subj.upper() or 'SUBJECT')}</b>\n"
                        f"{_kind_emoji(kind)} <b>{html.escape(kind.upper())}</b>\n"
                        f"<i>{html.escape(batch_name)}</i>")
                    log_uploaded_message(target_chat_id, getattr(bm, "id", None))
                    try:
                        await bm.pin(disable_notification=True)
                    except Exception as pe_:
                        logger.warning(f"Block pin failed: {pe_}")
                except Exception as be:
                    logger.warning(f"Block header failed: {be}")
            heading = (f"<b>{html.escape(batch_name)}</b>\n"
                       f"{_kind_emoji(kind)} <b>{html.escape(kind)}</b>\n"
                       f"{premium_emoji('chapter', '📚')} <b>{html.escape(subj)} - {html.escape(chapter)}</b>\n\n"
                       f"{premium_emoji('section', '🔷')} <b>{html.escape(section)}</b>")
            try:
                cm = await app.send_message(target_chat_id, heading)
                log_uploaded_message(target_chat_id, getattr(cm, "id", None))
                announced_sections[group] = getattr(cm, "link", None)
                section_index.append((subj, kind, chapter, section, getattr(cm, "link", None)))
                try:
                    await cm.pin(disable_notification=True)
                except Exception as pin_err:
                    logger.warning(f"Section pin failed: {pin_err}")
            except Exception as ce:
                announced_sections[group] = None
                section_index.append((subj, kind, chapter, section, None))
                logger.warning(f"Section header failed: {ce}")

        async def uploader():
            nonlocal ok, failed
            next_index = 1
            ready = {}
            in_flight = set()

            async def upload_one(idx, item, title, path):
                nonlocal ok, failed
                video_path = thumb_path = None
                try:
                    if path is None:
                        return
                    upload_path = path
                    if path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov")):
                        video_path, thumb_path = await asyncio.to_thread(prepare_video_for_upload, path)
                        upload_path = video_path
                        if custom_thumb_path and os.path.exists(custom_thumb_path):
                            thumb_path = None  # generated thumb discard; custom use hoga
                    dur = w = h = 0
                    if upload_path.lower().endswith((".mp4", ".mkv", ".ts", ".webm", ".mov")):
                        dur, w, h = await asyncio.to_thread(probe_video, upload_path)
                    parts = [upload_path]
                    if os.path.getsize(upload_path) > TG_UPLOAD_LIMIT:
                        parts = await asyncio.to_thread(split_video, upload_path, dur)
                    for pn, part in enumerate(parts, 1):
                        pdur, pw, ph = (dur, w, h) if len(parts) == 1 else await asyncio.to_thread(probe_video, part)
                        caption = build_caption(item, upload_path, pdur)
                        pitem = item
                        if len(parts) > 1:
                            caption = caption.replace("\nBatch Name :", f"\n<b>Part : {pn}/{len(parts)}</b>\nBatch Name :", 1)
                            pitem = dict(item, id=f"{item.get('id')}#p{pn}",
                                         title=f"{item.get('title')} (Part {pn})")
                        sent_msg = await async_upload_to_telegram(app, target_chat_id, part, caption,
                                                       custom_thumb_path or thumb_path, pdur, pw, ph)
                        if sent_msg is not None:
                            await asyncio.to_thread(catalog_add, pitem, target_chat_id, sent_msg.id, idx)
                        if part != upload_path and os.path.exists(part):
                            os.remove(part)
                    mark_done(item["id"], done, target_chat_id)
                    ok += 1
                    if ok % 25 == 0:
                        asyncio.create_task(save_state_to_channel(app, target_chat_id, done))
                except Exception as e:
                    failed += 1
                    logger.error(f"Upload failed ({title}): {e}")
                    failed_titles.append(f"{title} (upload)")
                finally:
                    for output in (path, video_path, thumb_path):
                        if output and os.path.exists(output):
                            try:
                                os.remove(output)
                            except Exception:
                                pass
                    slots.release()
                    gc.collect()

            while next_index <= total and running():
                try:
                    result = await asyncio.wait_for(queue.get(), timeout=5)
                    ready[result[0]] = result
                except asyncio.TimeoutError:
                    if all(d.done() for d in downloaders) and queue.empty():
                        break
                    continue
                while next_index in ready:
                    if len(in_flight) >= UPLOAD_PARALLELISM:
                        done_tasks, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                        in_flight -= done_tasks
                        continue
                    idx, item, title, path = ready.pop(next_index)
                    next_index += 1
                    if path is None:
                        slots.release()
                        continue  # failed lecture: no empty heading
                    group = item_group(item)
                    if group not in announced_sections:
                        # Finish the old section before pinning the next heading.
                        if in_flight:
                            await asyncio.gather(*in_flight, return_exceptions=True)
                            in_flight.clear()
                        try:
                            await announce_group(item)
                        except Exception as ae:
                            logger.warning(f"Heading failed: {ae}")
                    in_flight.add(asyncio.create_task(upload_one(idx, item, title, path)))
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

        downloaders = [asyncio.create_task(downloader()) for _ in range(MAX_PARALLEL_DOWNLOADS)]
        up = asyncio.create_task(uploader())
        ACTIVE_JOBS[key]["tasks"] = downloaders + [up]

        def _up_done(t):
            # Uploader crash ho jaye to downloaders hamesha ke liye atke na rahein.
            if t.cancelled() or t.exception() is not None:
                if not t.cancelled():
                    logger.error(f"Uploader crashed: {t.exception()}")
                for d in downloaders:
                    d.cancel()
        up.add_done_callback(_up_done)
        await asyncio.gather(*downloaders, return_exceptions=True)
        try:
            await up
        except asyncio.CancelledError:
            pass
        except Exception as ue:
            await message.reply_text(f"<blockquote><i>❌ Upload error: <code>{html.escape(str(ue))}</code></i></blockquote>")

        stopped = not running()
        if section_index:
            blocks = {}
            for subj, kind, chapter, section, link in section_index:
                blocks.setdefault((subj, kind), []).append((chapter, section, link))
            order = sorted(blocks, key=lambda b: (SUBJECT_ORDER.get(b[0].lower(), 9), b[0].lower(),
                                                  0 if "live" in b[1].lower() else 1))
            for subj, kind in order:
                rows = []
                last_ch = None
                ch_no = 0
                for chapter, section, link in blocks[(subj, kind)]:
                    if chapter != last_ch:
                        ch_no += 1
                        rows.append(f"\n<b>{ch_no}. {premium_emoji('chapter', '📚')} {html.escape(chapter)}</b>")
                        last_ch = chapter
                    label = premium_emoji("section", "🔷") + " " + html.escape(section)
                    rows.append(f'   <a href="{link}">{label}</a>' if link else f"   {label}")
                head = (f"{premium_emoji('index', '📑')} <b>{html.escape(subj)} — {_kind_emoji(kind)} {html.escape(kind)} Index</b>"
                        + (" (stopped)" if stopped else ""))
                chunk = head
                for row in rows:
                    if len(chunk) + len(row) + 1 > 3900:
                        im = await app.send_message(target_chat_id, chunk, disable_web_page_preview=True)
                        log_uploaded_message(target_chat_id, getattr(im, "id", None))
                        chunk = head + " (contd.)"
                    chunk += "\n" + row
                im = await app.send_message(target_chat_id, chunk, disable_web_page_preview=True)
                log_uploaded_message(target_chat_id, getattr(im, "id", None))
                try:
                    await im.pin(disable_notification=True)
                except Exception:
                    pass
        result.update(ok=ok, failed=failed, failed_titles=list(failed_titles), stopped=stopped)
        if stopped:
            await message.reply_text(
                f"<blockquote><i>🛑 Stopped. Uploaded: {ok} | Failed: {failed}</i></blockquote>")
            return

        await save_state_to_channel(app, target_chat_id, done)
        if quiet:
            return
        if failed_titles:
            txt = "⚠️ Failed list:\n" + "\n".join(failed_titles[:40])
            if len(failed_titles) > 40:
                txt += f"\n...aur {len(failed_titles)-40}"
            await message.reply_text(txt[:4000])
        await message.reply_text(
            f"<blockquote><i>✅ <b>Done!</b> Uploaded: {ok} | Failed: {failed}\n"
            f"Scope: {batch_id or 'ALL batches'} | {subject or 'All subjects'}</i></blockquote>")
    except Exception as e:
        logger.error(f"Batch job failed: {e}")
        await message.reply_text(f"<blockquote><i>❌ <b>Batch Error:</b> <code>{str(e)}</code></i></blockquote>")
    finally:
        ACTIVE_JOBS.pop(key, None)
        shutil.rmtree(work_dir, ignore_errors=True)


SHUTTING_DOWN = {"v": False}


def _mj_load():
    try:
        return json.loads(kv_get("manual_jobs") or "{}")
    except Exception:
        return {}


def _mj_set(k, v):
    try:
        d = _mj_load()
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
        kv_set("manual_jobs", json.dumps(d))
    except Exception as e:
        logger.warning(f"manual job save failed: {e}")


async def _tracked_batch(message, token, batch_id, subject, target_chat_id, status_msg):
    """/batch job ko DB me yaad rakho — redeploy/restart ke baad apne aap wahin se chalega."""
    k = f"{target_chat_id}|{batch_id or ''}|{subject or ''}"
    await asyncio.to_thread(_mj_set, k, {"chat": message.chat.id, "target": target_chat_id,
                                         "batch": batch_id, "subject": subject})
    await run_batch_job(message, token, batch_id, subject, target_chat_id, status_msg)
    if not SHUTTING_DOWN["v"]:
        await asyncio.to_thread(_mj_set, k, None)


async def resume_manual_jobs():
    jobs = await asyncio.to_thread(_mj_load)
    for k, j in jobs.items():
        try:
            tgt = int(j["target"])
            if str(j.get("batch") or "").startswith("vora:"):
                token = ""
            else:
                token = await asyncio.to_thread(get_or_login_allen_token, False, tgt)
            note = _Notifier(int(j.get("chat") or tgt))
            st = await app.send_message(note.chat.id,
                                        "<blockquote><i>♻️ Restart ke baad upload wahin se continue ho raha hai...</i></blockquote>")
            asyncio.create_task(_tracked_batch(note, token, j.get("batch"), j.get("subject"), tgt, st))
        except Exception as e:
            logger.error(f"Manual resume failed {k}: {e}")
            try:
                await app.send_message(int(j.get("chat") or OWNER_ID),
                                       f"<blockquote><i>⚠️ Restart ke baad job resume nahi hua: <code>{html.escape(str(e))}</code>\n"
                                       "Is channel me dobara /login karke /batch chalao.</i></blockquote>")
            except Exception:
                pass


@app.on_message(filters.command(["batch", "downloadall"]) & (filters.group | filters.channel | filters.private))
async def handle_batch(client: Client, message: Message):
    logger.info(f"/batch triggered by {message.from_user.id if message.from_user else 'Unknown'}")
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
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

    try:
        token = await asyncio.to_thread(get_or_login_allen_token, False, target_chat_id)
    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <b>Session Error:</b> <code>{str(e)}</code></i></blockquote>")
        return

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

    # Custom thumbnail: agar is channel ke liye set nahi hai toh pehle poochho.
    if not get_channel_thumb(target_chat_id):
        pend = {"event": asyncio.Event(), "file_id": None,
                "user_id": message.from_user.id if message.from_user else None}
        PENDING_THUMB[message.chat.id] = pend
        ask_msg = await message.reply_text(
            "<blockquote><i>🖼 <b>Custom thumbnail bhejo</b> (ek photo) — ye is channel ke saare videos par lagega.\n"
            "Auto thumbnail chahiye toh <code>/skip</code> bhejo. (90 sec wait)</i></blockquote>")
        try:
            await asyncio.wait_for(pend["event"].wait(), timeout=90)
        except asyncio.TimeoutError:
            pass
        PENDING_THUMB.pop(message.chat.id, None)
        try:
            await ask_msg.delete()
        except Exception:
            pass
        if pend["file_id"]:
            set_channel_thumb(target_chat_id, pend["file_id"])
            await message.reply_text("<blockquote><i>✅ Custom thumbnail set ho gaya.</i></blockquote>")

    status_msg = await message.reply_text(
        f"<blockquote><i>🔄 <b>Fetching:</b> {scope}\nUpload → {dest}</i></blockquote>")

    # fire-and-forget so multiple batches/channels download simultaneously
    asyncio.create_task(_tracked_batch(message, token, batch_id, subject, target_chat_id, status_msg))


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
        out = {"student_info": None, "navigation_subject_urls": [], "pages": []}
        r = requests.get(f"{ALLEN_BASE_URL}/user/studentInfo", headers=allen_content_headers(token), timeout=25)
        try:
            info = r.json()
        except Exception:
            info = {"raw": r.text[:2000]}
        out["student_info"] = {"http": r.status_code, "body": info}
        out["navigation_subject_urls"] = _discover_subject_urls(token)
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
            if v.get("work_dir"):
                kill_job_processes(v["work_dir"])
            for t in v.get("tasks") or []:
                if not t.done():
                    t.cancel()
            stopped += 1
    if stopped:
        await message.reply_text(f"<blockquote><i>🛑 {stopped} job(s) rok diye.</i></blockquote>")
    else:
        await message.reply_text("<blockquote><i>⚠️ Koi active task nahi hai.</i></blockquote>")


@app.on_message(filters.command("setthumb") & (filters.group | filters.channel | filters.private))
async def handle_setthumb(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    target_chat_id = message.chat.id
    parts = (message.text or "").split()[1:]
    if "-c" in parts:
        i = parts.index("-c")
        try:
            target_chat_id = int(parts[i + 1])
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
            return
    photo = message.photo or (message.reply_to_message.photo if message.reply_to_message else None)
    if not photo:
        await message.reply_text("<blockquote><i>⚠️ Ek photo ke saath <code>/setthumb</code> bhejo (caption me) ya kisi photo ko reply karke <code>/setthumb</code> likho.\nChannel ke liye: <code>/setthumb -c &lt;channel_id&gt;</code></i></blockquote>")
        return
    if target_chat_id == message.chat.id and message.chat.type == enums.ChatType.PRIVATE:
        set_channel_thumb("default", photo.file_id)
        await message.reply_text("<blockquote><i>✅ Default thumbnail set — ab har naye/purane channel ke videos par yahi lagega (jinka alag thumbnail set nahi hai).</i></blockquote>")
        return
    set_channel_thumb(target_chat_id, photo.file_id)
    await message.reply_text(f"<blockquote><i>✅ Custom thumbnail set ho gaya ({'channel ' + str(target_chat_id) if target_chat_id != message.chat.id else 'is chat'} ke liye).</i></blockquote>")


@app.on_message(filters.command("delthumb") & (filters.group | filters.channel | filters.private))
async def handle_delthumb(client: Client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    target_chat_id = message.chat.id
    parts = (message.text or "").split()[1:]
    if "-c" in parts:
        i = parts.index("-c")
        try:
            target_chat_id = int(parts[i + 1])
        except Exception:
            return
    if target_chat_id == message.chat.id and message.chat.type == enums.ChatType.PRIVATE:
        target_chat_id = "default"
    set_channel_thumb(target_chat_id, None)
    await message.reply_text("<blockquote><i>🗑 Custom thumbnail hata diya. Ab auto thumbnail lagega.</i></blockquote>")


@app.on_message(filters.command("skip") & (filters.group | filters.channel | filters.private))
async def handle_skip(client: Client, message: Message):
    pend = PENDING_THUMB.get(message.chat.id)
    if pend:
        pend["file_id"] = None
        pend["event"].set()


@app.on_message(filters.photo & (filters.group | filters.channel | filters.private))
async def handle_photo(client: Client, message: Message):
    pend = PENDING_THUMB.get(message.chat.id)
    if not pend or pend["event"].is_set():
        return
    if pend["user_id"] and message.from_user and message.from_user.id != pend["user_id"]:
        return
    pend["file_id"] = message.photo.file_id
    pend["event"].set()


@app.on_message(filters.command("id"))
async def show_id(client: Client, message: Message):
    await message.reply_text(f"<blockquote><i>🆔 Chat ID: <code>{message.chat.id}</code></i></blockquote>")



# ==========================================
# AUTO CHANNEL + LECTURE LIBRARY
# ==========================================
import hashlib
import sqlite3
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPrivileges, CallbackQuery

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USER_SESSION = os.getenv("USER_SESSION", "").strip()
HELPER = {"client": None}
CONNECT_FLOW = {}  # owner_id -> {"step", "client", "phone", "hash"}
CHANNEL_CREATE_LOCK = asyncio.Lock()
LIB_PAGE = 20


def _db():
    if DATABASE_URL:
        import psycopg2
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url, sslmode=os.getenv("DB_SSLMODE", "require")), "%s"
    return sqlite3.connect(os.path.join(DOWNLOAD_DIR, "..", "library.db")), "?"


def db_exec(sql, params=(), fetch=False):
    conn, ph = _db()
    try:
        cur = conn.cursor()
        cur.execute(sql.replace("%s", ph), params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def db_init():
    stmts = [
        """CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY, channel_id BIGINT NOT NULL,
            invite_link TEXT, created_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS catalog (
            batch_name TEXT NOT NULL, subject TEXT, kind TEXT, chapter TEXT, section TEXT,
            seq BIGINT, title TEXT, content_key TEXT NOT NULL,
            channel_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
            PRIMARY KEY (batch_name, content_key))""",
        """CREATE TABLE IF NOT EXISTS library_users (user_id BIGINT PRIMARY KEY)""",
        """CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)""",
    ]
    for s in stmts:
        db_exec(s)


def kv_get(k):
    rows = db_exec("SELECT v FROM kv WHERE k=%s", (k,), fetch=True)
    return rows[0][0] if rows else None


def kv_set(k, v):
    db_exec("INSERT INTO kv (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, v))


def catalog_add(item, channel_id, message_id, seq):
    try:
        batch = str(item.get("batch") or "Allen Batch").strip()
        subj = str(item.get("subject") or "").strip() or "Other"
        chapter = str(item.get("topic") or "").strip() or subj
        key = str(item.get("id") or "") or _title_key(item.get("title"))
        db_exec("""INSERT INTO catalog (batch_name, subject, kind, chapter, section, seq, title,
                   content_key, channel_id, message_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (batch, subj, _kind_label(item.get("kind")), chapter,
                 str(item.get("section") or ""), int(seq), str(item.get("title") or ""),
                 key, int(channel_id), int(message_id)))
    except Exception as e:
        logger.warning(f"Catalog add failed: {e}")


def _h(s):
    return hashlib.md5(str(s).encode()).hexdigest()[:8]


def library_allowed(uid):
    if uid == OWNER_ID or uid in AUTHORIZED_USERS:
        return True
    try:
        return bool(db_exec("SELECT 1 FROM library_users WHERE user_id=%s", (uid,), fetch=True))
    except Exception:
        return False


# ---------- helper (user) account ----------
async def get_helper():
    if HELPER["client"]:
        return HELPER["client"]
    sess = USER_SESSION
    if not sess:
        try:
            sess = await asyncio.to_thread(kv_get, "user_session") or ""
        except Exception:
            sess = ""
    if not sess:
        return None
    c = Client("helper_user", api_id=TG_API_ID, api_hash=TG_API_HASH,
               session_string=sess, in_memory=True, no_updates=True)
    await c.start()
    HELPER["client"] = c
    return c


@app.on_message(filters.command("connectuser") & filters.private)
async def handle_connectuser(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.reply_text("<blockquote><i>🚫 Owner-only command.</i></blockquote>")
        return
    CONNECT_FLOW[OWNER_ID] = {"step": "phone"}
    await message.reply_text(
        "<blockquote><i>📱 Helper Telegram account ka number bhejo (country code ke saath), jaise "
        "<code>+919876543210</code>.\nSpare number use karo, main number nahi.\nCancel: <code>/cancel</code></i></blockquote>")


@app.on_message(filters.command("cancel") & filters.private)
async def handle_cancel(client, message: Message):
    flow = CONNECT_FLOW.pop(message.from_user.id if message.from_user else 0, None)
    if flow and flow.get("client"):
        try:
            await flow["client"].disconnect()
        except Exception:
            pass
    await message.reply_text("<blockquote><i>❎ Cancel ho gaya.</i></blockquote>")


@app.on_message(filters.private & filters.text & ~filters.regex(r"^/"), group=1)
async def handle_connect_steps(client, message: Message):
    uid = message.from_user.id if message.from_user else 0
    flow = CONNECT_FLOW.get(uid)
    if not flow:
        return
    from pyrogram.errors import SessionPasswordNeeded
    text = message.text.strip()
    try:
        if flow["step"] == "phone":
            c = Client("helper_login", api_id=TG_API_ID, api_hash=TG_API_HASH, in_memory=True)
            await c.connect()
            sent = await c.send_code(text)
            flow.update(step="code", client=c, phone=text, hash=sent.phone_code_hash)
            await message.reply_text(
                "<blockquote><i>🔢 Telegram ne OTP bheja hai. Code beech me space daal ke bhejo, jaise "
                "<code>1 2 3 4 5</code> (warna Telegram code block kar deta hai).</i></blockquote>")
        elif flow["step"] == "code":
            code = text.replace(" ", "").replace("-", "")
            try:
                await flow["client"].sign_in(flow["phone"], flow["hash"], code)
            except SessionPasswordNeeded:
                flow["step"] = "password"
                await message.reply_text("<blockquote><i>🔐 2-Step password bhejo.</i></blockquote>")
                return
            await _finish_connect(message, flow)
        elif flow["step"] == "password":
            try:
                await message.delete()
            except Exception:
                pass
            await flow["client"].check_password(text)
            await _finish_connect(message, flow)
    except Exception as e:
        CONNECT_FLOW.pop(uid, None)
        await message.reply_text(f"<blockquote><i>❌ Helper login fail: <code>{html.escape(str(e))}</code>\nDobara <code>/connectuser</code> karo.</i></blockquote>")


async def _finish_connect(message, flow):
    c = flow["client"]
    sess = await c.export_session_string()
    await c.disconnect()
    CONNECT_FLOW.pop(OWNER_ID, None)
    await asyncio.to_thread(kv_set, "user_session", sess)
    HELPER["client"] = None
    await get_helper()
    await message.reply_text(
        "<blockquote><i>✅ <b>Helper account connect ho gaya.</b> Ab <code>/login username*password</code> "
        "bot ke DM me bhejo — channel apne aap banega.</i></blockquote>")


async def get_or_create_account_channel(username, title_hint):
    rows = await asyncio.to_thread(db_exec, "SELECT channel_id, invite_link FROM accounts WHERE username=%s",
                                   (username,), True)
    if rows:
        return int(rows[0][0]), rows[0][1], False
    helper = await get_helper()
    if not helper:
        raise ValueError("Helper account connect nahi hai. Pehle owner <code>/connectuser</code> kare.")
    async with CHANNEL_CREATE_LOCK:
        last = float(await asyncio.to_thread(kv_get, "last_channel_at") or 0)
        wait = 180 - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        chan = await helper.create_channel(f"LN | {title_hint}"[:120], "Courier Well lectures")
        await asyncio.to_thread(kv_set, "last_channel_at", str(time.time()))
    me = await app.get_me()
    await helper.promote_chat_member(chan.id, me.username, privileges=ChatPrivileges(
        can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
        can_delete_messages=True, can_pin_messages=True, can_invite_users=True))
    try:
        await add_libbot_to_channel(helper, chan.id)
    except Exception as e:
        logger.warning(f"Library bot add failed: {e}")
    try:
        link = await helper.export_chat_invite_link(chan.id)
    except Exception:
        link = None
    await asyncio.to_thread(db_exec,
                            "INSERT INTO accounts (username, channel_id, invite_link, created_at) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (username) DO NOTHING",
                            (username, int(chan.id), link, time.strftime("%Y-%m-%d %H:%M")))
    return int(chan.id), link, True


async def auto_channel_flow(message, username, password):
    """DM /login: account ka channel banao/dhundho, login bind karo, saare batches upload karo."""
    status = await message.reply_text("<blockquote><i>🔑 Allen login check ho raha hai...</i></blockquote>")
    try:
        token = await asyncio.to_thread(allen_login_idpass, username, password, message.chat.id)
        info = await asyncio.to_thread(fetch_student_info, token, message.chat.id)
        st = info.get("student_detail") or {}
        courses = info.get("course_details") or []
        name = f"{st.get('first_name', '')} {st.get('last_name', '')}".strip() or username
        course = (courses[0].get("course_name") if courses else "") or "Allen"
        await status.edit_text("<blockquote><i>📢 Channel ready kiya ja raha hai...</i></blockquote>")
        chan_id, link, created = await get_or_create_account_channel(username, f"{name} | {course}")
        token = await asyncio.to_thread(allen_login_idpass, username, password, chan_id)
        if username in ACCOUNT_RUNNING:
            await status.edit_text("<blockquote><i>⚠️ Is account ka upload already chal raha hai.</i></blockquote>")
            return
        await status.edit_text(
            f"<blockquote><i>{'🆕 Naya channel bana' if created else '♻️ Purana channel mila'}: <code>{chan_id}</code>\n"
            + (f"🔗 {link}\n" if link else "")
            + "🚀 Saare batches upload shuru — sirf bache hue lectures jayenge.</i></blockquote>")
        await asyncio.to_thread(save_creds, username, password, message.chat.id)
        if not get_channel_thumb(chan_id):
            pend = {"event": asyncio.Event(), "file_id": None,
                    "user_id": message.from_user.id if message.from_user else None}
            PENDING_THUMB[message.chat.id] = pend
            ask = await message.reply_text(
                "<blockquote><i>🖼 <b>Is channel ka thumbnail bhejo</b> (ek photo), ya <code>/skip</code>.\n"
                "Tip: DM me <code>/setthumb</code> (photo ke caption me) bhejoge toh wo sab channels ka default ban jayega. (90 sec wait)</i></blockquote>")
            try:
                await asyncio.wait_for(pend["event"].wait(), timeout=90)
            except asyncio.TimeoutError:
                pass
            PENDING_THUMB.pop(message.chat.id, None)
            try:
                await ask.delete()
            except Exception:
                pass
            if pend["file_id"]:
                set_channel_thumb(chan_id, pend["file_id"])
                await message.reply_text("<blockquote><i>✅ Thumbnail set ho gaya.</i></blockquote>")
        asyncio.create_task(run_account_job(username, chan_id, message.chat.id, reason="manual"))
    except Exception as e:
        logger.error(f"Auto channel flow failed: {e}")
        await status.edit_text(f"<blockquote><i>❌ <code>{html.escape(str(e))}</code></i></blockquote>")


@app.on_message(filters.command("channels") & filters.private)
async def handle_channels(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    rows = await asyncio.to_thread(db_exec, "SELECT username, channel_id, invite_link FROM accounts", (), True)
    if not rows:
        await message.reply_text("<blockquote><i>Abhi koi auto channel nahi bana.</i></blockquote>")
        return
    out = ["<b>📢 Channels</b>"]
    for u, cid, link in rows:
        cnt = await asyncio.to_thread(db_exec, "SELECT COUNT(*) FROM catalog WHERE channel_id=%s", (cid,), True)
        out.append(f"• <code>{html.escape(str(u))}</code> → <code>{cid}</code> | {cnt[0][0]} lectures"
                   + (f" | <a href=\"{link}\">open</a>" if link else ""))
    await message.reply_text("\n".join(out)[:4000], disable_web_page_preview=True)


@app.on_message(filters.command(["access", "revoke"]) & filters.private)
async def handle_access(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply_text("<blockquote><i>Usage: <code>/access &lt;user_id&gt;</code> / <code>/revoke &lt;user_id&gt;</code></i></blockquote>")
        return
    uid = int(parts[1])
    if parts[0].lower().startswith("/access"):
        await asyncio.to_thread(db_exec, "INSERT INTO library_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
        await message.reply_text(f"<blockquote><i>✅ <code>{uid}</code> ko library access mil gaya.</i></blockquote>")
    else:
        await asyncio.to_thread(db_exec, "DELETE FROM library_users WHERE user_id=%s", (uid,))
        await message.reply_text(f"<blockquote><i>❌ <code>{uid}</code> ka access hata diya.</i></blockquote>")


@app.on_message(filters.command("importchannel") & (filters.private | filters.channel | filters.group))
async def handle_importchannel(client, message: Message):
    if message.from_user and message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    chan = message.chat.id
    if "-c" in parts:
        try:
            chan = int(parts[parts.index("-c") + 1])
        except Exception:
            await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad channel ID do.</i></blockquote>")
            return
    st = await message.reply_text("<blockquote><i>📥 Channel ke purane lectures library me add ho rahe hain...</i></blockquote>")
    added = 0
    try:
        async for m in app.get_chat_history(chan):
            cap = (m.caption or "")
            if not (m.video or m.document) or "File Title :" not in cap:
                continue
            fields = {}
            for line in cap.splitlines():
                if " : " in line:
                    k, _, v = line.partition(" : ")
                    fields[k.strip()] = v.strip()
            topic = fields.get("Topic Name", "")
            subj, _, chapter = topic.partition(" - ")
            item = {"batch": fields.get("Batch Name"), "subject": subj or topic, "topic": chapter or topic,
                    "kind": fields.get("Type", "Recorded Lecture"), "title": fields.get("File Title"),
                    "section": "", "id": "t:" + _title_key(fields.get("File Title"))}
            await asyncio.to_thread(catalog_add, item, chan, m.id, m.id)
            added += 1
        await st.edit_text(f"<blockquote><i>✅ {added} lectures library me add ho gaye.</i></blockquote>")
    except Exception as e:
        await st.edit_text(f"<blockquote><i>❌ Import fail: <code>{html.escape(str(e))}</code>\nBot channel me admin hona chahiye.</i></blockquote>")


# ---------- Library menu ----------
def _lib_rows(sql, params=()):
    return db_exec(sql, params, fetch=True) or []


def _sort_subjects(names):
    return sorted(names, key=lambda s: (SUBJECT_ORDER.get(str(s).lower(), 9), str(s).lower()))


async def lib_batches(uid):
    rows = await asyncio.to_thread(_lib_rows, "SELECT DISTINCT batch_name FROM catalog")
    names = sorted(r[0] for r in rows)
    if not names:
        return "📚 Library abhi khaali hai.", None
    kb = [[InlineKeyboardButton(n[:60], callback_data=f"L1|{_h(n)}")] for n in names]
    return ("📚 <b>COURIER WELL LIBRARY</b>\n━━━━━━━━━━━━━━━━\n"
            f"Total batches: <b>{len(names)}</b>\n\n👇 Apna batch chuno"), InlineKeyboardMarkup(kb)


async def _resolve_batch(bh):
    rows = await asyncio.to_thread(_lib_rows, "SELECT DISTINCT batch_name FROM catalog")
    for r in rows:
        if _h(r[0]) == bh:
            return r[0]
    return None


FSUB_CHANNELS = [c.strip() for c in os.getenv("FSUB_CHANNELS", "").split(",") if c.strip()]
FSUB_LINKS = {}


def _fsub_id(c):
    return int(c) if c.lstrip("-").isdigit() else c


async def fsub_missing(client, uid):
    """Jo channels user ne join nahi kiye (ya leave kar diye) unki list [(title, link)]."""
    if not FSUB_CHANNELS or uid == OWNER_ID:
        return []
    from pyrogram.errors import UserNotParticipant
    missing = []
    for c in FSUB_CHANNELS:
        cid = _fsub_id(c)
        try:
            m = await client.get_chat_member(cid, uid)
            if m.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
                raise UserNotParticipant
            continue
        except UserNotParticipant:
            pass
        except Exception as e:
            if "USER_NOT_PARTICIPANT" not in str(e).upper():
                logger.warning(f"fsub check {c}: {e} (bot ko is channel me admin banao)")
                continue  # bot ki galti par student ko block mat karo
        link = FSUB_LINKS.get(c)
        title = "Channel"
        try:
            ch = await client.get_chat(cid)
            title = ch.title or title
            if not link:
                link = ch.invite_link or (f"https://t.me/{ch.username}" if ch.username else None)
                if not link:
                    link = await client.export_chat_invite_link(cid)
                FSUB_LINKS[c] = link
        except Exception as e:
            logger.warning(f"fsub link {c}: {e}")
        missing.append((title, link))
    return missing


async def fsub_prompt(client, uid, missing, edit_msg=None):
    kb = [[InlineKeyboardButton(f"📢 Join {t}"[:60], url=l)] for t, l in missing if l]
    kb.append([InlineKeyboardButton("✅ Join kar liya", callback_data="L0")])
    text = ("<b>🔒 Pehle channel join karo</b>\n━━━━━━━━━━━━━━━━\n"
            "Lectures tabhi milenge jab aap neeche ke channel(s) join karoge.\n"
            "Channel chhodoge toh lectures band ho jayenge.\n\n👇 Join karke <b>✅ Join kar liya</b> dabao")
    if edit_msg is not None:
        try:
            await edit_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
            return
        except Exception:
            pass
    await client.send_message(uid, text, reply_markup=InlineKeyboardMarkup(kb))


@app.on_message(filters.command(["start", "library"]) & filters.private)
async def handle_library(client, message: Message):
    uid = message.from_user.id if message.from_user else 0
    if not _lib_ok(client, uid):
        await message.reply_text(f"<blockquote><i>🚫 Library access nahi hai. Admin ko apna ID bhejo: <code>{uid}</code></i></blockquote>")
        return
    miss = await fsub_missing(client, uid)
    if miss:
        await fsub_prompt(client, uid, miss)
        return
    if client is app and is_user_authorized(uid) and (message.text or "").startswith("/start"):
        await message.reply_text(PANEL_TEXT, reply_markup=PANEL_KB)
        return
    text, kb = await lib_batches(uid)
    await message.reply_text(text, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^L\d"))
async def handle_lib_cb(client, cq: CallbackQuery):
    uid = cq.from_user.id
    if not _lib_ok(client, uid):
        await cq.answer("Access nahi hai", show_alert=True)
        return
    p = cq.data.split("|")
    lvl = p[0]
    miss = await fsub_missing(client, uid)
    if miss:
        try:
            await cq.answer("🔒 Pehle channel join karo", show_alert=True)
        except Exception:
            pass
        await fsub_prompt(client, uid, miss, cq.message)
        return
    try:
        if lvl == "L0":
            text, kb = await lib_batches(uid)
            await cq.message.edit_text(text, reply_markup=kb)
            return
        batch = await _resolve_batch(p[1])
        if not batch:
            await cq.answer("Batch nahi mila", show_alert=True)
            return
        if lvl == "L1":
            rows = await asyncio.to_thread(_lib_rows, "SELECT DISTINCT subject FROM catalog WHERE batch_name=%s", (batch,))
            subs = _sort_subjects([r[0] for r in rows])
            kb = [[InlineKeyboardButton(f"📘 {s}", callback_data=f"L2|{p[1]}|{_h(s)}")] for s in subs]
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="L0")])
            await cq.message.edit_text(f"<b>{html.escape(batch)}</b>\nSubject chuno", reply_markup=InlineKeyboardMarkup(kb))
            return
        rows = await asyncio.to_thread(_lib_rows, "SELECT DISTINCT subject FROM catalog WHERE batch_name=%s", (batch,))
        subj = next((r[0] for r in rows if _h(r[0]) == p[2]), None)
        if subj is None:
            await cq.answer("Subject nahi mila", show_alert=True)
            return
        if lvl == "L2":
            kb = [[InlineKeyboardButton("🔴 Live Lectures", callback_data=f"L3|{p[1]}|{p[2]}|L")],
                  [InlineKeyboardButton("🎬 Recorded Lectures", callback_data=f"L3|{p[1]}|{p[2]}|R")],
                  [InlineKeyboardButton("⬅️ Back", callback_data=f"L1|{p[1]}")]]
            await cq.message.edit_text(f"<b>{html.escape(batch)}</b>\n📘 {html.escape(subj)}", reply_markup=InlineKeyboardMarkup(kb))
            return
        kind = "Live Lectures" if p[3] == "L" else "Recorded Lectures"
        rows = await asyncio.to_thread(_lib_rows,
                                       "SELECT chapter, MIN(seq) FROM catalog WHERE batch_name=%s AND subject=%s AND kind=%s "
                                       "GROUP BY chapter ORDER BY MIN(seq)", (batch, subj, kind))
        if lvl == "L3":
            if not rows:
                await cq.answer("Is type ke lecture nahi hain", show_alert=True)
                return
            kb = [[InlineKeyboardButton(f"{n}. {r[0]}"[:60], callback_data=f"L4|{p[1]}|{p[2]}|{p[3]}|{_h(r[0])}|0")]
                  for n, r in enumerate(rows, 1)]
            kb.append([InlineKeyboardButton("⬅️ Back", callback_data=f"L2|{p[1]}|{p[2]}")])
            tree = []
            for n, r in enumerate(rows):
                br = "└──" if n == len(rows) - 1 else "├──"
                tree.append(f"{br} 📚 {html.escape(str(r[0]))}")
            head = (f"🔥 <b>{html.escape(batch)}</b> 🔥\n━━━━━━━━━━━━━━━━\n"
                    f"📘 <b>{html.escape(subj)}</b> — {_kind_emoji(kind)} {kind}\n\n")
            body = "\n".join(tree)
            if len(head) + len(body) > 3800:
                body = body[:3800 - len(head)] + "\n..."
            await cq.message.edit_text(head + body + "\n\n👇 Chapter chuno, lectures yahin aa jayenge",
                                       reply_markup=InlineKeyboardMarkup(kb[:99]))
            return
        if lvl == "L4":
            chapter = next((r[0] for r in rows if _h(r[0]) == p[4]), None)
            page = int(p[5])
            items = await asyncio.to_thread(_lib_rows,
                                            "SELECT channel_id, message_id FROM catalog WHERE batch_name=%s AND subject=%s "
                                            "AND kind=%s AND chapter=%s ORDER BY seq, message_id",
                                            (batch, subj, kind, chapter))
            chunk = items[page * LIB_PAGE:(page + 1) * LIB_PAGE]
            if not chunk:
                await cq.answer("Is chapter me abhi lecture nahi hain", show_alert=True)
                return
            await cq.answer(f"{len(chunk)} lectures bhej raha hoon...")
            sent, errs = 0, {}
            for cid, mid in chunk:
                err = await _lib_send(client, uid, int(cid), int(mid))
                if err is None:
                    sent += 1
                else:
                    errs.setdefault(int(cid), err)
                await asyncio.sleep(0.4)
            if errs:
                await client.send_message(uid, (
                    f"<blockquote><i>⚠️ {len(chunk) - sent}/{len(chunk)} lecture nahi bhej paya. "
                    "Admin ko bata diya hai, thodi der baad dobara try karo.</i></blockquote>"))
                for c, er in errs.items():
                    try:
                        await app.send_message(OWNER_ID, (
                            f"<blockquote><i>⚠️ Library bot channel <code>{c}</code> se lecture nahi bhej paya.\n"
                            f"Error: <code>{html.escape(er[:300])}</code>\n\n"
                            f"Fix: us channel me @{LIB.get('username') or 'library bot'} ko admin banao "
                            "(ya /addlibbot chalao), aur channel settings me "
                            "<b>Restrict saving content</b> OFF rakho.</i></blockquote>"))
                    except Exception:
                        pass
            if (page + 1) * LIB_PAGE < len(items):
                await client.send_message(uid, f"Aage ke lectures ({len(items) - (page + 1) * LIB_PAGE} baaki)",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                           "➡️ Next", callback_data=f"L4|{p[1]}|{p[2]}|{p[3]}|{p[4]}|{page + 1}")]]))
    except Exception as e:
        logger.error(f"Library callback failed: {e}")
        try:
            await cq.answer("Error aaya, dobara try karo", show_alert=True)
        except Exception:
            pass



# ---------- Separate library bot (students ke liye ek jagah saare batches) ----------
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
LIBRARY_BOT_TOKEN = os.getenv("LIBRARY_BOT_TOKEN", "").strip()
LIBRARY_PUBLIC = os.getenv("LIBRARY_PUBLIC", "1").strip() != "0"
LIB = {"client": None, "username": None}


async def _lib_send(client, uid, cid, mid):
    """Lecture student ko bhejo. Fail ho toh peer refresh, library bot ko channel me add, phir retry.
    Returns None on success, else error text."""
    last = ""
    for attempt in range(3):
        try:
            await client.copy_message(uid, cid, mid)
            return None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            logger.warning(f"Library copy failed {cid}/{mid} (try {attempt + 1}): {last}")
            if "FLOOD" in last.upper():
                m = re.search(r"(\d+)\s*seconds", last)
                await asyncio.sleep(min(int(m.group(1)) if m else 5, 60))
                continue
        if attempt == 0:
            try:
                await client.get_chat(cid)  # in-memory session ko channel ka pata chale
            except Exception:
                pass
        elif attempt == 1 and client is LIB.get("client"):
            try:
                helper = await get_helper()
                if helper:
                    await add_libbot_to_channel(helper, cid)
                    await asyncio.sleep(1)
                    await client.get_chat(cid)
            except Exception as e:
                logger.warning(f"Library bot auto-add {cid}: {e}")
    if client is not app:
        try:  # last fallback: upload bot (jo channel me admin hai) se bhejo
            await app.copy_message(uid, cid, mid)
            return None
        except Exception:
            pass
    return last


async def _lib_autoadd_all():
    """Startup par library bot ko saare auto-channels me admin bana do (silent)."""
    await asyncio.sleep(20)
    try:
        helper = await get_helper()
        if not helper or not LIB.get("username"):
            return
        rows = await asyncio.to_thread(db_exec, "SELECT DISTINCT channel_id FROM accounts", (), True)
        for r in rows or []:
            try:
                await add_libbot_to_channel(helper, int(r[0]))
            except Exception as e:
                logger.warning(f"lib autoadd {r[0]}: {e}")
            await asyncio.sleep(3)
    except Exception as e:
        logger.warning(f"lib autoadd failed: {e}")


def _lib_ok(client, uid):
    if LIB.get("client") is not None and client is LIB["client"] and LIBRARY_PUBLIC:
        return True
    return library_allowed(uid)


async def start_library_bot():
    if not LIBRARY_BOT_TOKEN:
        return
    try:
        lib = Client("library_bot", api_id=TG_API_ID, api_hash=TG_API_HASH, bot_token=LIBRARY_BOT_TOKEN,
                     workers=16, parse_mode=enums.ParseMode.HTML, in_memory=True)
        lib.add_handler(MessageHandler(handle_library, filters.command(["start", "library"]) & filters.private))
        lib.add_handler(CallbackQueryHandler(handle_lib_cb, filters.regex(r"^L\d")))
        await lib.start()
        me = await lib.get_me()
        LIB["client"], LIB["username"] = lib, me.username
        logger.info(f"Library bot started: @{me.username}")
        asyncio.create_task(_lib_autoadd_all())
    except Exception as e:
        logger.error(f"Library bot start failed: {e}")


async def add_libbot_to_channel(helper, chan_id):
    if not LIB.get("username"):
        return False
    priv = ChatPrivileges(can_manage_chat=True, can_post_messages=True)
    try:
        await helper.promote_chat_member(chan_id, LIB["username"], privileges=priv)
    except Exception as e:
        if "peer id invalid" not in str(e).lower() and "PEER_ID_INVALID" not in str(e):
            raise
        # helper ka in-memory session channel nahi jaanta: dialogs load karke peer cache bharo
        async for _ in helper.get_dialogs():
            pass
        await helper.promote_chat_member(chan_id, LIB["username"], privileges=priv)
    return True


@app.on_message(filters.command("addlibbot") & filters.private)
async def handle_addlibbot(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    if not LIB.get("username"):
        await message.reply_text("<blockquote><i>⚠️ Library bot chalu nahi hai. Heroku Config Vars me <code>LIBRARY_BOT_TOKEN</code> daalo.</i></blockquote>")
        return
    helper = await get_helper()
    rows = await asyncio.to_thread(db_exec, "SELECT channel_id FROM accounts", (), True)
    auto_ids = {int(r[0]) for r in rows or []}
    ok, bad = 0, []
    for cid in auto_ids:
        try:
            if helper and await add_libbot_to_channel(helper, cid):
                ok += 1
            else:
                bad.append(cid)
        except Exception as e:
            bad.append(cid)
            logger.warning(f"addlibbot {cid}: {e}")
        await asyncio.sleep(2)
    other = await asyncio.to_thread(db_exec, "SELECT DISTINCT channel_id FROM catalog", (), True)
    manual = [int(r[0]) for r in other or [] if int(r[0]) not in auto_ids]
    out = f"✅ @{LIB['username']} {ok} auto channels me add ho gaya."
    if bad:
        out += "\n⚠️ Nahi hua: " + ", ".join(f"<code>{c}</code>" for c in bad)
    if manual:
        out += ("\n\n📌 In channels me khud @" + LIB["username"] + " ko admin banao:\n"
                + "\n".join(f"<code>{c}</code>" for c in manual))
    await message.reply_text(f"<blockquote><i>{out}</i></blockquote>")


# ==========================================
# FULL AUTOMATION: account jobs, retries, resume, 6h auto-check
# ==========================================
import base64 as _b64
from pyrogram import idle

AUTO_INTERVAL = int(os.getenv("AUTO_CHECK_HOURS", "6")) * 3600
RETRY_ROUNDS = int(os.getenv("RETRY_ROUNDS", "3"))
RETRY_GAP = int(os.getenv("RETRY_GAP_SEC", "180"))
ACCOUNT_RUNNING = set()
AUTO_STATE = {"next_at": 0}


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("CREDS_KEY", "").strip()
    if not key:
        key = _b64.urlsafe_b64encode(hashlib.sha256(f"creds:{TG_BOT_TOKEN}".encode()).digest()).decode()
    return Fernet(key.encode())


def auto_db_init():
    db_exec("""CREATE TABLE IF NOT EXISTS account_creds (
        username TEXT PRIMARY KEY, password_enc TEXT NOT NULL, notify_chat BIGINT)""")
    db_exec("""CREATE TABLE IF NOT EXISTS jobs (
        account TEXT PRIMARY KEY, channel_id BIGINT, status TEXT, current_batch TEXT,
        done BIGINT DEFAULT 0, total BIGINT DEFAULT 0, updated_at TEXT)""")


def save_creds(username, password, notify_chat):
    enc = _fernet().encrypt(password.encode()).decode()
    db_exec("INSERT INTO account_creds (username, password_enc, notify_chat) VALUES (%s,%s,%s) "
            "ON CONFLICT (username) DO UPDATE SET password_enc=EXCLUDED.password_enc, notify_chat=EXCLUDED.notify_chat",
            (username, enc, int(notify_chat)))


def load_creds(username):
    rows = db_exec("SELECT password_enc, notify_chat FROM account_creds WHERE username=%s", (username,), fetch=True)
    if not rows:
        return None, None
    return _fernet().decrypt(rows[0][0].encode()).decode(), rows[0][1]


def chat_creds(chat_id):
    """Channel ka saved Allen login (auto re-login ke liye)."""
    try:
        rows = db_exec("SELECT username FROM accounts WHERE channel_id=%s", (int(chat_id),), fetch=True)
        if rows:
            pw, _ = load_creds(rows[0][0])
            if pw:
                return rows[0][0], pw
    except Exception as e:
        logger.warning(f"chat_creds failed: {e}")
    return None, None


def relogin_for_chat(chat_id):
    u, p = chat_creds(chat_id) if chat_id is not None else (None, None)
    if u and p:
        return allen_login_idpass(u, p, chat_id)
    if ALLEN_USERNAME and ALLEN_PASSWORD:
        return allen_login_idpass(ALLEN_USERNAME, ALLEN_PASSWORD, chat_id)
    return None


def job_set(account, **kw):
    try:
        cols = dict(kw, updated_at=time.strftime("%Y-%m-%d %H:%M"))
        db_exec("INSERT INTO jobs (account) VALUES (%s) ON CONFLICT (account) DO NOTHING", (account,))
        sets = ", ".join(f"{k}=%s" for k in cols)
        db_exec(f"UPDATE jobs SET {sets} WHERE account=%s", tuple(cols.values()) + (account,))
    except Exception as e:
        logger.warning(f"job_set failed: {e}")


class _Notifier:
    """run_batch_job ko message jaisa object chahiye; ye DM/owner ko bhejta hai."""
    def __init__(self, chat_id):
        self.chat = type("C", (), {"id": chat_id})()
        self.from_user = None

    async def reply_text(self, text, **kw):
        return await app.send_message(self.chat.id, text, **kw)


def _account_batches(info):
    out = []
    for c in info.get("course_details") or []:
        for b in c.get("enrolled_batches") or []:
            if b not in out:
                out.append(b)
    return out


async def run_account_job(username, chan_id, notify_chat, reason="manual"):
    if username in ACCOUNT_RUNNING:
        return
    ACCOUNT_RUNNING.add(username)
    notify = _Notifier(notify_chat or OWNER_ID)
    try:
        pw, _ = await asyncio.to_thread(load_creds, username)
        if not pw:
            await notify.reply_text(f"<blockquote><i>⚠️ <code>{username}</code> ka password saved nahi. Ek baar /login karo.</i></blockquote>")
            return
        try:
            token = await asyncio.to_thread(allen_login_idpass, username, pw, chan_id)
        except Exception as e:
            await asyncio.to_thread(job_set, username, status="login_failed")
            await notify.reply_text(f"<blockquote><i>❌ <code>{username}</code> login fail (password badla?): <code>{html.escape(str(e))}</code></i></blockquote>")
            return
        info = await asyncio.to_thread(fetch_student_info, token, chan_id)
        batches = _account_batches(info) or [None]
        await asyncio.to_thread(job_set, username, channel_id=int(chan_id), status="running")
        report = []
        new_total = 0
        for b in batches:
            await asyncio.to_thread(job_set, username, current_batch=str(b))
            last = None
            first_total = 0
            for rnd in range(RETRY_ROUNDS + 2):
                key = _job_key(chan_id, b, None)
                if ACTIVE_JOBS.get(key, {}).get("running"):
                    break
                st = await app.send_message(notify.chat.id, f"<blockquote><i>🔄 {b or 'Batch'} — round {rnd + 1}</i></blockquote>",
                                            disable_notification=True)
                res = {}
                await run_batch_job(notify, token, b, None, chan_id, st, quiet=True, result=res)
                try:
                    await st.delete()
                except Exception:
                    pass
                if res.get("stopped"):
                    await asyncio.to_thread(job_set, username, status="stopped")
                    return
                if rnd == 0:
                    first_total = res.get("pending", 0)
                last = res
                if not res.get("pending") or not res.get("failed"):
                    break  # sab upload ho gaya (ya kuch pending hi nahi tha)
                await asyncio.sleep(RETRY_GAP)
                token = await asyncio.to_thread(relogin_for_chat, chan_id) or token
            new_total += first_total
            if last is not None:
                all_n = len(last.get("items") or [])
                miss = last.get("failed_titles") or []
                line = f"• <code>{b}</code>: {all_n - len(miss)}/{all_n} channel me"
                if miss:
                    line += "\n   ❗ Nahi aaye: " + html.escape(", ".join(miss[:15]))[:900]
                report.append(line)
        await asyncio.to_thread(job_set, username, status="idle")
        if reason == "manual" or new_total:
            await notify.reply_text(("<b>📊 Final report</b> (" + html.escape(username) + ")\n" + "\n".join(report))[:4000])
    except Exception as e:
        logger.error(f"Account job failed {username}: {e}")
        await asyncio.to_thread(job_set, username, status="error")
        try:
            await notify.reply_text(f"<blockquote><i>❌ Auto job error: <code>{html.escape(str(e))}</code> — next auto-check pe dobara try hoga.</i></blockquote>")
        except Exception:
            pass
    finally:
        ACCOUNT_RUNNING.discard(username)


async def auto_loop():
    while True:
        AUTO_STATE["next_at"] = time.time() + AUTO_INTERVAL
        await asyncio.sleep(AUTO_INTERVAL)
        if (await asyncio.to_thread(kv_get, "auto_paused")) == "1":
            continue
        try:
            rows = await asyncio.to_thread(db_exec,
                                           "SELECT a.username, a.channel_id, c.notify_chat FROM accounts a "
                                           "JOIN account_creds c ON c.username=a.username", (), True)
            for u, cid, nc in rows or []:
                await run_account_job(u, int(cid), nc, reason="auto")
        except Exception as e:
            logger.error(f"Auto loop failed: {e}")


async def resume_jobs():
    try:
        rows = await asyncio.to_thread(db_exec,
                                       "SELECT j.account, j.channel_id, c.notify_chat FROM jobs j "
                                       "JOIN account_creds c ON c.username=j.account WHERE j.status='running'", (), True)
        for u, cid, nc in rows or []:
            logger.info(f"Resuming job for {u}")
            try:
                await app.send_message(nc or OWNER_ID, f"<blockquote><i>♻️ Restart ke baad <code>{html.escape(u)}</code> ka upload wahin se continue ho raha hai.</i></blockquote>")
            except Exception:
                pass
            asyncio.create_task(run_account_job(u, int(cid), nc, reason="manual"))
    except Exception as e:
        logger.error(f"Resume failed: {e}")


@app.on_message(filters.command(["pauseauto", "resumeauto"]) & filters.private)
async def handle_pauseauto(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    pause = message.text.lower().startswith("/pause")
    await asyncio.to_thread(kv_set, "auto_paused", "1" if pause else "0")
    await message.reply_text(f"<blockquote><i>{'⏸ Auto-check band.' if pause else '▶️ Auto-check chalu.'}</i></blockquote>")


@app.on_message(filters.command("autostatus") & filters.private)
async def handle_autostatus(client, message: Message):
    rows = await asyncio.to_thread(db_exec, "SELECT account, channel_id, status, current_batch, updated_at FROM jobs", (), True)
    nxt = AUTO_STATE.get("next_at") or 0
    out = [f"<b>⚙️ Auto jobs</b> — next check: {time.strftime('%d-%m %H:%M', time.localtime(nxt)) if nxt else '?'}"]
    for a, cid, st, cb, up in rows or []:
        out.append(f"• <code>{html.escape(str(a))}</code> → <code>{cid}</code> | {st} | {cb or '-'} | {up}")
    await message.reply_text("\n".join(out)[:4000])


@app.on_message(filters.command("resetall") & filters.private)
async def handle_resetall(client, message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].upper() != "CONFIRM":
        await message.reply_text(
            "<blockquote><i>⚠️ <b>RESET ALL</b>\n\n"
            "Ye command:\n"
            "• Saare auto-bane channels <b>delete</b> kar dega (lectures bhi)\n"
            "• Saare accounts, catalog, jobs, sessions, thumbnails ka data <b>wipe</b> kar dega\n"
            "• Chal rahe uploads <b>rok</b> dega\n\n"
            "Ye wapas nahi hoga. Pakka karna hai toh bhejo:\n"
            "<code>/resetall CONFIRM</code></i></blockquote>")
        return
    # 1) chal rahe jobs roko
    for k, v in list(ACTIVE_JOBS.items()):
        v["running"] = False
        if v.get("work_dir"):
            try:
                kill_job_processes(v["work_dir"])
            except Exception:
                pass
        for t in v.get("tasks") or []:
            if not t.done():
                t.cancel()
    ACTIVE_JOBS.clear()
    ACCOUNT_RUNNING.clear()
    await message.reply_text("<blockquote><i>🧹 Reset shuru... channels delete ho rahe hain.</i></blockquote>")
    # 2) channels delete (helper ne banaye the, wahi delete kar sakta hai)
    chan_ids = set()
    try:
        for r in await asyncio.to_thread(db_exec, "SELECT DISTINCT channel_id FROM accounts", (), True) or []:
            chan_ids.add(int(r[0]))
        for r in await asyncio.to_thread(db_exec, "SELECT DISTINCT channel_id FROM catalog", (), True) or []:
            chan_ids.add(int(r[0]))
    except Exception as e:
        logger.warning(f"resetall channel list: {e}")
    helper = await get_helper()
    deleted, failed = 0, 0
    for cid in chan_ids:
        ok = False
        if helper:
            try:
                await helper.delete_channel(cid)
                ok = True
            except Exception as e:
                logger.warning(f"delete_channel {cid}: {e}")
                try:
                    async for _ in helper.get_dialogs():
                        pass
                    await helper.delete_channel(cid)
                    ok = True
                except Exception as e2:
                    logger.warning(f"delete_channel retry {cid}: {e2}")
        if not ok:
            try:
                await app.leave_chat(cid)
                ok = True
            except Exception as e:
                logger.warning(f"leave_chat {cid}: {e}")
        if ok:
            deleted += 1
        else:
            failed += 1
        await asyncio.sleep(2)
    # 3) DB wipe (user_session = helper login, use bacha ke rakho)
    try:
        for tbl in ("accounts", "catalog", "jobs", "account_creds", "library_users"):
            await asyncio.to_thread(db_exec, f"DELETE FROM {tbl}")
        await asyncio.to_thread(db_exec, "DELETE FROM kv WHERE k <> %s", ("user_session",))
    except Exception as e:
        logger.warning(f"resetall db wipe: {e}")
    # 4) local files wipe
    for f in (SESSION_FILE, DONE_FILE, STATE_MSG_FILE, THUMB_FILE):
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass
    try:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception:
        pass
    await message.reply_text(
        f"<blockquote><i>✅ <b>Reset complete.</b>\n\n"
        f"• Channels delete: <b>{deleted}</b>\n"
        f"• Delete na ho sake: <b>{failed}</b> (unhe Telegram me manually delete karo)\n"
        f"• Saara data wipe ho gaya\n\n"
        f"Ab fresh start: <code>/login user*pass</code></i></blockquote>")


# ======================= VORA CLASSES (Appx) =======================
import vora as _vora

VORA_CLIENTS = {}          # chat_id -> Vora client (cached, avoids re-login = device-limit block)
VORA_LOCK = asyncio.Lock()


def _vs_load():
    try:
        return json.loads(kv_get("vora_sessions") or "{}")
    except Exception:
        try:
            with open("vora_sessions.json") as f:
                return json.load(f)
        except Exception:
            return {}


def _vs_save(d):
    raw = json.dumps(d)
    try:
        kv_set("vora_sessions", raw)
    except Exception:
        pass
    try:
        with open("vora_sessions.json", "w") as f:
            f.write(raw)
    except Exception:
        pass


def _vs_get(chat_id):
    d = _vs_load()
    return d.get(str(chat_id)) or d.get("default")


def vora_login(chat_id, email, password, force=False):
    # Pehle saved login try karo -> naya device login nahi = block nahi
    rec = _vs_get(chat_id)
    if not force and rec and rec.get("email") == email:
        try:
            v = _vora.Vora({"userid": rec["userid"], "token": rec["token"]})
            v.courses()
            d = _vs_load(); d[str(chat_id)] = rec; _vs_save(d)
            VORA_CLIENTS[str(chat_id)] = v
            return {"userid": rec["userid"], "token": rec["token"], "name": rec.get("name", "")}
        except Exception as e:
            logger.info(f"Saved Vora token not usable, fresh login: {e}")
    v = _vora.Vora()
    info = v.login(email, password)
    d = _vs_load()
    rec = {"userid": info["userid"], "token": info["token"], "name": info.get("name", ""),
           "email": email, "pw": _fernet().encrypt(password.encode()).decode()}
    d[str(chat_id)] = rec
    d.setdefault("default", rec)
    _vs_save(d)
    VORA_CLIENTS[str(chat_id)] = v
    return info


def vora_client(chat_id, fresh=False):
    k = str(chat_id)
    if not fresh and k in VORA_CLIENTS:
        return VORA_CLIENTS[k]
    rec = _vs_get(chat_id)
    if not rec:
        raise RuntimeError("Vora login nahi hai. Pehle /vlogin email:password bhejo.")
    if fresh:
        pw = _fernet().decrypt(rec["pw"].encode()).decode()
        vora_login(chat_id, rec["email"], pw, force=True)
        return VORA_CLIENTS[k]
    v = _vora.Vora({"userid": rec["userid"], "token": rec["token"]})
    VORA_CLIENTS[k] = v
    return v


VORA_LOGIN_GAP = int(os.getenv("VORA_LOGIN_GAP", "900"))   # 15 min me max 1 re-login (device-limit block se bachne ke liye)
VORA_BLOCK_WAIT = int(os.getenv("VORA_BLOCK_WAIT", "660"))
_VORA_RELOGIN = {"lock": __import__("threading").Lock(), "at": {}, "blocked_until": {}}


def _vora_wait_block(chat_id):
    until = _VORA_RELOGIN["blocked_until"].get(str(chat_id), 0)
    if until > time.time():
        time.sleep(until - time.time())


def _vora_call(chat_id, fn):
    """Saved token use karo. Re-login SIRF jab token expire ho (aur 15 min me ek hi baar).
    429 / segment errors par kabhi login nahi -> 'too many devices' block nahi lagega.
    Block lag gaya toh sab downloads 11 min ruk kar wahin se continue."""
    k = str(chat_id)
    for attempt in range(3):
        _vora_wait_block(chat_id)
        try:
            return fn(vora_client(chat_id))
        except _vora.VoraBlocked as e:
            logger.warning(f"Vora account blocked, {VORA_BLOCK_WAIT}s wait: {e}")
            _VORA_RELOGIN["blocked_until"][k] = time.time() + VORA_BLOCK_WAIT
        except _vora.VoraAuthError as e:
            with _VORA_RELOGIN["lock"]:
                last = _VORA_RELOGIN["at"].get(k, 0)
                if time.time() - last > VORA_LOGIN_GAP:
                    logger.warning(f"Vora token expired, re-login once: {e}")
                    _VORA_RELOGIN["at"][k] = time.time()
                    try:
                        vora_client(chat_id, fresh=True)
                    except _vora.VoraBlocked as be:
                        _VORA_RELOGIN["blocked_until"][k] = time.time() + VORA_BLOCK_WAIT
                        logger.warning(f"Vora re-login blocked: {be}")
                else:
                    VORA_CLIENTS.pop(k, None)   # dusre thread ne abhi login kiya hai -> naya token lo
    return fn(vora_client(chat_id))


def vora_fetch_items(batch_id, chat_id):
    cid = str(batch_id).split(":", 1)[1]
    courses = _vora_call(chat_id, lambda v: v.courses())
    name = next((c["name"] for c in courses if c["id"] == cid), f"Vora Course {cid}")
    items = _vora_call(chat_id, lambda v: v.bot_items(cid, name))
    logger.info(f"Vora course {cid} ({name}): {len(items)} items")
    return items


def _fetch_items(batch_id, token, subject, chat_id):
    if str(batch_id or "").startswith("vora:"):
        return vora_fetch_items(batch_id, chat_id)
    return fetch_batch_contents(batch_id, token, subject, chat_id)


VORA_PARALLEL = int(os.getenv("VORA_PARALLEL", "4"))
_VORA_SEM = {"sem": None}


def _vora_sem():
    if _VORA_SEM["sem"] is None:
        _VORA_SEM["sem"] = asyncio.Semaphore(VORA_PARALLEL)
    return _VORA_SEM["sem"]


async def vora_download(url, clean, work_dir, chat_id):
    course_id, video_id = url[len("vora://"):].split("/", 1)
    out = os.path.join(work_dir, clean + ".mp4")
    threads = max(4, int(DL_THREADS))
    async with _vora_sem():              # Vora ka server zyada parallel par 429 deta hai
        async with _global_dl_sem():
            r = await asyncio.to_thread(
                _vora_call, chat_id, lambda v: v.download_video(course_id, video_id, out, threads=threads))
    logger.info(f"Vora video {video_id}: {r.get('quality')} {r.get('segments')} segments")
    gc.collect()
    if not os.path.exists(out) or os.path.getsize(out) < 1024:
        raise RuntimeError("Vora video empty")
    return out


def _target_from(parts, default):
    if "-c" in parts:
        i = parts.index("-c")
        tgt = int(parts[i + 1])
        return tgt, parts[:i] + parts[i + 2:]
    return default, parts


@app.on_message(filters.command("vlogin") & (filters.group | filters.channel | filters.private))
async def handle_vlogin(client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    try:
        tgt, parts = _target_from((message.text or "").split()[1:], message.chat.id)
    except Exception:
        await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
        return
    raw = " ".join(parts).strip()
    m = re.match(r"^(\S+?)[:*](\S+)$", raw)
    _schedule_delete(message.chat.id, message.id, 5)
    if not m:
        await message.reply_text("<blockquote><i>Use: <code>/vlogin email:password</code> [-c channel_id]</i></blockquote>")
        return
    st = await message.reply_text("<blockquote><i>🔐 Vora login ho raha hai...</i></blockquote>")
    try:
        info = await asyncio.to_thread(vora_login, tgt, m.group(1), m.group(2))
        courses = await asyncio.to_thread(_vora_call, tgt, lambda v: v.courses())
    except Exception as e:
        await st.edit_text(f"<blockquote><i>❌ Vora login fail: <code>{html.escape(str(e))}</code></i></blockquote>")
        return
    lines = [f"✅ <b>Vora login ho gaya</b> — {html.escape(info.get('name') or '')}",
             f"Channel: <code>{tgt}</code>", "", "<b>Batches:</b>"]
    lines += [f"<code>{c['id']}</code> — {html.escape(c['name'])}" for c in courses[:60]]
    lines += ["", f"Upload: <code>/vbatch &lt;ID&gt; -c {tgt}</code>  ya sab: <code>/vbatch all -c {tgt}</code>"]
    await st.edit_text("\n".join(lines)[:MAX_TG_MSG_LEN])


@app.on_message(filters.command("vbatches") & (filters.group | filters.channel | filters.private))
async def handle_vbatches(client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        return
    try:
        tgt, _ = _target_from((message.text or "").split()[1:], message.chat.id)
        courses = await asyncio.to_thread(_vora_call, tgt, lambda v: v.courses())
    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <code>{html.escape(str(e))}</code></i></blockquote>")
        return
    txt = "<b>📗 Vora batches:</b>\n" + "\n".join(
        f"<code>{c['id']}</code> — {html.escape(c['name'])}" for c in courses[:80])
    await message.reply_text(txt[:MAX_TG_MSG_LEN] or "Koi batch nahi mila.")


async def _vora_run_many(message, ids, tgt):
    for cid in ids:
        if SHUTTING_DOWN["v"]:
            return
        bid = f"vora:{cid}"
        if ACTIVE_JOBS.get(_job_key(tgt, bid, None), {}).get("running"):
            continue
        st = await message.reply_text(f"<blockquote><i>🔄 <b>Vora fetching:</b> {cid}\nUpload → <code>{tgt}</code></i></blockquote>")
        try:
            await _tracked_batch(message, "", bid, None, tgt, st)
        except Exception as e:
            logger.error(f"Vora batch {cid} failed: {e}")
            await message.reply_text(f"<blockquote><i>❌ Vora batch {cid}: <code>{html.escape(str(e))}</code></i></blockquote>")


@app.on_message(filters.command("vbatch") & (filters.group | filters.channel | filters.private))
async def handle_vbatch(client, message: Message):
    if message.from_user and not is_user_authorized(message.from_user.id):
        await message.reply_text("<blockquote><i>🚫 Access Denied.</i></blockquote>")
        return
    try:
        tgt, parts = _target_from((message.text or "").split()[1:], message.chat.id)
    except Exception:
        await message.reply_text("<blockquote><i>⚠️ <code>-c</code> ke baad valid channel ID do.</i></blockquote>")
        return
    if not parts:
        await message.reply_text("<blockquote><i>Use: <code>/vbatch &lt;ID&gt;</code> ya <code>/vbatch all</code> [-c channel_id]\nIDs: /vbatches</i></blockquote>")
        return
    try:
        if parts[0].lower() in ("all", "sab", "*"):
            ids = [c["id"] for c in await asyncio.to_thread(_vora_call, tgt, lambda v: v.courses())]
        else:
            ids = [p.strip(",") for p in parts if p.strip(",").isdigit()]
    except Exception as e:
        await message.reply_text(f"<blockquote><i>❌ <code>{html.escape(str(e))}</code></i></blockquote>")
        return
    if not ids:
        await message.reply_text("<blockquote><i>⚠️ Batch ID number hona chahiye. /vbatches dekho.</i></blockquote>")
        return
    if not get_channel_thumb(tgt):
        pend = {"event": asyncio.Event(), "file_id": None,
                "user_id": message.from_user.id if message.from_user else None}
        PENDING_THUMB[message.chat.id] = pend
        ask = await message.reply_text(
            "<blockquote><i>🖼 <b>Custom thumbnail bhejo</b> (photo) ya <code>/skip</code> (90 sec)</i></blockquote>")
        try:
            await asyncio.wait_for(pend["event"].wait(), timeout=90)
        except asyncio.TimeoutError:
            pass
        PENDING_THUMB.pop(message.chat.id, None)
        try:
            await ask.delete()
        except Exception:
            pass
        if pend["file_id"]:
            set_channel_thumb(tgt, pend["file_id"])
    await message.reply_text(f"<blockquote><i>📗 Vora: {len(ids)} batch queue me — ek-ek karke upload honge.</i></blockquote>")
    asyncio.create_task(_vora_run_many(message, ids, tgt))


PANEL_TEXT = ("<b>Courier Well Uploader</b>\n\nPlatform chuno 👇")
PANEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🅰️ Allen", callback_data="Pallen"),
     InlineKeyboardButton("📗 Vora Classes", callback_data="Pvora")],
    [InlineKeyboardButton("📚 Library", callback_data="Plib")]])
_PANEL_HELP = {
    "allen": ("<b>🅰️ Allen</b>\n\n<code>/login user*pass</code> [-c channel_id]\n<code>/mybatches</code>\n"
              "<code>/batch &lt;ID&gt; all -c &lt;channel_id&gt;</code>\n<code>/downloadall</code>\n<code>/jobs</code> · <code>/stop</code>"),
    "vora": ("<b>📗 Vora Classes</b>\n\n<code>/vlogin email:password</code> [-c channel_id]\n<code>/vbatches</code>\n"
             "<code>/vbatch &lt;ID&gt; -c &lt;channel_id&gt;</code>\n<code>/vbatch all -c &lt;channel_id&gt;</code>\n<code>/jobs</code> · <code>/stop</code>"),
}


@app.on_callback_query(filters.regex(r"^P(allen|vora|lib|back)$"))
async def handle_panel_cb(client, cq: CallbackQuery):
    if not is_user_authorized(cq.from_user.id):
        await cq.answer("Access nahi hai", show_alert=True)
        return
    what = cq.data[1:]
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="Pback")]])
    if what == "back":
        await cq.message.edit_text(PANEL_TEXT, reply_markup=PANEL_KB)
    elif what == "lib":
        text, kb = await lib_batches(cq.from_user.id)
        await cq.message.edit_text(text, reply_markup=kb)
    else:
        await cq.message.edit_text(_PANEL_HELP[what], reply_markup=back)
    await cq.answer()


async def _boot():
    await app.start()
    logger.info("Bot started")
    await start_library_bot()
    asyncio.create_task(resume_jobs())
    asyncio.create_task(resume_manual_jobs())
    asyncio.create_task(auto_loop())
    await idle()
    SHUTTING_DOWN["v"] = True  # redeploy: jobs ko "running" hi rehne do taaki wapas chalu ho
    if LIB.get("client"):
        try:
            await LIB["client"].stop()
        except Exception:
            pass
    await app.stop()



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
    try:
        db_init()
        auto_db_init()
        logger.info("Library DB ready (%s)", "postgres" if DATABASE_URL else "sqlite - restart pe reset hoga")
    except Exception as e:
        logger.error("Library DB init failed: %s", e)
    logger.info("Workspace clean. Booting Pyrogram engine...")
    app.run(_boot())

if __name__ == "__main__":
    main()
