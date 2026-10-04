"""Vora Classes (Appx / classx) downloader: login, courses, folder tree, video unlock + download."""
import base64, hashlib, json, os, re, subprocess, time, concurrent.futures as cf
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VORA_HOST = "https://voraclassesapi.classx.co.in"
VORA_REFERER = "https://voraclasses.classx.co.in/"
_LINK_KEY = b"638udh3829162018"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _aes_cbc(key, iv, data, unpad=True):
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    p = d.update(data) + d.finalize()
    return p[:-p[-1]] if unpad and p and p[-1] <= 16 else p


def decrypt_link(s):
    """Appx 'ciphertext:base64iv' link -> plain text."""
    if not s or ":" not in s:
        return s or ""
    ct, iv = s.split(":", 1)
    return _aes_cbc(_LINK_KEY, base64.b64decode(iv), base64.b64decode(ct)).decode(errors="replace")


class Vora:
    def __init__(self, session=None):
        self.s = requests.Session()
        self.s.headers.update({"Client-Service": "Appx", "Auth-Key": "appxapi", "source": "website", "User-Agent": UA})
        self.userid = self.token = None
        if session:
            self.userid, self.token = session["userid"], session["token"]
            self._auth()

    def _auth(self):
        self.s.headers.update({"User-ID": str(self.userid), "Authorization": self.token})

    def login(self, email, password):
        r = self.s.post(f"{VORA_HOST}/post/userLogin", data={"email": email, "password": password}, timeout=30).json()
        if r.get("status") != 200:
            raise RuntimeError(r.get("message") or "Vora login failed")
        d = r["data"]
        self.userid, self.token = d["userid"], d["token"]
        self._auth()
        return {"userid": self.userid, "token": self.token, "name": d.get("name", "")}

    def get(self, path):
        r = self.s.get(f"{VORA_HOST}/{path}", timeout=40)
        r.raise_for_status()
        return r.json()

    def courses(self):
        out = []
        for it in self.get(f"get/get_all_purchasesv2?userid={self.userid}&start=0").get("data") or []:
            if str(it.get("itemtype")) == "10" or not it.get("coursedt"):
                continue
            c = it["coursedt"][0]
            out.append({"id": str(c.get("id") or it.get("itemid")), "name": c.get("course_name", "")})
        return out

    def folder(self, course_id, parent_id=-1):
        items, start = [], 0
        while True:
            d = self.get(f"get/folder_contentsv3?course_id={course_id}&parent_id={parent_id}&start={start}&userid={self.userid}").get("data") or []
            items += d
            if len(d) < 20:
                return items
            start += len(d)

    def walk(self, course_id, parent_id=-1, path=()):
        """Yield (path_tuple, item) for every VIDEO / PDF under the folder tree, in order."""
        for it in self.folder(course_id, parent_id):
            t = it.get("material_type")
            if t == "FOLDER":
                yield from self.walk(course_id, it["id"], path + (it.get("Title", ""),))
            elif t in ("VIDEO", "PDF"):
                yield path, it

    # ---------- video ----------
    def _player(self, course_id, video_id):
        v = self.get(f"get/fetchVideoDetailsById?course_id={course_id}&video_id={video_id}&ytflag=0&folder_wise_course=1")["data"]
        hdr = {"User-Agent": UA, "Referer": VORA_REFERER, "Origin": VORA_REFERER.rstrip("/"),
               "Cookie": f"appxplayer={v.get('cookie_value', '')}"}
        base = v.get("download_url_lower_version") or "https://appx-play.classx.co.in/combined-img-player?isMobile=true&videoPlayer=hls&token="
        page = requests.get(base + v["video_player_token"], headers=hdr, timeout=40).text.replace('\\"', '"')
        dt = re.search(r'"datetime":"(\d+)"', page).group(1)
        tok = re.search(r'"token":"([0-9a-f]+)"', page).group(1)
        iv = base64.b64decode(re.search(r'"ivb6":"([^"]+)"', page).group(1))
        m = re.search(r'"kstr":"([^"]+)","jstr":"([^"]+)"', page)
        n4 = dt[-4:]
        k = hashlib.sha256((dt + tok[int(n4[0]):int(n4[1:3])]).encode()).digest()
        k = k[:16] if n4[3] == "6" else k[:24] if n4[3] == "7" else k
        aes_key = base64.b64decode(_aes_cbc(k, iv, base64.b64decode(m.group(1))))
        manifest = _aes_cbc(k, iv, base64.b64decode(m.group(2))).decode()
        return v, hdr, aes_key, manifest

    _B64 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r")
    _MASKS = (lambda o: o - 20, lambda o: (o >> 3) ^ 42)   # .tsa style, .tsb style

    @staticmethod
    def _unmask(t):
        """Appx segment text masks differ per video; pick the one that yields base64."""
        sample = t[:4000]
        for f in Vora._MASKS:
            try:
                if all(chr(f(ord(c))) in Vora._B64 for c in sample):
                    return "".join(chr(f(ord(c))) for c in t).replace("\n", "").replace("\r", "")
            except (ValueError, OverflowError):
                continue
        raise ValueError("Unknown Vora segment format")

    @staticmethod
    def _seg(raw, key, iv):
        b = base64.b64decode(Vora._unmask(raw.decode()))
        d = bytearray(_aes_cbc(key, iv, b))
        for i in range(0, len(d) - 187, 188):           # undo per-packet inversion
            if d[i] != 0x47:
                continue
            off = i + 5 + d[i + 4] if (d[i + 3] & 0x30) >> 4 > 1 else i + 4
            if i + 188 - off == 184:
                for j in range(off, off + 94):
                    d[j] ^= 0xFF
        return d

    @staticmethod
    def _fix_ts(d, st=None):
        st = st if st is not None else {}
        def rd(o): return ((d[o] & 0x0e) << 29) | (d[o+1] << 22) | ((d[o+2] & 0xfe) << 14) | (d[o+3] << 7) | (d[o+4] >> 1)
        def wr(o, v, pre):
            d[o] = (pre << 4) | (((v >> 30) & 7) << 1) | 1; d[o+1] = (v >> 22) & 0xff
            d[o+2] = (((v >> 15) & 0x7f) << 1) | 1; d[o+3] = (v >> 7) & 0xff; d[o+4] = ((v & 0x7f) << 1) | 1
        def js(v):
            x = (((v ^ 0xFF674FF) & 0xffffffff) << 2) & 0xffffffff
            return (x - (1 << 32) if x & 0x80000000 else x) & ((1 << 33) - 1)
        for i in range(0, len(d) - 187, 188):
            if d[i] != 0x47 or not d[i + 1] & 0x40:
                continue
            off = i + 5 + d[i + 4] if (d[i + 3] & 0x30) >> 4 > 1 else i + 4
            if d[off:off + 3] != b"\0\0\1" or not 0xc0 <= d[off + 3] < 0xf0 or not d[off + 7] & 0x80:
                continue
            fl = d[off + 7]; pts = rd(off + 9); dts = rd(off + 14) if fl & 0x40 else pts
            if st.get("flg") is None:
                st["flg"] = pts > 100000000 or dts > 100000000
            if not st["flg"]:
                return d
            wr(off + 9, js(pts), 3 if fl & 0x40 else 2)
            if fl & 0x40:
                wr(off + 14, js(dts), 1)
        return d

    def download_video(self, course_id, video_id, out_mp4, threads=16, progress=None):
        v, hdr, key, man = self._player(course_id, video_id)
        iv = bytes.fromhex(re.search(r"IV=0x([0-9a-fA-F]+)", man).group(1))
        segs = [l for l in man.splitlines() if l and not l.startswith("#")]
        sess = requests.Session(); sess.headers.update(hdr)
        segs, quality = self._best_quality(sess, segs)

        def fetch(u):
            for _ in range(4):
                try:
                    r = sess.get(u, timeout=60); r.raise_for_status()
                    return self._seg(r.content, key, iv)
                except Exception as e:
                    err = e
            raise err
        ts = out_mp4 + ".ts"
        with open(ts, "wb") as f, cf.ThreadPoolExecutor(threads) as ex:
            st = {}
            for n, part in enumerate(ex.map(fetch, segs), 1):   # streamed: low RAM even for 3h lectures
                f.write(self._fix_ts(part, st))
                if progress:
                    progress(n, len(segs))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", ts, "-c", "copy", "-movflags", "+faststart", out_mp4], check=True)
        os.remove(ts)
        return {"title": v.get("Title", ""), "segments": len(segs), "path": out_mp4, "quality": quality}

    @staticmethod
    def _best_quality(sess, segs):
        """Vora stores renditions side by side (/480p/, /720p/...); the signed token covers
        the whole folder, so use the highest one that really exists."""
        m = re.search(r"/(\d{3,4})p/", segs[0]) if segs else None
        if not m:
            return segs, "?"
        cur = int(m.group(1))
        for q in (1080, 720):
            if q <= cur:
                break
            test = segs[0].replace(f"/{cur}p/", f"/{q}p/")
            try:
                if sess.get(test, timeout=30, stream=True).status_code == 200:
                    return [x.replace(f"/{cur}p/", f"/{q}p/") for x in segs], f"{q}p"
            except Exception:
                pass
        return segs, f"{cur}p"

    # ---------- bot items ----------
    @staticmethod
    def pdf_url(it):
        for k in ("pdf_link", "pdf_link2", "file_link", "download_link"):
            u = it.get(k)
            if not u:
                continue
            try:
                u = decrypt_link(u) if not u.startswith("http") else u
            except Exception:
                continue
            u = u.strip()
            if u.startswith("http"):
                return u
        return ""

    def bot_items(self, course_id, course_name):
        """Folder tree -> bot item dicts (subject / chapter / section)."""
        out, now = [], time.time()
        subj_words = ("phys", "chem", "math", "bio", "botany", "zoology", "english", "science")
        for path, it in self.walk(course_id):
            path = tuple(p for p in path if p.strip().lower() not in ("home", "main", "root", "content"))
            idx = next((n for n, p in enumerate(path) if any(w in p.lower() for w in subj_words)), 0 if path else None)
            subj = path[idx].strip() if idx is not None else "General"
            prefix = " - ".join(path[:idx]) if idx else ""
            rest = path[idx + 1:] if idx is not None else ()
            topic = " - ".join(rest) or subj
            is_vid = it.get("material_type") == "VIDEO"
            try:
                ts = int(it.get("strtotime") or 0)
            except Exception:
                ts = 0
            if is_vid and ts and ts > now:          # upcoming live class: not recorded yet
                continue
            url = f"vora://{course_id}/{it['id']}" if is_vid else self.pdf_url(it)
            if not url:
                continue
            date = time.strftime("%d-%m-%Y", time.localtime(ts)) if ts else ""
            out.append({"id": f"vora:{it['id']}", "title": it.get("Title") or f"Item {it['id']}",
                        "url": url, "batch": course_name, "subject": subj, "topic": topic,
                        "section": ("Notes / PDF" if not is_vid else (prefix or "Lectures")),
                        "kind": "Recorded Lecture", "date": date})
        return out
