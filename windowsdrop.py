"""
WindowsDrop v1.2 - iPhone <-> Windows AirDrop benzeri fotoğraf/video aktarımı.

Çalıştırma:
    pip install -r requirements.txt
    python windowsdrop.py
Başlat menüsüne kısayol (bir kere):
    python windowsdrop.py --install-shortcut        (kaldırmak için --remove-shortcut)

Güvenlik katmanları:
  1. Yalnızca yerel ağ (özel IP) ve bu bilgisayar erişebilir; internet IP'leri reddedilir.
  2. QR'daki anahtar 10 dk sonra geçersiz olur ve bir cihaz onaylanınca yenilenir.
  3. Her yeni cihaz için bilgisayarda "İzin ver / Reddet" onayı ve 4 haneli eşleşme kodu.
  4. Oturum, onaylanan cihazın IP'sine bağlıdır ve 2 saat sonra biter.
  5. Sadece foto/video uzantılarına izin verilir (bilgisayardan telefona gönderirken her dosya türü).
  6. Çok fazla reddedilen/şüpheli deneme yapan IP engellenir.
  7. 15 dakika hareketsizlikte uygulama kendini kapatır (devam eden yükleme varken kapanmaz).
  8. Güvenlik başlıkları (CSP, no-store vb.), Host kontrolü ve yönetim isteklerinde CSRF başlığı.
  9. Güncellemeler yalnızca HTTPS'ten, SHA-256 + Ed25519 imza doğrulamasıyla ve
     bilgisayarda onay verilerek yapılır; eski sürüme geri dönüş reddedilir.

v1.2 ile gelenler: güncelleme imzası, aynı dosyayı tekrar gönderme uyarısı, orijinal çekim
tarihini koruma, bilgisayar -> iPhone yönü, disk alanı kontrolü, kısayol/tepsi simgesi.

Ayarlar:  windowsdrop_config.json (ilk çalıştırmada oluşur, güncellemeler bu dosyaya dokunmaz)
Kayıtlar: Downloads\\WindowsDrop   |   Aktarım günlüğü: <kullanıcı>\\.windowsdrop\\transfers.csv
"""
import base64
import csv
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import qrcode
from flask import Flask, abort, jsonify, redirect, request, send_file
from werkzeug.utils import secure_filename

VERSION = "1.5.0"
FROZEN = bool(getattr(sys, "frozen", False))     # PyInstaller .exe mi?
APP_FILE = Path(sys.executable if FROZEN else __file__).resolve()
BASE_DIR = Path(getattr(sys, "_MEIPASS", APP_FILE.parent))   # simge dosyaları (.exe içinde de)
CONFIG_FILE = APP_FILE.with_name("windowsdrop_config.json")
DATA_DIR = Path.home() / ".windowsdrop"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = DATA_DIR / "transfers.csv"
SEEN_FILE = DATA_DIR / "seen.jsonl"              # daha önce aktarılan dosyalar (ad|boyut)
OUTBOX_DIR = DATA_DIR / "outbox"                 # bilgisayardan iPhone'a gönderilecekler
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

# Güncelleme imzası için Ed25519 açık anahtarı (base64).
# `python make_manifest.py genkey` komutu üretir; çıkan satırı buraya yapıştır.
# Boşsa güncellemeler indirilir ama UYGULANMAZ.
UPDATE_PUBLIC_KEY = ""

# Pencereli (konsolsuz) çalışınca çıktıyı dosyaya yaz
if sys.stdout is None or sys.stderr is None:
    _logf = open(DATA_DIR / "windowsdrop.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _logf
    sys.stderr = sys.stderr or _logf

DEFAULT_CONFIG = {
    "port": 8080,
    "save_dir": "",                 # boşsa İndirilenler\WindowsDrop
    "update_manifest_url": "",      # örn. https://raw.githubusercontent.com/KULLANICI/REPO/main/manifest.json
    "check_updates_on_start": True,
    "min_free_mb": 512,             # aktarım sonrası diskte en az bu kadar boş yer kalmalı
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        else:
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # bozuk ayar dosyası uygulamayı durdurmasın
        print(f"Ayar dosyası okunamadı, varsayılanlar kullanılıyor: {e}")
    return cfg


CONFIG = load_config()
try:
    PORT = int(CONFIG.get("port", 8080))
except (TypeError, ValueError):
    PORT = 8080
SAVE_DIR = Path(CONFIG["save_dir"]) if CONFIG.get("save_dir") else Path.home() / "Downloads" / "WindowsDrop"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

TOKEN_TTL = 600        # QR anahtarı geçerlilik süresi (sn)
PENDING_TTL = 120      # onay bekleme süresi (sn)
SESSION_TTL = 7200     # onaylı oturum süresi (sn)
IDLE_LIMIT = 900       # hareketsizlikte otomatik kapanma (sn)
MAX_PENDING = 5
MAX_DENIALS = 3
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tiff", ".tif", ".dng"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".3gp"}
BROWSER_IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp"}   # tarayıcıda önizlenebilenler
OTHER_EXT = {".aae"}   # iPhone düzenleme bilgisi (fotoğraf/video sayılmaz)
ALLOWED_EXT = PHOTO_EXT | VIDEO_EXT | OTHER_EXT

MAX_OUTBOX = 50
lock = threading.Lock()
log_lock = threading.Lock()
active_uploads = 0
seen: dict = {}                                             # "ad|boyut" -> aktarım tarihi
outbox: dict = {}                                           # id -> {name, path, size}
_tray = {"icon": None}
_token = {"v": secrets.token_urlsafe(8), "t": time.time()}
sessions: dict = {}
denials: dict = {}
stats = {"photo": 0, "video": 0, "other": 0, "bytes": 0}   # uygulama açıldığından beri
hidden_files: set = set()                                    # listeden gizlenen dosyalar (silinmez)
history: list = []                                          # biten aktarım özetleri
last_activity = time.time()
update_state = {"status": "idle", "latest": None, "message": "", "manifest": None}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # büyük videolar için sınır yok


# ---------- yardımcılar ----------
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def rotate_token():
    _token["v"] = secrets.token_urlsafe(8)
    _token["t"] = time.time()


def current_token() -> str:
    if time.time() - _token["t"] > TOKEN_TTL:
        rotate_token()
    return _token["v"]


def phone_url() -> str:
    return f"http://{local_ip()}:{PORT}/?t={current_token()}"


def is_local() -> bool:
    host = request.host.rsplit(":", 1)[0].strip("[]")
    return request.remote_addr in ("127.0.0.1", "::1") and host in ("localhost", "127.0.0.1", "::1")


def unique_path(name: str) -> Path:
    name = secure_filename(name) or "dosya"
    stem, ext = Path(name).stem, Path(name).suffix
    p = SAVE_DIR / name
    i = 1
    while p.exists():
        p = SAVE_DIR / f"{stem}__{i}{ext}"
        i += 1
    return p


def device_name(ua: str) -> str:
    for key in ("iPhone", "iPad", "Android", "Windows", "Macintosh"):
        if key in ua:
            return key
    return ua[:40] or "Bilinmeyen cihaz"


def human_size(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


def new_batch() -> dict:
    return {"photo": 0, "video": 0, "other": 0, "bytes": 0}


def log_transfer(device: str, filename: str, kind: str, size: int):
    try:
        with log_lock:
            new = not LOG_FILE.exists()
            with LOG_FILE.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["tarih_saat", "cihaz", "dosya", "tur", "bayt"])
                w.writerow([datetime.now().isoformat(timespec="seconds"), device, filename, kind, size])
    except OSError:
        pass  # günlük yazılamazsa aktarım etkilenmesin


def get_session(sid: str):
    """Geçerli ve bu IP'ye ait oturumu döndürür; süresi dolmuşsa durumunu günceller."""
    s = sessions.get(sid or "")
    if not s or s["ip"] != request.remote_addr:
        return None
    now = time.time()
    if s["status"] == "pending" and now - s["created"] > PENDING_TTL:
        s["status"] = "expired"
    if s["status"] == "approved" and now - s["approved_at"] > SESSION_TTL:
        s["status"] = "expired"
    return s


def expire_sessions():
    """lock tutulurken çağrılır: süresi dolanları işaretler, eskileri siler."""
    now = time.time()
    for sid, s in list(sessions.items()):
        if s["status"] == "pending" and now - s["created"] > PENDING_TTL:
            s["status"] = "expired"
        elif s["status"] == "approved" and now - s["approved_at"] > SESSION_TTL:
            s["status"] = "expired"
        if s["status"] not in ("pending", "approved") and now - s.get("approved_at", s["created"]) > 3600:
            del sessions[sid]


# ---------- disk alanı ----------
def min_free() -> int:
    try:
        return max(0, int(CONFIG.get("min_free_mb", 512))) * 1048576
    except (TypeError, ValueError):
        return 512 * 1048576


def free_bytes(path: Path = None) -> int:
    return shutil.disk_usage(path or SAVE_DIR).free


def space_ok(need: int, path: Path = None) -> bool:
    return free_bytes(path) - min_free() >= need


# ---------- daha önce aktarılanlar ----------
def seen_key(name: str, size) -> str:
    return f"{secure_filename(str(name or '')).lower()}|{int(size)}"


def load_seen():
    try:
        for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
            try:
                k, at = json.loads(line)
                seen[k] = at
            except (ValueError, TypeError):
                pass
    except OSError:
        pass


def mark_seen(name: str, size: int):
    """lock tutulurken çağrılır."""
    k = seen_key(name, size)
    if k in seen:
        return
    seen[k] = now_str()
    try:
        with SEEN_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps([k, seen[k]], ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------- orijinal çekim tarihi ----------
_EPOCH_MIN = 946684800  # 2000-01-01
_MP4_EPOCH = 2082844800  # 1904 -> 1970 farkı (sn)


def _sane(ts) -> bool:
    return ts is not None and _EPOCH_MIN < ts <= time.time() + 86400


def _exif_ts(ex):
    sub = ex.get_ifd(0x8769)
    raw = sub.get(0x9003) or sub.get(0x9004) or ex.get(0x0132)
    if not raw:
        return None
    return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()


def exif_time(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return _exif_ts(im.getexif())
    except Exception:
        return None


def raw_exif_time(path: Path):
    """HEIC/HEIF gibi Pillow'un açamadığı dosyalarda EXIF bloğunu ham olarak arar."""
    try:
        from PIL import Image
        if path.stat().st_size > 60_000_000:
            return None
        data = path.read_bytes()
        pos = 0
        for _ in range(8):
            i = data.find(b"Exif\x00\x00", pos)
            if i < 0:
                break
            pos = i + 6
            try:
                ex = Image.Exif()
                ex.load(data[i:i + 262144])
                ts = _exif_ts(ex)
                if ts:
                    return ts
            except Exception:
                continue
    except Exception:
        pass
    return None


def mp4_time(path: Path):
    """MOV/MP4 dosyasının 'moov/mvhd' bölümünden oluşturulma zamanını okur."""
    try:
        total = path.stat().st_size
        with path.open("rb") as fh:
            pos = 0
            while pos + 8 <= total:
                fh.seek(pos)
                hdr = fh.read(16)
                size, typ = struct.unpack(">I4s", hdr[:8])
                hlen = 8
                if size == 1:
                    size = struct.unpack(">Q", hdr[8:16])[0]
                    hlen = 16
                elif size == 0:
                    size = total - pos
                if size < hlen:
                    return None
                if typ == b"moov":
                    inner, end = pos + hlen, pos + size
                    while inner + 8 <= end:
                        fh.seek(inner)
                        s2, t2 = struct.unpack(">I4s", fh.read(8))
                        if s2 < 8:
                            return None
                        if t2 == b"mvhd":
                            body = fh.read(12)
                            ct = struct.unpack(">Q", body[4:12])[0] if body[0] == 1 else struct.unpack(">I", body[4:8])[0]
                            return ct - _MP4_EPOCH if ct else None
                        inner += s2
                    return None
                pos += size
    except Exception:
        pass
    return None


def _set_win_ctime(path: Path, ts: float):
    """Windows'ta dosyanın 'Oluşturma tarihi'ni de ayarlar (os.utime bunu yapmaz)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    k32.SetFileTime.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x100, 3, None, 3, 0x80, None)   # FILE_WRITE_ATTRIBUTES
    if h in (None, ctypes.c_void_p(-1).value):
        return
    try:
        t = int(ts * 10_000_000) + 116444736000000000
        ft = wintypes.FILETIME(t & 0xFFFFFFFF, t >> 32)
        k32.SetFileTime(h, ctypes.byref(ft), None, None)
    finally:
        k32.CloseHandle(h)


def apply_capture_time(path: Path, ext: str, client_ms):
    """Çekim tarihini dosya meta verisinden (EXIF / MOV), yoksa telefonun bildirdiği tarihten alır."""
    ts, src = None, None
    if ext in PHOTO_EXT:
        ts = exif_time(path) or raw_exif_time(path)
    elif ext in VIDEO_EXT:
        ts = mp4_time(path)
    if _sane(ts):
        src = "meta"
    else:
        ts = None
        try:
            c = float(client_ms) / 1000.0
            if _sane(c):
                ts, src = c, "telefon"
        except (TypeError, ValueError):
            pass
    if ts is None:
        return None
    try:
        os.utime(path, (ts, ts))
        if os.name == "nt":
            _set_win_ctime(path, ts)
    except Exception:
        return None
    return src


# ---------- bilgisayardan iPhone'a gönderilecekler ----------
def clear_outbox():
    shutil.rmtree(OUTBOX_DIR, ignore_errors=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    outbox.clear()


MIME_FIX = {".heic": "image/heic", ".heif": "image/heif", ".mov": "video/quicktime",
            ".m4v": "video/x-m4v", ".dng": "image/x-adobe-dng", ".3gp": "video/3gpp"}


# ---------- güncelleme ----------
class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "HTTPS dışı yönlendirme reddedildi", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnly)


def https_get(url: str, limit: int) -> bytes:
    if not str(url).lower().startswith("https://"):
        raise ValueError("Yalnızca HTTPS adresleri kullanılabilir.")
    req = urllib.request.Request(url, headers={"User-Agent": f"WindowsDrop/{VERSION}"})
    with _opener.open(req, timeout=10) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("İndirilen dosya beklenenden büyük.")
    return data


def parse_ver(v) -> tuple:
    return tuple(int(x) for x in str(v).strip().split("."))


def check_update():
    url = str(CONFIG.get("update_manifest_url", "")).strip()
    if not url:
        update_state.update(status="unconfigured", latest=None, manifest=None,
                            message="Güncelleme adresi ayarlanmadı (windowsdrop_config.json).")
        return
    try:
        m = json.loads(https_get(url, 65536).decode("utf-8"))
        latest = str(m["version"])
        if parse_ver(latest) > parse_ver(VERSION):
            note = str(m.get("notes", ""))[:200]
            update_state.update(status="available", latest=latest, manifest=m,
                                message=f"Yeni sürüm {latest} mevcut. {note}".strip())
        else:
            update_state.update(status="current", latest=latest, manifest=None, message="Uygulama güncel.")
    except Exception as e:
        update_state.update(status="error", manifest=None, message=f"Güncelleme denetlenemedi: {e}")


def verify_signature(data: bytes, sig_b64: str):
    """Güncellemenin, geliştiricinin gizli anahtarıyla imzalandığını doğrular (Ed25519)."""
    if not UPDATE_PUBLIC_KEY:
        raise RuntimeError("Güncelleme imza anahtarı tanımlı değil (UPDATE_PUBLIC_KEY); güvenlik için uygulanmadı.")
    if not sig_b64:
        raise ValueError("Güncellemede imza yok; iptal edildi.")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise RuntimeError("İmza doğrulaması için 'cryptography' paketi gerekli: pip install cryptography")
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_PUBLIC_KEY))
        pub.verify(base64.b64decode(sig_b64), data)
    except (InvalidSignature, ValueError):
        raise ValueError("İmza doğrulanamadı; güncelleme iptal edildi.")


def apply_update() -> str:
    if FROZEN:
        raise RuntimeError(".exe sürümü bu yöntemle güncellenemez; yeni .exe dosyasını indir.")
    m = update_state.get("manifest")
    if update_state["status"] != "available" or not m:
        raise RuntimeError("Önce güncellemeleri denetle.")
    data = https_get(m["url"], 2_000_000)
    if hashlib.sha256(data).hexdigest().lower() != str(m["sha256"]).strip().lower():
        raise ValueError("SHA-256 doğrulaması başarısız; güncelleme iptal edildi.")
    verify_signature(data, str(m.get("signature", "")))
    src = data.decode("utf-8")
    compile(src, "windowsdrop_update", "exec")  # söz dizimi hatası varsa çalıştırmadan reddet
    vm = re.search(r'^VERSION = "([\d.]+)"', src, re.M)
    if not vm or vm.group(1) != str(m["version"]):
        raise ValueError("Dosyadaki sürüm numarası manifestle uyuşmuyor; güncelleme iptal edildi.")
    if parse_ver(vm.group(1)) <= parse_ver(VERSION):
        raise ValueError("Eski veya aynı sürüme geri dönüş reddedildi.")
    backup = APP_FILE.with_name(APP_FILE.name + ".bak")
    shutil.copy2(APP_FILE, backup)
    tmp = APP_FILE.with_name(APP_FILE.name + ".new")
    tmp.write_bytes(data)
    os.replace(tmp, APP_FILE)
    msg = f"Sürüm {m['version']} yüklendi. Uygulamayı kapatıp yeniden aç. (Eski sürüm: {backup.name})"
    update_state.update(status="updated", manifest=None, message=msg)
    print(msg)
    return msg


def update_public() -> dict:
    return {"status": update_state["status"], "latest": update_state["latest"], "message": update_state["message"]}


# ---------- güvenlik katmanı ----------
@app.before_request
def guard():
    global last_activity
    try:
        ip = ipaddress.ip_address(request.remote_addr)
    except ValueError:
        abort(403)
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        abort(403)
    if request.path.startswith(("/pc", "/thumb/", "/media/", "/files")) or request.path == "/qr.png":
        if not is_local():
            abort(403)
        # Başka bir web sitesinin tarayıcın üzerinden yönetim isteği göndermesini engelle (CSRF)
        if request.method == "POST" and request.headers.get("X-Requested-With") != "WindowsDrop":
            abort(403)
        if request.path in ("/pc/state", "/qr.png", "/files") or request.path.startswith(("/thumb/", "/media/")):
            return  # PC sayfasının kendi sorguları hareketsizlik sayacını sıfırlamaz
    if request.path in ("/pair/status", "/outbox"):
        return      # telefon sayfasının arka plan sorguları da sıfırlamaz
    last_activity = time.time()


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:"
    )
    return resp


def watchdog():
    while True:
        time.sleep(30)
        if time.time() - last_activity > IDLE_LIMIT and active_uploads == 0:
            print("\n15 dakika hareketsiz kalındı, WindowsDrop güvenlik için kapanıyor.")
            try:
                if _tray["icon"]:
                    _tray["icon"].stop()
            except Exception:
                pass
            os._exit(0)


# ---------- sayfalar ----------
PHONE_PAGE = """<!doctype html>
<html lang="tr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>WindowsDrop</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="WindowsDrop">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#0a84ff">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
  body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:24px;background:#f2f2f7;color:#111}
  h1{font-size:26px;margin:0 0 4px} p{color:#555;margin:0 0 20px}
  .btn{display:block;width:100%;padding:18px;border:0;border-radius:14px;background:#0a84ff;color:#fff;
       font-size:18px;font-weight:600;text-align:center;box-sizing:border-box}
  .btn.send{background:#30d158;margin-top:12px}
  .btn.off{opacity:.35;pointer-events:none} .btn:disabled{opacity:.35}
  input[type=file]{display:none}
  #status{background:#fff;border-radius:12px;padding:14px;margin-bottom:16px;font-size:15px}
  #code{font-size:34px;font-weight:700;letter-spacing:6px;text-align:center;margin-top:8px}
  .item{background:#fff;border-radius:12px;padding:12px;margin-top:10px;font-size:14px;word-break:break-all}
  .bar{height:6px;background:#e5e5ea;border-radius:3px;margin-top:8px;overflow:hidden}
  .bar>div{height:100%;width:0;background:#0a84ff;transition:width .2s}
  .ok .bar>div{background:#30d158} .err .bar>div{background:#ff453a}
  .done{background:#fff;border-radius:12px;padding:16px;margin-top:16px;text-align:center}
  .dt{font-weight:700;color:#248a3d;font-size:18px;margin-bottom:4px}
  .dn{font-size:20px;font-weight:600;margin:8px 0}
  .df{color:#d70015;margin-top:6px}
  .hint{font-size:12px;color:#777;margin-top:16px}
  .tag{color:#b25e00;font-size:13px;margin-top:4px}
  .opt{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:14px;color:#444}
  .opt input{width:20px;height:20px}
  .lnk{display:inline-block;margin:10px 8px 0 0;padding:9px 16px;border-radius:10px;background:#0a84ff;
       color:#fff;text-decoration:none;font-size:15px}
  .lnk.alt{background:#e5e5ea;color:#111}
  h2{font-size:18px;margin:24px 0 0}
  .brand{display:flex;align-items:center;gap:10px}
  .btn{border-radius:16px;background:linear-gradient(180deg,#56a8ff,#2f7bff)}
  h2{color:#0c2052}
</style></head><body>
<h1 class="brand"><img src="/icon.png" alt="" width="52" height="52" style="border-radius:12px"><span>WindowsDrop</span></h1>
<p>Fotoğraf ve videoları bilgisayara gönder.</p>
<div id="status"><span id="msg">Bağlanılıyor...</span><div id="code"></div></div>
<label id="pick" class="btn off" for="f">Fotoğraf / Video Seç</label>
<input id="f" type="file" multiple accept="image/*,video/*">
<label class="opt"><input id="again" type="checkbox"> Daha önce gönderilenleri de tekrar gönder</label>
<button id="send" class="btn send" disabled>Bilgisayara Gönder</button>
<div id="done" class="done" hidden></div>
<div id="list"></div>
<div id="inboxwrap" hidden>
  <h2>Bilgisayardan gelenler</h2>
  <div id="inbox"></div>
  <div class="hint">Fotoğraflar'a kaydetmek için "Aç"a bas, fotoğrafa uzun basıp "Fotoğraflar'a Ekle"yi seç.
  Olmazsa "İndir" dosyayı Dosyalar uygulamasına indirir; oradan Paylaş &rarr; Kaydet de.</div>
</div>
<div class="hint">İpucu: Orijinal HEIC/HEVC dosyaları korumak için seçim ekranında
"Seçenekler" altından uyumluluk dönüşümünü kapat ("Tümü" / orijinal).</div>
<script>
const $ = id => document.getElementById(id);
const qs = new URLSearchParams(location.search);
const t = qs.get("t");
let sid = null, files = [], approved = false;
try { if (!t) sid = sessionStorage.getItem("sid"); } catch (e) {}
history.replaceState(null, "", "/");   // anahtarı adres çubuğundan sil

function msg(s) { $("msg").textContent = s; }
function clearSid() { try { sessionStorage.removeItem("sid"); } catch (e) {} }

async function start() {
  if (!sid) {
    if (!t) { msg("Bağlantı anahtarı yok. Bilgisayardaki QR kodu tekrar okut."); return; }
    const r = await fetch("/pair?t=" + encodeURIComponent(t), {method: "POST"});
    if (!r.ok) {
      msg(r.status === 429 ? "Çok fazla deneme yapıldı. Daha sonra tekrar dene."
                           : "Anahtar geçersiz veya süresi dolmuş. Bilgisayardaki yeni QR kodu okut.");
      return;
    }
    const j = await r.json(); sid = j.sid;
    try { sessionStorage.setItem("sid", sid); } catch (e) {}
  }
  poll();
}

async function poll() {
  let j;
  try { j = await (await fetch("/pair/status?sid=" + encodeURIComponent(sid))).json(); }
  catch (e) { setTimeout(poll, 2000); return; }
  if (j.status === "pending") {
    msg("Bilgisayarda onay bekleniyor. Ekrandaki kod aynı mı kontrol et:");
    $("code").textContent = j.code || "";
    setTimeout(poll, 1500);
  } else if (j.status === "approved") {
    approved = true; $("code").textContent = "";
    msg("Bağlandı ✓ Dosya seçebilirsin.");
    $("pick").classList.remove("off");
    loadOutbox();
    setTimeout(poll, 5000);
  } else {
    approved = false; clearSid(); $("code").textContent = "";
    $("pick").classList.add("off"); $("send").disabled = true;
    msg(j.status === "denied" ? "Bağlantı bilgisayarda reddedildi."
                              : "Oturum sona erdi. Bilgisayardaki QR kodu tekrar okut.");
  }
}

let dupFlags = [];

async function check() {
  if (!approved || !files.length) return null;
  let r;
  try {
    r = await fetch("/check?sid=" + encodeURIComponent(sid), {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({files: files.map(x => ({name: x.name, size: x.size})), include_dups: $("again").checked})});
  } catch (e) { return null; }
  if (!r.ok) return null;
  const j = await r.json(); dupFlags = j.dups;
  files.forEach((x, i) => {
    const d = $("i" + i); if (!d) return;
    let tag = d.querySelector(".tag");
    if (!tag) { tag = document.createElement("div"); tag.className = "tag"; d.insertBefore(tag, d.querySelector(".bar")); }
    tag.textContent = (j.dups[i] && !$("again").checked) ? "Daha önce aktarılmış, atlanacak" : "";
  });
  return j;
}
$("again").onchange = check;

$("f").onchange = () => {
  files = [...$("f").files]; $("list").innerHTML = ""; $("done").hidden = true;
  files.forEach((x, i) => {
    const d = document.createElement("div"); d.className = "item"; d.id = "i" + i;
    const n = document.createElement("div"); n.textContent = x.name + " (" + (x.size / 1048576).toFixed(1) + " MB)";
    const b = document.createElement("div"); b.className = "bar"; b.appendChild(document.createElement("div"));
    d.appendChild(n); d.appendChild(b); $("list").appendChild(d);
  });
  $("send").disabled = !files.length || !approved;
  check();
};

function up(file, i) {
  return new Promise(res => {
    const el = $("i" + i), bar = el.querySelector(".bar>div");
    const fd = new FormData();
    fd.append("mtime", file.lastModified || "");      // çekim tarihi için yedek bilgi
    fd.append("file", file, file.name);
    const x = new XMLHttpRequest();
    x.open("POST", "/upload?sid=" + encodeURIComponent(sid));
    x.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = (e.loaded / e.total * 100) + "%"; };
    x.onload = () => { const ok = x.status === 200; el.classList.add(ok ? "ok" : "err"); bar.style.width = "100%"; res(ok); };
    x.onerror = () => { el.classList.add("err"); res(false); };
    x.send(fd);
  });
}

function box(lines) {
  const d = $("done"); d.innerHTML = ""; d.hidden = false;
  lines.forEach(([text, cls]) => { const e = document.createElement("div"); e.textContent = text; if (cls) e.className = cls; d.appendChild(e); });
}

function showDone(j, failed, skipped) {
  if (!j) { box([["Aktarım bitti ama özet alınamadı."]]); return; }
  const L = [["Aktarım tamamlandı ✓", "dt"], [j.at],
    [j.photos + " fotoğraf, " + j.videos + " video aktarıldı" + (j.other ? " (+" + j.other + " ek dosya)" : ""), "dn"],
    ["Toplam boyut: " + j.size]];
  if (skipped) L.push([skipped + " dosya daha önce aktarıldığı için atlandı"]);
  if (failed) L.push([failed + " dosya gönderilemedi", "df"]);
  box(L);
}

$("send").onclick = async () => {
  $("send").disabled = true; $("done").hidden = true;
  const c = await check();
  if (!c) { box([["Bilgisayara ulaşılamadı. Bağlantıyı kontrol et.", "df"]]); $("send").disabled = false; return; }
  if (!c.enough) {
    box([["Bilgisayarda yeterli disk alanı yok. Hiçbir dosya gönderilmedi.", "df"],
         ["Gerekli: " + c.need_h + " — boş: " + c.free_h]]);
    $("send").disabled = false; return;
  }
  const todo = files.map((x, i) => i).filter(i => $("again").checked || !c.dups[i]);
  const skipped = files.length - todo.length;
  if (!todo.length) {
    box([["Gönderilecek yeni dosya yok", "dt"], [skipped + " dosya daha önce aktarılmış."],
         ["Yine de göndermek için yukarıdaki kutuyu işaretle."]]);
    $("send").disabled = false; return;
  }
  $("send").textContent = "Gönderiliyor...";
  let failed = 0;
  for (const i of todo) { if (!(await up(files[i], i))) failed++; }
  let j = null;
  try { j = await (await fetch("/finish?sid=" + encodeURIComponent(sid), {method: "POST"})).json(); } catch (e) {}
  showDone(j, failed, skipped);
  $("send").textContent = "Bilgisayara Gönder"; $("send").disabled = true;
  $("f").value = ""; files = [];
};

async function loadOutbox() {
  let j;
  try { const r = await fetch("/outbox?sid=" + encodeURIComponent(sid)); if (!r.ok) return; j = await r.json(); }
  catch (e) { return; }
  const b = $("inbox"); b.innerHTML = ""; $("inboxwrap").hidden = !j.length;
  j.forEach(f => {
    const d = document.createElement("div"); d.className = "item";
    const n = document.createElement("div"); n.textContent = f.name + " (" + f.size + ")"; d.appendChild(n);
    const mk = (text, href, cls) => { const a = document.createElement("a"); a.className = "lnk " + (cls || "");
      a.textContent = text; a.href = href; return a; };
    const base = "/outbox/" + encodeURIComponent(f.id) + "?sid=" + encodeURIComponent(sid);
    const o = mk("Aç", base); o.target = "_blank"; d.appendChild(o);
    d.appendChild(mk("İndir", base + "&dl=1", "alt"));
    b.appendChild(d);
  });
}
start();
</script></body></html>"""

PC_PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WindowsDrop</title>
<meta name="theme-color" content="#0a1f4d">
<link rel="icon" href="/icon.png">
<style>
  :root{--bg1:#0c2052;--bg2:#040a22;--card:rgba(255,255,255,.055);--line:rgba(120,165,255,.16);
        --text:#e8eefc;--muted:#8fa3c9;--acc:#2f7bff;--acc2:#56a8ff;--ok:#30d158;--no:#ff453a}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;font-family:"Segoe UI Variable","Segoe UI",system-ui,sans-serif;color:var(--text);
       background:radial-gradient(900px 500px at 50% -120px,#18397f 0%,transparent 70%),linear-gradient(180deg,var(--bg1),var(--bg2)) fixed;
       padding:28px 20px 40px}
  .wrap{max-width:900px;margin:0 auto}
  header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
  header img{width:52px;height:52px;border-radius:12px;flex:none;box-shadow:0 6px 22px rgba(47,123,255,.35)}
  header h1{margin:0;font-size:24px;font-weight:700;letter-spacing:.2px}
  header .ver{color:var(--muted);font-size:13px;margin-left:8px;font-weight:400}
  h3{font-size:15px;font-weight:600;margin:0;color:#dfe8ff;letter-spacing:.3px}
  .grid{display:grid;grid-template-columns:260px 1fr;gap:18px;align-items:start}
  @media (max-width:720px){.grid{grid-template-columns:1fr}}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:16px;backdrop-filter:blur(6px)}
  .qrbox{background:#fff;border-radius:14px;padding:12px;display:flex;justify-content:center}
  .qrbox img{width:220px;height:220px;display:block;image-rendering:pixelated}
  .hint{color:var(--muted);font-size:13px;line-height:1.5;margin:12px 2px 0}
  code{background:rgba(255,255,255,.07);padding:3px 8px;border-radius:7px;word-break:break-all;font-size:12.5px;color:#cfe0ff}
  .muted{color:var(--muted);font-size:13px;line-height:1.5}
  .big{font-size:17px;font-weight:600}
  .row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}
  button{font:inherit;font-size:14px;font-weight:600;padding:9px 16px;margin:4px 4px 4px 0;border:0;border-radius:10px;cursor:pointer;
         transition:filter .15s,transform .05s}
  button:hover{filter:brightness(1.12)} button:active{transform:scale(.97)}
  .ok{background:var(--ok);color:#052;} .no{background:var(--no);color:#fff} .gray{background:rgba(255,255,255,.1);color:var(--text)}
  .pri{background:linear-gradient(180deg,var(--acc2),var(--acc));color:#fff}
  .ghost{background:transparent;color:var(--acc2);border:1px solid var(--line)}
  .code{font-size:34px;font-weight:700;letter-spacing:7px;margin:8px 0;color:#fff;font-variant-numeric:tabular-nums}
  .req{border:1px solid rgba(255,159,10,.55);background:rgba(255,159,10,.08)}
  .drop{border:1.5px dashed rgba(86,168,255,.45);border-radius:16px;padding:22px;text-align:center;transition:.15s;background:rgba(47,123,255,.04)}
  .drop.over{border-color:var(--acc2);background:rgba(47,123,255,.14)}
  #outl{list-style:none;padding:0;margin:12px 0 0;text-align:left}
  #outl li{display:flex;justify-content:space-between;align-items:center;gap:8px;background:rgba(255,255,255,.05);
           padding:8px 12px;border-radius:10px;margin:6px 0;font-size:14px;word-break:break-all}
  .hist{font-size:14px;color:#c9d6f3;margin:7px 0;padding-left:10px;border-left:2px solid var(--line)}
  .files{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin:12px 0 0;padding:0;list-style:none}
  .fitem{position:relative;background:rgba(255,255,255,.05);border:1px solid var(--line);border-radius:14px;overflow:hidden;
         cursor:pointer;transition:transform .15s,box-shadow .15s,border-color .15s}
  .fitem:hover{transform:translateY(-3px);border-color:rgba(86,168,255,.5);box-shadow:0 10px 26px rgba(0,0,0,.35)}
  .thumb{aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;background:linear-gradient(160deg,#17346f,#0a1a44);
         color:var(--acc2);font-weight:700;font-size:13px;letter-spacing:1px;position:relative;overflow:hidden}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .3s}
  .fitem:hover .thumb img{transform:scale(1.05)}
  .thumb.vid::before{content:"";position:absolute;inset:0;background:rgba(0,0,0,.25)}
  .play{position:absolute;width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,.92);box-shadow:0 4px 14px rgba(0,0,0,.4);
        display:flex;align-items:center;justify-content:center}
  .play::after{content:"";width:0;height:0;margin-left:4px;border-left:13px solid #1a4fd1;border-top:8px solid transparent;border-bottom:8px solid transparent}
  .badge{position:absolute;right:6px;bottom:6px;background:rgba(3,8,24,.75);color:#dfe8ff;font-size:10px;font-weight:700;
         letter-spacing:.6px;padding:3px 6px;border-radius:6px}
  .fname{font-size:11.5px;color:#c7d4f0;padding:7px 9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .empty{color:var(--muted);font-size:14px;padding:18px 4px}
  .lowdisk{color:#ff8a80}
  .viewer{position:fixed;inset:0;z-index:50;background:rgba(3,8,24,.94);backdrop-filter:blur(10px);
          display:flex;flex-direction:column;align-items:center;justify-content:center;animation:fade .18s ease}
  .viewer[hidden]{display:none}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .vbar{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;gap:14px;padding:16px 20px;color:#dfe8ff}
  .vcount{font-size:13px;color:var(--muted);white-space:nowrap}
  .vname{flex:1;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:center}
  .vclose{width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.1);color:#fff;font-size:18px;padding:0;margin:0}
  .vstage{flex:1;min-height:0;width:100%;display:flex;align-items:center;justify-content:center;padding:72px 84px 48px;box-sizing:border-box}
  .vstage img,.vstage video{max-width:100%;max-height:100%;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.6);outline:none}
  .vnav{position:absolute;top:50%;transform:translateY(-50%);width:52px;height:52px;border-radius:50%;background:rgba(255,255,255,.1);
        color:#fff;font-size:30px;line-height:1;padding:0;margin:0;display:flex;align-items:center;justify-content:center}
  .vnav:hover{background:rgba(255,255,255,.2)}
  .vnav.prev{left:16px} .vnav.next{right:16px}
  .vmsg{color:var(--muted);font-size:15px;text-align:center;padding:30px}
  .vhint{position:absolute;bottom:14px;left:0;right:0;text-align:center;font-size:12px;color:var(--muted)}
  @media (max-width:600px){.vstage{padding:64px 12px 44px}.vnav{width:42px;height:42px;font-size:24px}}
  #upd .panel{margin-bottom:0}
</style></head><body>
<div class="wrap">
<header>
  <img src="/icon.png" alt="">
  <h1>WindowsDrop <span class="ver" id="ver"></span></h1>
</header>

<div class="grid">
  <div>
    <div class="panel">
      <div class="qrbox"><img id="qr" alt="QR kodu"></div>
      <p class="hint">iPhone kamerasıyla QR kodu okut. Aynı Wi-Fi'da olmalısınız.</p>
      <p class="hint">Ya da Safari'ye yaz:<br><code id="url"></code></p>
      <p class="hint">Kayıt klasörü:<br><code id="dir"></code></p>
    </div>
    <div class="muted" id="free"></div>
  </div>

  <div>
    <div id="reqs"></div>
    <div id="devs"></div>
    <div id="sum"></div>

    <div class="panel">
      <div class="drop" id="drop">
        <div class="big">iPhone'a gönder</div>
        <div class="muted" style="margin:6px 0 12px">Dosyaları buraya sürükle ya da seç. iPhone'daki sayfada "Bilgisayardan gelenler" bölümünde görünür.</div>
        <input id="outf" type="file" multiple style="display:none">
        <button class="pri" id="outpick">Dosya seç</button>
        <ul id="outl"></ul>
      </div>
    </div>

    <div class="panel">
      <div class="row">
        <h3>Gelen dosyalar</h3>
        <button class="ghost" id="clr">Listeyi temizle</button>
      </div>
      <div class="muted">Dosyalar silinmez, yalnızca bu listeden gizlenir. Açmak için bir dosyaya tıkla.</div>
      <ul class="files" id="l"></ul>
    </div>

    <div id="upd"></div>
  </div>
</div>
</div>

<div class="viewer" id="viewer" hidden>
  <div class="vbar">
    <span class="vcount" id="vcount"></span>
    <span class="vname" id="vname"></span>
    <button class="vclose" id="vclose" title="Kapat">✕</button>
  </div>
  <button class="vnav prev" id="vprev" title="Önceki (←)">‹</button>
  <div class="vstage" id="vstage"></div>
  <button class="vnav next" id="vnext" title="Sonraki (→)">›</button>
  <div class="vhint">← → ile geç · Esc ile kapat</div>
</div>

<script>
const $ = id => document.getElementById(id);
let lastTok = null;
let gallery = [], gi = 0;
async function post(url, body) {
  const o = {method: "POST", headers: {"X-Requested-With": "WindowsDrop"}};
  if (body) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(body); }
  const r = await fetch(url, o);
  try { return await r.json(); } catch (e) { return {}; }
}
async function decide(sid, allow) { await post("/pc/decide", {sid: sid, allow: allow}); tick(); }
async function revoke() { await post("/pc/revoke"); tick(); }
async function clearFiles() { await post("/files/clear"); tick(); }
async function checkUpd() { await post("/pc/update/check"); tick(); }
async function applyUpd() {
  if (!confirm("Yeni sürüm indirilip doğrulanacak ve mevcut dosyanın yerine yazılacak. Devam edilsin mi?")) return;
  const j = await post("/pc/update/apply"); if (j.message) alert(j.message); tick();
}
async function addOut(list) {
  if (!list.length) return;
  const fd = new FormData(); list.forEach(f => fd.append("files", f, f.name));
  const r = await fetch("/pc/outbox/add", {method: "POST", headers: {"X-Requested-With": "WindowsDrop"}, body: fd});
  if (!r.ok) { let m = "Dosyalar eklenemedi."; try { m = (await r.json()).message || m; } catch (e) {} alert(m); }
  tick();
}
async function rmOut(id) { await post("/pc/outbox/remove", {id: id}); tick(); }
function el(tag, text, cls) { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; }

/* ---------- gelen dosyalar ---------- */
function fileItem(f, idx) {
  const li = el("li", "", "fitem");
  const box = el("div", "", "thumb" + (f.kind === "video" ? " vid" : ""));
  const label = f.ext.replace(".", "").toUpperCase();
  if (f.kind === "photo") {
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = "";
    img.src = "/thumb/" + encodeURIComponent(f.name);
    img.onerror = () => { img.remove(); box.textContent = label; };
    box.appendChild(img);
  } else if (f.kind === "video") {
    box.appendChild(el("div", "", "play"));
  } else {
    box.textContent = label;
  }
  if (f.kind !== "other") box.appendChild(el("span", label, "badge"));
  li.appendChild(box);
  li.appendChild(el("div", f.name, "fname"));
  li.title = f.name;
  li.onclick = () => openViewer(idx);
  return li;
}

/* ---------- önizleme penceresi ---------- */
const viewer = $("viewer"), stage = $("vstage");
function openViewer(i) {
  if (!gallery.length) return;
  gi = i; viewer.hidden = false; document.body.style.overflow = "hidden";
  showCurrent();
}
function closeViewer() {
  viewer.hidden = true; stage.innerHTML = ""; document.body.style.overflow = "";
}
function step(d) {
  if (viewer.hidden || !gallery.length) return;
  gi = (gi + d + gallery.length) % gallery.length; showCurrent();
}
function fallback(f) {
  stage.innerHTML = "";
  stage.appendChild(el("div", "Bu biçim tarayıcıda gösterilemiyor (" + f.ext + ").", "vmsg"));
}
function showCurrent() {
  const f = gallery[gi]; if (!f) return;
  stage.innerHTML = "";
  $("vcount").textContent = (gi + 1) + " / " + gallery.length;
  $("vname").textContent = f.name;
  const src = "/media/" + encodeURIComponent(f.name);
  let node = null;
  if (f.kind === "photo") {
    node = document.createElement("img"); node.src = src; node.alt = f.name;
  } else if (f.kind === "video") {
    node = document.createElement("video"); node.src = src; node.controls = true;
    node.autoplay = true; node.playsInline = true;
  }
  if (!node) return fallback(f);
  node.onerror = () => fallback(f);
  stage.appendChild(node);
}
$("vclose").onclick = closeViewer;
$("vprev").onclick = () => step(-1);
$("vnext").onclick = () => step(1);
viewer.addEventListener("click", e => { if (e.target === viewer || e.target === stage) closeViewer(); });
document.addEventListener("keydown", e => {
  if (viewer.hidden) return;
  if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "ArrowRight") step(1);
  else if (e.key === "Escape") closeViewer();
});
let touchX = null;
viewer.addEventListener("touchstart", e => { touchX = e.touches[0].clientX; }, {passive: true});
viewer.addEventListener("touchend", e => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX; touchX = null;
  if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
});

/* ---------- durum ---------- */
async function tick() {
  let s; try { s = await (await fetch("/pc/state")).json(); } catch (e) { return; }
  $("url").textContent = s.url; $("dir").textContent = s.dir; $("ver").textContent = "v" + s.version;
  if (s.token !== lastTok) { lastTok = s.token; $("qr").src = "/qr.png?v=" + encodeURIComponent(s.token); }

  // açık önizleme: aynı dosyayı yeni sırada bul, yoksa kapat
  if (!viewer.hidden && gallery[gi]) {
    const cur = gallery[gi].name;
    const k = s.files.findIndex(x => x.name === cur);
    if (k >= 0) { gi = k; $("vcount").textContent = (gi + 1) + " / " + s.files.length; }
    else closeViewer();
  }
  gallery = s.files;

  const r = $("reqs"); r.innerHTML = "";
  s.pending.forEach(p => {
    const c = el("div", "", "panel req");
    c.appendChild(el("div", "Yeni cihaz bağlanmak istiyor: " + p.device + " (" + p.ip + ")", "big"));
    c.appendChild(el("div", p.code, "code"));
    c.appendChild(el("div", "Telefondaki kodla aynıysa izin ver.", "muted"));
    const a = el("button", "İzin ver", "ok"); a.onclick = () => decide(p.sid, true);
    const d = el("button", "Reddet", "no"); d.onclick = () => decide(p.sid, false);
    c.appendChild(a); c.appendChild(d); r.appendChild(c);
  });

  const dv = $("devs"); dv.innerHTML = "";
  if (s.devices.length) {
    const c = el("div", "", "panel");
    c.appendChild(el("div", "Bağlı cihaz: " + s.devices.join(", "), "muted"));
    const b = el("button", "Tüm bağlantıları kes", "gray"); b.onclick = revoke;
    c.appendChild(b); dv.appendChild(c);
  }

  const sm = $("sum"); sm.innerHTML = "";
  if (s.history.length || s.stats.photos || s.stats.videos) {
    const c = el("div", "", "panel");
    c.appendChild(el("div", "Bu oturumda: " + s.stats.photos + " fotoğraf, " + s.stats.videos + " video (" + s.stats.size + ")", "big"));
    s.history.forEach(h => {
      c.appendChild(el("div", h.at + " · " + h.device + ": " + h.photos + " fotoğraf, " + h.videos + " video (" + h.size + ")", "hist"));
    });
    sm.appendChild(c);
  }

  const ol = $("outl"); ol.innerHTML = "";
  s.outbox.forEach(o => {
    const li = el("li", o.name + " (" + o.size + ")"); const x = el("button", "Kaldır", "gray");
    x.onclick = () => rmOut(o.id); li.appendChild(x); ol.appendChild(li);
  });

  const fr = $("free"); fr.textContent = "Boş disk alanı: " + s.free + (s.lowdisk ? " — azalıyor, aktarımlar durabilir" : "");
  fr.className = s.lowdisk ? "lowdisk" : "muted";

  const l = $("l"); l.innerHTML = "";
  if (!s.files.length) l.appendChild(el("div", "Henüz gelen dosya yok.", "empty"));
  s.files.forEach((f, i) => l.appendChild(fileItem(f, i)));

  const u = $("upd"); u.innerHTML = "";
  const c = el("div", "", "panel");
  if (s.update.message) c.appendChild(el("div", s.update.message, "muted"));
  if (s.update.status === "available") { const b = el("button", "Güncelle", "ok"); b.onclick = applyUpd; c.appendChild(b); }
  if (s.update.status !== "updated") { const b = el("button", "Güncellemeleri denetle", "gray"); b.onclick = checkUpd; c.appendChild(b); }
  u.appendChild(c);
}
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => e.preventDefault());
$("outpick").onclick = () => $("outf").click();
$("outf").onchange = () => { addOut([...$("outf").files]); $("outf").value = ""; };
const drop = $("drop");
drop.addEventListener("dragover", () => drop.classList.add("over"));
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", e => { drop.classList.remove("over"); addOut([...e.dataTransfer.files]); });
$("clr").onclick = clearFiles;
tick(); setInterval(tick, 1500);
</script></body></html>"""


# ---------- rotalar ----------
@app.get("/")
def phone():
    if is_local() and not request.args.get("t"):
        return redirect("/pc")
    return PHONE_PAGE


@app.get("/pc")
def pc():
    return PC_PAGE


@app.get("/icon.png")
def app_icon():
    p = BASE_DIR / "icon-512.png"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def touch_icon():
    p = BASE_DIR / "apple-touch-icon.png"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png")


@app.get("/qr.png")
def qr():
    buf = io.BytesIO()
    qrcode.make(phone_url()).save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/thumb/<name>")
def thumb(name):
    safe = secure_filename(name)
    p = SAVE_DIR / safe
    if not safe or p.suffix.lower() not in BROWSER_IMG or not p.is_file():
        abort(404)
    return send_file(p, mimetype=mimetypes.guess_type(p.name)[0] or "image/jpeg", conditional=True)


@app.get("/media/<name>")
def media(name):
    safe = secure_filename(name)
    p = SAVE_DIR / safe
    ext = p.suffix.lower()
    if not safe or ext not in (PHOTO_EXT | VIDEO_EXT) or not p.is_file():
        abort(404)
    mime = MIME_FIX.get(ext) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return send_file(p, mimetype=mime, conditional=True)


@app.post("/files/clear")
def files_clear():
    with lock:
        hidden_files.update(p.name for p in SAVE_DIR.iterdir() if p.is_file())
    return jsonify(ok=True)


@app.get("/files")
def files():
    items = sorted(SAVE_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([p.name for p in items[:30]])


@app.get("/pc/state")
def pc_state():
    now = time.time()
    with lock:
        expire_sessions()
        pending, devices = [], []
        for sid, s in sessions.items():
            if s["status"] == "pending" and now - s["created"] > PENDING_TTL:
                s["status"] = "expired"
            if s["status"] == "approved" and now - s["approved_at"] > SESSION_TTL:
                s["status"] = "expired"
            if s["status"] == "pending":
                pending.append({"sid": sid, "code": s["code"], "device": s["device"], "ip": s["ip"]})
            elif s["status"] == "approved":
                devices.append(f'{s["device"]} ({s["ip"]})')
        st = {"photos": stats["photo"], "videos": stats["video"], "size": human_size(stats["bytes"])}
        hist = list(history[:5])
        out = [{"id": k, "name": v["name"], "size": human_size(v["size"])} for k, v in outbox.items()]
    with lock:
        hidden = set(hidden_files)
    items = [p for p in SAVE_DIR.iterdir() if p.is_file() and p.name not in hidden]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = []
    for p in items[:30]:
        ext = p.suffix.lower()
        kind = "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else "other"
        files.append({"name": p.name, "kind": kind, "ext": ext or "?"})
    free = free_bytes()
    return jsonify(
        url=phone_url(), token=current_token(), dir=str(SAVE_DIR), version=VERSION,
        pending=pending, devices=devices, files=files,
        stats=st, history=hist, update=update_public(),
        outbox=out, free=human_size(free), lowdisk=free < min_free() * 2,
    )


@app.post("/pc/decide")
def pc_decide():
    data = request.get_json(silent=True) or {}
    with lock:
        s = sessions.get(data.get("sid", ""))
        if not s or s["status"] != "pending":
            abort(404)
        if data.get("allow"):
            s["status"] = "approved"
            s["approved_at"] = time.time()
            rotate_token()  # QR artık başka biri tarafından kullanılamaz
            print(f'Cihaz onaylandı: {s["device"]} {s["ip"]}')
        else:
            s["status"] = "denied"
            denials[s["ip"]] = denials.get(s["ip"], 0) + 1
            print(f'Cihaz reddedildi: {s["device"]} {s["ip"]}')
    return "ok"


@app.post("/pc/revoke")
def pc_revoke():
    with lock:
        for s in sessions.values():
            if s["status"] in ("approved", "pending"):
                s["status"] = "revoked"
        rotate_token()
        clear_outbox()
    return "ok"


@app.post("/pc/outbox/add")
def pc_outbox_add():
    files = request.files.getlist("files")
    if not files:
        abort(400)
    cl = request.content_length or 0
    if cl and not space_ok(cl, OUTBOX_DIR):
        return jsonify(message=f"Diskte yeterli boş alan yok (boş: {human_size(free_bytes(OUTBOX_DIR))})."), 507
    added = 0
    for f in files:
        if len(outbox) >= MAX_OUTBOX:
            break
        name = secure_filename(f.filename or "") or "dosya"
        oid = secrets.token_hex(6)
        path = OUTBOX_DIR / f"{oid}_{name}"
        try:
            f.save(path)
            size = path.stat().st_size
        except Exception:
            path.unlink(missing_ok=True)
            return jsonify(message="Dosya kopyalanamadı (disk dolu olabilir)."), 500
        with lock:
            outbox[oid] = {"name": name, "path": path, "size": size}
        added += 1
    if not added:
        return jsonify(message=f"En fazla {MAX_OUTBOX} dosya eklenebilir."), 400
    return jsonify(added=added)


@app.post("/pc/outbox/remove")
def pc_outbox_remove():
    data = request.get_json(silent=True) or {}
    with lock:
        item = outbox.pop(str(data.get("id", "")), None)
    if item:
        item["path"].unlink(missing_ok=True)
    return "ok"


@app.post("/pc/update/check")
def pc_update_check():
    check_update()
    return jsonify(update_public())


@app.post("/pc/update/apply")
def pc_update_apply():
    try:
        return jsonify(ok=True, message=apply_update())
    except Exception as e:
        update_state["message"] = f"Güncelleme yapılamadı: {e}"
        return jsonify(ok=False, message=update_state["message"]), 400


@app.post("/pair")
def pair():
    ip = request.remote_addr
    if denials.get(ip, 0) >= MAX_DENIALS:
        abort(429)
    if request.args.get("t") != current_token():
        denials[ip] = denials.get(ip, 0) + 1  # yanlış anahtar denemeleri de sayılır
        abort(403)
    with lock:
        expire_sessions()
        pending = [s for s in sessions.values() if s["status"] == "pending"]
        if len(pending) >= MAX_PENDING:
            abort(429)
        sid = secrets.token_urlsafe(16)
        sessions[sid] = {
            "ip": ip, "status": "pending", "created": time.time(),
            "device": device_name(request.headers.get("User-Agent", "")),
            "code": f"{secrets.randbelow(10000):04d}",
            "batch": new_batch(),
        }
    return jsonify(sid=sid)


@app.get("/pair/status")
def pair_status():
    s = get_session(request.args.get("sid", ""))
    if not s:
        return jsonify(status="expired")
    return jsonify(status=s["status"], code=s["code"] if s["status"] == "pending" else "")


def approved_session():
    s = get_session(request.args.get("sid", ""))
    if not s or s["status"] != "approved":
        abort(403)
    return s


@app.post("/check")
def check():
    """Gönderimden önce: tekrar eden dosyalar ve disk alanı."""
    approved_session()
    data = request.get_json(silent=True) or {}
    items = data.get("files")
    if not isinstance(items, list) or len(items) > 5000:
        abort(400)
    include = bool(data.get("include_dups"))
    dups, need = [], 0
    for it in items:
        try:
            name, size = str(it.get("name", "")), max(0, int(it.get("size", 0)))
        except (AttributeError, TypeError, ValueError):
            abort(400)
        dup = seen_key(name, size) in seen
        dups.append(dup)
        if include or not dup:
            need += size
    free = free_bytes()
    return jsonify(dups=dups, need=need, need_h=human_size(need), free=free, free_h=human_size(free),
                   enough=free - min_free() >= need)


@app.post("/upload")
def upload():
    global active_uploads, last_activity
    s = approved_session()
    cl = request.content_length
    if cl and not space_ok(cl):
        return jsonify(error="Bilgisayarda yeterli disk alanı yok."), 507
    with lock:
        active_uploads += 1
    try:
        f = request.files.get("file")
        if not f or not f.filename:
            abort(400)
        ext = Path(secure_filename(f.filename)).suffix.lower()
        if ext not in ALLOWED_EXT:
            abort(415)
        kind = "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else "other"
        path = unique_path(f.filename)
        try:
            f.save(path)
            size = path.stat().st_size
        except Exception:
            path.unlink(missing_ok=True)  # yarım kalan dosyayı sil (disk dolu olabilir)
            abort(500)
        src = apply_capture_time(path, ext, request.form.get("mtime"))
        with lock:
            s["batch"][kind] += 1
            s["batch"]["bytes"] += size
            stats[kind] += 1
            stats["bytes"] += size
            mark_seen(f.filename, size)
        log_transfer(s["device"], path.name, kind, size)
        print(f"Alındı: {path.name}" + (f" (tarih: {src})" if src else ""))
        return jsonify(kind=kind)
    finally:
        with lock:
            active_uploads -= 1
        last_activity = time.time()


# ---------- bilgisayardan iPhone'a ----------
@app.get("/outbox")
def outbox_list():
    approved_session()
    with lock:
        return jsonify([{"id": k, "name": v["name"], "size": human_size(v["size"])} for k, v in outbox.items()])


@app.get("/outbox/<oid>")
def outbox_get(oid):
    approved_session()
    with lock:
        item = outbox.get(oid)
    if not item or not item["path"].exists():
        abort(404)
    ext = Path(item["name"]).suffix.lower()
    media = ext in PHOTO_EXT or ext in VIDEO_EXT
    inline = media and not request.args.get("dl")
    mime = (MIME_FIX.get(ext) or mimetypes.guess_type(item["name"])[0]) if media else None
    return send_file(item["path"], mimetype=mime or "application/octet-stream", as_attachment=not inline,
                     download_name=item["name"], conditional=True)


@app.post("/finish")
def finish():
    s = approved_session()
    with lock:
        b = s["batch"]
        s["batch"] = new_batch()
        entry = {
            "at": now_str(), "device": s["device"],
            "photos": b["photo"], "videos": b["video"], "other": b["other"],
            "size": human_size(b["bytes"]),
        }
        if b["photo"] + b["video"] + b["other"]:
            history.insert(0, entry)
            del history[20:]
    print(f'Aktarım bitti: {entry["at"]} - {entry["photos"]} fotoğraf, {entry["videos"]} video ({entry["size"]})')
    return jsonify(entry)


# ---------- kısayol / tepsi / başlatma ----------
def _shortcut_path() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "WindowsDrop.lnk"


def _ps_quote(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def shortcut_script(lnk: Path) -> str:
    """Başlat menüsü kısayolunu oluşturan PowerShell betiği."""
    if FROZEN:
        target, args = Path(sys.executable), ""
    else:
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")                 # konsol penceresi açmaz
        target, args = (pyw if pyw.exists() else py), f'"{APP_FILE}"'
    ico = APP_FILE.parent / "icon.ico"
    icon_loc = str(ico) if (not FROZEN and ico.exists()) else f"{target},0"
    return (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut({_ps_quote(lnk)}); "
        f"$s.TargetPath = {_ps_quote(target)}; $s.Arguments = {_ps_quote(args)}; "
        f"$s.WorkingDirectory = {_ps_quote(APP_FILE.parent)}; "
        "$s.Description = 'WindowsDrop - iPhone dosya aktarımı'; "
        f"$s.IconLocation = {_ps_quote(icon_loc)}; $s.Save()"
    )


def install_shortcut():
    if os.name != "nt":
        sys.exit("Kısayol yalnızca Windows'ta oluşturulabilir.")
    lnk = _shortcut_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    enc = base64.b64encode(shortcut_script(lnk).encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], check=True)
    print(f"Kısayol oluşturuldu: {lnk}\nBaşlat menüsünde 'WindowsDrop' yazarak açabilirsin.")


def remove_shortcut():
    if os.name != "nt":
        sys.exit("Kısayol yalnızca Windows'ta kaldırılabilir.")
    lnk = _shortcut_path()
    lnk.unlink(missing_ok=True)
    print(f"Kısayol kaldırıldı: {lnk}")


def start_tray():
    """Sağ alttaki bildirim alanına simge koyar (pystray kuruluysa)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return
    png = BASE_DIR / "icon-512.png"
    if png.exists():
        img = Image.open(png).convert("RGBA").resize((64, 64), Image.LANCZOS)
    else:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((2, 2, 62, 62), fill=(10, 132, 255, 255))
        d.polygon([(32, 14), (50, 34), (38, 34), (38, 50), (26, 50), (26, 34), (14, 34)], fill=(255, 255, 255, 255))

    def open_panel(icon=None, item=None):
        show_panel()

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    icon = pystray.Icon("WindowsDrop", img, f"WindowsDrop v{VERSION}",
                        menu=pystray.Menu(pystray.MenuItem("Paneli aç", open_panel, default=True),
                                          pystray.MenuItem("Çıkış", quit_app)))
    _tray["icon"] = icon
    icon.run_detached()


_window = {"w": None}


def show_panel():
    w = _window["w"]
    if w is not None:
        try:
            w.show()
            return
        except Exception:
            pass
    webbrowser.open(f"http://localhost:{PORT}/pc")


def desktop_window_available() -> bool:
    try:
        import webview  # noqa: F401  (pywebview)
        return True
    except ImportError:
        return False


def run_window():
    """Kendi masaüstü penceresinde açar. Kapatınca tepsiye iner, uygulama arka planda çalışır."""
    import webview
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False),
                     daemon=True).start()
    time.sleep(0.8)
    w = webview.create_window("WindowsDrop", f"http://localhost:{PORT}/pc",
                              width=980, height=760, min_size=(720, 560))
    def on_closing():
        w.hide()
        return False          # pencerenin kapanmasını engelle
    w.events.closing += on_closing
    _window["w"] = w
    webview.start()


def port_in_use() -> bool:
    c = socket.socket()
    c.settimeout(0.5)
    try:
        return c.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        c.close()


if __name__ == "__main__":
    if "--install-shortcut" in sys.argv:
        install_shortcut()
        sys.exit(0)
    if "--remove-shortcut" in sys.argv:
        remove_shortcut()
        sys.exit(0)
    if port_in_use():   # zaten çalışıyorsa yenisini başlatma, panelini aç
        print("WindowsDrop zaten çalışıyor, panel açılıyor.")
        webbrowser.open(f"http://localhost:{PORT}/pc")
        sys.exit(0)
    clear_outbox()
    load_seen()
    start_tray()
    threading.Thread(target=watchdog, daemon=True).start()
    if CONFIG.get("check_updates_on_start") and str(CONFIG.get("update_manifest_url", "")).strip():
        threading.Thread(target=check_update, daemon=True).start()
    print(f"\nWindowsDrop v{VERSION} çalışıyor.\nTelefon adresi: {phone_url()}\nKayıt: {SAVE_DIR}")
    print("Bilgisayardaki sayfada QR kodu görebilirsin. 15 dk hareketsizlikte otomatik kapanır.\n")
    if desktop_window_available():
        run_window()
    else:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}/pc")).start()
        app.run(host="0.0.0.0", port=PORT, threaded=True)
