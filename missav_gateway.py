"""
missav-gw v2 — API Gateway lengkap untuk missav.ws (hasil reverse engineering).

Sumber data (recon 2026-10-20):
  * Halaman listing (home, /new, /release, /search/<q>, /genres/<g>, /actresses/<a>,
    /makers/<m>, /tags/<t>, /<studio>) dirender server-side; tiap kartu punya
    cover fourhoi.com/<code>/cover-t.jpg + badge subtitle + durasi H:MM:SS.
  * Halaman video: blok detail (Release date/Code/Title/Actress/Genre/Maker/Tag) +
    og:video:duration + packer JS berlapis yang berakhir di URL m3u8 surrit.com.
  * Cloudflare memblokir IP datacenter untuk missav.ws (403). Jalur andal:
    FlareSolverr (browser sungguhan) -> proxy pool -> curl_cffi direct.
  * surrit.com (CDN video) TIDAK memblokir IP ini; cukup Referer/Origin + TLS
    impersonation Chrome (curl_cffi). Segment TS tidak terenkripsi.

Arsitektur:
    klien ─┬─ /api/v1/search            pencarian + sort + pagination
           ├─ /api/v1/video/{code}      detail lengkap + daftar kualitas + related
           ├─ /api/v1/stream/{code}     master/variant m3u8 (URI sudah di-proxy)
           ├─ /api/v1/proxy/segment     pipe segment TS (Range pass-through)
           ├─ /api/v1/download/{code}   remux MP4 via ffmpeg (stream, tanpa disk)
           ├─ /api/v1/browse/{section}  new/release/uncensored-leak/studio/hot/...
           ├─ /api/v1/genres|makers|actresses|tags   index + listing
           ├─ /api/v1/player/{code}     halaman player hls.js siap pakai
           └─ /api/v1/image             proxy cover/preview (allowlist CDN)

Env: GW_HOST, GW_PORT, GW_WORKERS, GW_API_KEY, GW_FLARESOLVERR, GW_PROXY_FILE,
     GW_PAGE_TTL, GW_STREAM_TTL, GW_INDEX_TTL, GW_RATELIMIT, GW_CACHE_DIR,
     GW_PUBLIC_URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

import missav_parse as P

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

LANGS = ("en", "ja", "cn", "zh", "ko", "ms", "th", "de", "fr", "vi", "id", "fil", "pt")
SORTS = ("released_at", "published_at", "saved", "today_views", "weekly_views",
         "monthly_views", "views")
START = time.time()


@dataclass
class Settings:
    host: str = os.getenv("GW_HOST", "0.0.0.0")
    port: int = int(os.getenv("GW_PORT", "8100"))
    api_key: str = os.getenv("GW_API_KEY", "")
    public_url: str = os.getenv("GW_PUBLIC_URL", "").rstrip("/")
    page_ttl: int = int(os.getenv("GW_PAGE_TTL", "900"))          # 15 mnt
    stream_ttl: int = int(os.getenv("GW_STREAM_TTL", "1800"))     # 30 mnt
    index_ttl: int = int(os.getenv("GW_INDEX_TTL", "21600"))      # 6 jam
    ratelimit: int = int(os.getenv("GW_RATELIMIT", "240"))        # req/menit/IP
    page_timeout: float = float(os.getenv("GW_PAGE_TIMEOUT", "25"))
    flaresolverr: str = os.getenv("GW_FLARESOLVERR", "")
    proxy_file: str = os.getenv("GW_PROXY_FILE", "")
    fs_timeout: float = float(os.getenv("GW_FS_TIMEOUT", "120"))
    fs_concurrency: int = int(os.getenv("GW_FS_CONCURRENCY", "3"))
    proxy_attempts: int = int(os.getenv("GW_PROXY_ATTEMPTS", "6"))
    proxy_timeout: int = int(os.getenv("GW_PROXY_TIMEOUT", "12"))
    cache_dir: str = os.getenv("GW_CACHE_DIR", "/opt/missav/cache")
    mirrors: tuple = ("https://missav.ws", "https://missav.ai")
    ua: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    # section browse yang sudah diverifikasi ada di nav situs
    sections: dict = field(default_factory=lambda: {
        "new": "new",
        "recent-update": "new",
        "release": "release",
        "new-releases": "release",
        "uncensored-leak": "uncensored-leak",
        "english-subtitle": "english-subtitle",
        "chinese-subtitle": "chinese-subtitle",
        "today-hot": "today-hot",
        "weekly-hot": "weekly-hot",
        "monthly-hot": "monthly-hot",
        "vr": "genres/VR",
        "siro": "siro", "luxu": "luxu", "gana": "gana", "maan": "maan",
        "scute": "scute", "ara": "ara",
        "fc2": "fc2", "heyzo": "heyzo", "tokyohot": "tokyohot",
        "1pondo": "1pondo", "caribbeancom": "caribbeancom",
        "caribbeancompr": "caribbeancompr", "10musume": "10musume",
        "pacopacomama": "pacopacomama", "gachinco": "gachinco",
        "xxxav": "xxxav", "marriedslash": "marriedslash",
        "naughty4610": "naughty4610", "naughty0930": "naughty0930",
        "madou": "madou", "twav": "twav", "furuke": "furuke",
        "klive": "klive", "clive": "clive",
    })


S = Settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("missav-gw")

app = FastAPI(
    title="missav-gw",
    version="2.0.0",
    description="API gateway missav.ws: search, detail, streaming HLS, download, "
                "browse, genre/actress/maker index.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)


# --------------------------------------------------------------------------- #
# Middleware: API key (opsional) + rate limit
# --------------------------------------------------------------------------- #

_PUBLIC_PATHS = ("/health", "/", "/docs", "/openapi.json", "/redoc",
                 "/dashboard", "/favicon.ico")

# Path streaming: tidak dihitung ke rate limit (satu video = ~1.800 segmen).
_STREAM_PATHS = ("/proxy/", "/api/v1/proxy/", "/api/v1/stream/",
                 "/api/v1/download/", "/api/v1/image")


class GateMiddleware(BaseHTTPMiddleware):
    """Rate limit + API key opsional. State disimpan di class-level supaya
    instance yang dibuat Starlette dan yang dipakai endpoint /stats adalah
    objek yang sama (add_middleware membuat instance baru sendiri).

    PENTING: request segmen/stream TIDAK dihitung. Satu video punya ~1.800
    segmen, jadi kalau dihitung, menonton satu video saja langsung menghabiskan
    limit dan semua request lain (resolve, search) kena 429. Pemutaran punya
    pembatas sendiri berupa bandwidth, bukan hitungan request.
    """

    buckets: dict[str, list[float]] = {}
    hits = 0
    blocked = 0

    def __init__(self, app):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        cls = type(self)
        if S.api_key:
            supplied = request.headers.get("x-api-key") or request.query_params.get("api_key")
            if supplied != S.api_key:
                cls.blocked += 1
                return JSONResponse({"detail": "invalid or missing api key"}, status_code=401)
        # Streaming tidak di-rate-limit: jumlah segmen ribuan per video.
        if path.startswith(_STREAM_PATHS):
            cls.hits += 1
            return await call_next(request)
        ip = request.client.host if request.client else "?"
        now = time.time()
        window = [t for t in cls.buckets.setdefault(ip, []) if now - t < 60]
        if len(window) >= S.ratelimit:
            cls.blocked += 1
            return JSONResponse(
                {"detail": "rate limited", "limit": S.ratelimit,
                 "retry_after": max(1, int(60 - (now - window[0])) if window else 1)},
                status_code=429,
                headers={"Retry-After": str(max(1, int(60 - (now - window[0])) if window else 1))})
        window.append(now)
        cls.buckets[ip] = window
        cls.hits += 1
        if len(cls.buckets) > 5000:          # buang bucket IP yang sudah dingin
            for k in [k for k, v in cls.buckets.items() if not v or now - v[-1] > 120]:
                cls.buckets.pop(k, None)
        return await call_next(request)


app.add_middleware(GateMiddleware)


# --------------------------------------------------------------------------- #
# Cache: memori + disk (TTL) + single-flight
# --------------------------------------------------------------------------- #

class Cache:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self.mem: dict[str, tuple[float, object]] = {}
        self.hits = 0
        self.misses = 0
        self._inflight: dict[str, asyncio.Future] = {}

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, hashlib.sha1(key.encode()).hexdigest() + ".json")

    def get(self, key: str):
        ent = self.mem.get(key)
        if ent and time.time() < ent[0]:
            self.hits += 1
            return ent[1]
        if ent:
            self.mem.pop(key, None)
        try:
            with open(self._path(key)) as f:
                data = json.load(f)
            if data.get("exp", 0) > time.time():
                self.mem[key] = (data["exp"], data["data"])
                self.hits += 1
                return data["data"]
            os.unlink(self._path(key))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            pass
        self.misses += 1
        return None

    def put(self, key: str, value, ttl: int):
        exp = time.time() + ttl
        self.mem[key] = (exp, value)
        try:
            tmp = self._path(key) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"exp": exp, "data": value}, f)
            os.replace(tmp, self._path(key))
        except (OSError, TypeError) as e:
            log.debug("disk cache write failed: %s", e)
        if len(self.mem) > 4000:
            now = time.time()
            for k in [k for k, v in self.mem.items() if v[0] < now][:2000]:
                self.mem.pop(k, None)

    async def get_or_set(self, key: str, ttl: int, producer):
        """Single-flight: request identik yang bersamaan berbagi satu fetch."""
        hit = self.get(key)
        if hit is not None:
            return hit
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await producer()
            self.put(key, value, ttl)
            fut.set_result(value)
            return value
        except BaseException as e:
            fut.set_exception(e)
            raise
        finally:
            self._inflight.pop(key, None)

    def stats(self) -> dict:
        files = 0
        size = 0
        try:
            for n in os.listdir(self.dir):
                if n.endswith(".json"):
                    files += 1
                    size += os.path.getsize(os.path.join(self.dir, n))
        except OSError:
            pass
        return {"mem_entries": len(self.mem), "disk_files": files,
                "disk_bytes": size, "hits": self.hits, "misses": self.misses}


CACHE = Cache(S.cache_dir)


# --------------------------------------------------------------------------- #
# Upstream fetcher (FlareSolverr -> proxy pool -> direct curl_cffi)
# --------------------------------------------------------------------------- #

class _Resp:
    """Adaptor minimal menyerupai httpx.Response untuk hasil curl_cffi."""

    def __init__(self, r):
        self.status_code = r.status_code
        self.text = r.text
        self.content = r.content
        self.headers = dict(r.headers or {})


class Upstream:
    def __init__(self):
        from curl_cffi.requests import AsyncSession
        self._session_cls = AsyncSession
        self._sessions: dict[int, object] = {}
        self._proxies: list[str] = []
        self._good: list[str] = []
        self._bad: dict[str, float] = {}
        self._pi = 0
        if S.proxy_file and os.path.exists(S.proxy_file):
            self._proxies = [l.strip() for l in open(S.proxy_file) if ":" in l and l.strip()]
            log.info("proxy pool: %d", len(self._proxies))
        self._fs_sem = asyncio.Semaphore(S.fs_concurrency)
        self._fs_ok = 0
        self._fs_fail = 0

    # -- sessions ---------------------------------------------------------- #
    def session(self):
        """curl_cffi AsyncSession terikat event loop -> satu session per loop."""
        loop = asyncio.get_running_loop()
        sess = self._sessions.get(id(loop))
        if sess is None:
            sess = self._session_cls(impersonate="chrome", timeout=S.proxy_timeout)
            self._sessions[id(loop)] = sess
        return sess

    def _headers(self, referer: str = "https://missav.ws/") -> dict:
        return {
            "User-Agent": S.ua,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": referer,
            "Origin": "https://missav.ws",
        }

    # -- proxy pool -------------------------------------------------------- #
    def _candidates(self, n: int) -> list[Optional[dict]]:
        now = time.time()
        pool = [p for p in self._good if p in self._proxies]
        rest = [p for p in self._proxies if p not in pool and self._bad.get(p, 0) < now]
        if not rest:
            rest = [p for p in self._proxies if p not in pool]
        order = pool + rest
        if not order:
            return [None]
        picked = [order[(self._pi + i) % len(order)] for i in range(min(n, len(order)))]
        self._pi += len(picked)
        return [{"http": "http://" + p, "https": "http://" + p} for p in picked]

    def _mark(self, px: Optional[dict], ok: bool):
        p = (px or {}).get("http", "").replace("http://", "")
        if not p:
            return
        if ok:
            if p in self._good:
                self._good.remove(p)
            self._good.insert(0, p)
            del self._good[20:]
            self._bad.pop(p, None)
        else:
            self._bad[p] = time.time() + 300

    # -- FlareSolverr ------------------------------------------------------ #
    async def fs_get(self, url: str) -> Optional[str]:
        if not S.flaresolverr:
            return None
        async with self._fs_sem:
            try:
                async with httpx.AsyncClient(timeout=S.fs_timeout) as c:
                    r = await c.post(f"{S.flaresolverr}/v1", json={
                        "cmd": "request.get", "url": url,
                        "maxTimeout": int(S.fs_timeout * 1000) - 5000})
                    data = r.json()
                if data.get("status") != "ok":
                    self._fs_fail += 1
                    log.warning("fs %s: %s", url, data.get("message"))
                    return None
                body = (data.get("solution") or {}).get("response") or ""
                if len(body) > 4000:
                    self._fs_ok += 1
                    return body
                self._fs_fail += 1
                return None
            except Exception as e:
                self._fs_fail += 1
                log.warning("fs error %s: %s", url, e)
                return None

    # -- halaman missav ---------------------------------------------------- #
    async def page(self, url: str, validate=None) -> str:
        """Ambil HTML halaman missav. Urutan: FlareSolverr -> proxy pool -> direct.

        `validate(html) -> bool` menolak halaman "shell" (nav saja) yang kadang
        dikembalikan proxy publik, supaya tidak di-cache sebagai hasil valid.
        """
        errors = []
        html = await self.fs_get(url)
        if html and (validate is None or validate(html)):
            return html
        if html:
            errors.append("fs: konten tidak valid")
        for px in self._candidates(S.proxy_attempts):
            try:
                r = await self.session().get(url, headers=self._headers(), proxies=px,
                                             timeout=S.proxy_timeout)
                if r.status_code == 200 and len(r.text) > 4000 and (validate is None or validate(r.text)):
                    self._mark(px, True)
                    return r.text
                self._mark(px, False)
                errors.append(f"proxy {px}: {r.status_code}")
            except Exception as e:
                self._mark(px, False)
                errors.append(f"proxy {px}: {e}")
        try:
            r = await self.session().get(url, headers=self._headers(), timeout=S.page_timeout)
            if r.status_code == 200 and (validate is None or validate(r.text)):
                return r.text
            errors.append(f"direct: {r.status_code}")
        except Exception as e:
            errors.append(f"direct: {e}")
        raise HTTPException(502, {"message": "upstream missav tidak bisa diakses",
                                  "url": url, "attempts": errors[-4:]})

    # -- CDN surrit / fourhoi ---------------------------------------------- #
    async def cdn(self, url: str, extra_headers: Optional[dict] = None,
                  timeout: Optional[float] = None) -> _Resp:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        r = await self.session().get(url, headers=headers,
                                     timeout=timeout or S.proxy_timeout)
        return _Resp(r)

    async def aclose(self):
        for sess in self._sessions.values():
            try:
                await sess.close()
            except Exception:
                pass


UP = Upstream()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _abs_base(request: Request) -> str:
    """Base URL absolut untuk URI di dalam playlist."""
    if S.public_url:
        return S.public_url
    return str(request.base_url).rstrip("/")


def _proxied(url: str, base: str, prefix: str = "/api/v1") -> str:
    kind = "m3u8" if ".m3u8" in url.split("?")[0] else "segment"
    return f"{base}{prefix}/proxy/{kind}?u={quote(url, safe='')}"


def _lang(lang: str) -> str:
    return lang if lang in LANGS else "en"


def _sort(sort: Optional[str]) -> Optional[str]:
    return sort if sort in SORTS else None


def _host_ok(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return (host == "surrit.com" or host.endswith(".surrit.com")
            or host.endswith("missav.ws") or host.endswith("missav.ai")
            or host == "fourhoi.com" or host.endswith(".fourhoi.com"))


async def expand_master(master_url: str) -> tuple[dict[str, str], Optional[str]]:
    """Master playlist -> ({label: url variant}, url variant terbaik).

    Beberapa master hanya berisi satu varian 720p/1080p; label diambil dari
    RESOLUTION asli supaya kualitas yang ditampilkan tidak menyesatkan.
    """
    out: dict[str, str] = {}
    best_url, best_score = None, -1
    try:
        r = await UP.cdn(master_url)
        if r.status_code != 200:
            return out, None
    except Exception:
        return out, None
    if "#EXT-X-STREAM-INF" not in r.text:
        # sudah variant playlist (bukan master)
        return out, master_url
    base = master_url.rsplit("/", 1)[0]
    lines = r.text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line)
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
        if not nxt:
            continue
        url = nxt if nxt.startswith("http") else f"{base}/{nxt.lstrip('/')}"
        score = int(bw.group(1)) if bw else (int(m.group(1)) if m else 0)
        if score > best_score:
            best_url, best_score = url, score
        if m:
            label = f"{m.group(2)}p"
        elif bw:
            label = f"{int(bw.group(1)) // 1000}kbps"
        else:
            label = f"variant{len(out) + 1}"
        out[label] = url
    return out, best_url


def rewrite_playlist(body: str, playlist_url: str, base: str,
                     suffix: str = "", prefix: str = "/api/v1",
                     relative: bool = False) -> str:
    """Ubah semua URI (variant, segment, EXT-X-KEY/MAP) menjadi endpoint proxy.

    `suffix` ditempelkan di belakang query (mis. "seg.ts") supaya ffmpeg mau
    menerima URI yang ekstensinya .jpeg (host surrit menyamarkan segmen TS
    sebagai .jpeg; whitelist ekstensi di hls.c tidak bisa diakali selain ini).
    `relative=True` menghasilkan path relatif ("/proxy/...") — dipakai endpoint
    legacy yang masih di-rewrite oleh missav_bot.py.
    """
    origin = urlparse(playlist_url)
    root = f"{origin.scheme}://{origin.netloc}"
    pl_dir = playlist_url.rsplit("/", 1)[0]
    tail = f"&x={suffix}" if suffix else ""
    base = "" if relative else base

    def absolute(ref: str) -> str:
        if ref.startswith("http"):
            return ref
        if ref.startswith("/"):
            return root + ref
        return f"{pl_dir}/{ref}"

    def proxied(ref: str) -> str:
        if relative:
            kind = "m3u8" if ".m3u8" in ref.split("?")[0] else "segment"
            return f"/proxy/{kind}?u={quote(ref, safe='')}"
        return _proxied(ref, base, prefix) + tail

    out_lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
            continue
        if s.startswith("#"):
            out_lines.append(re.sub(
                r'URI="([^"]+)"',
                lambda m: 'URI="' + proxied(absolute(m.group(1))) + '"', line))
        else:
            out_lines.append(proxied(absolute(s)))
    return "\n".join(out_lines)


async def listing(url: str, ttl: Optional[int] = None) -> dict:
    """Fetch + parse halaman listing (search/genre/actress/maker/section)."""

    def validate(html: str) -> bool:
        return ('class="thumbnail group"' in html or "No results" in html
                or "Not Found" in html)

    async def produce():
        html = await UP.page(url, validate=validate)
        data = P.parse_listing(html)
        data["source"] = url
        return data

    return await CACHE.get_or_set("listing:" + url, ttl or S.page_ttl, produce)


async def detail_page(code: str, lang: str = "en") -> tuple[str, dict]:
    """Fetch + parse halaman video. Return (html, detail)."""
    url = f"https://missav.ws/{_lang(lang)}/{code}"

    def is_404(html: str) -> bool:
        return bool(re.search(r"<h1[^>]*>\s*404\s*</h1>", html)) or \
            "Please check the URL in the address bar" in html

    def validate(html: str) -> bool:
        ok = any(k in html for k in ('id="video-details"', 'property="og:video:duration"',
                                     "<span>Code:</span>", "Not Found"))
        if not ok:
            return False
        # Halaman HARUS memuat kode yang diminta. Proxy publik kadang
        # mengembalikan halaman valid milik video LAIN; tanpa cek ini,
        # detail video lain bisa ikut ter-cache di bawah kode yang diminta.
        want = code.lower()
        return want in html.lower()

    html = await UP.page(url, validate=validate)
    if is_404(html):
        raise HTTPException(404, f"video '{code}' tidak ditemukan")
    d = P.parse_detail(html, code)
    if not d.get("duration") and not d.get("actresses") and not d.get("genres") \
            and not P.extract_sources(html):
        raise HTTPException(404, f"video '{code}' tidak ditemukan / tidak punya sumber stream")
    return html, d


async def resolve_streams(html: str, code: str, lang: str = "en") -> dict:
    """Sumber m3u8 -> {label: url}, terurut dari kualitas tertinggi.

    Halaman hanya memberi satu URL master (var `source`); kualitas nyata ada di
    dalam master sebagai #EXT-X-STREAM-INF RESOLUTION.
    """
    raw = P.extract_sources(html)
    ranked = P.rank_sources(raw)
    master = ranked.get("auto") or next(iter(ranked.values()), None)
    if not master:
        return {}
    variants, best = await expand_master(master)
    if variants:
        merged = dict(variants)
        for k, v in ranked.items():               # tambahkan label eksplisit (1080p/720p)
            if k not in ("auto",) and k not in merged:
                merged[k] = v
        ordered = sorted(merged.items(), key=lambda kv: -int(re.sub(r"\D", "", kv[0]) or 0))
        out = {k: v for k, v in ordered}
        if best:
            out["auto"] = best
        return out
    if best:
        ranked["auto"] = best
    return ranked


async def get_video(code: str, lang: str = "en", related: bool = True) -> dict:
    """Detail video (cache 30 mnt) — juga dipakai endpoint legacy."""
    code = code.strip("/").lower()

    async def produce():
        html, d = await detail_page(code, lang)
        streams = await resolve_streams(html, code, lang)
        rel = []
        if related:
            rel = [it for it in P.parse_items(html) if it["code"] != code][:24]
        return {
            **d,
            "qualities": list(streams.keys()),
            "streams": streams,
            "stream_url": f"/api/v1/stream/{code}",
            "download_url": f"/api/v1/download/{code}",
            "related": rel,
            "source": f"https://missav.ws/{_lang(lang)}/{code}",
        }

    return await CACHE.get_or_set(f"video:{code}:{_lang(lang)}:{int(related)}",
                                  S.stream_ttl, produce)


# --------------------------------------------------------------------------- #
# Root + health
# --------------------------------------------------------------------------- #

@app.get("/", tags=["meta"], summary="Info gateway + daftar endpoint")
async def root():
    return {
        "service": "missav-gw",
        "version": "2.0.0",
        "upstream": "https://missav.ws",
        "docs": "/docs",
        "dashboard": "/dashboard",
        "endpoints": {
            "search": "/api/v1/search?q=&page=1&sort=&lang=en",
            "video": "/api/v1/video/{code}",
            "stream": "/api/v1/stream/{code}?quality=720p",
            "download": "/api/v1/download/{code}?quality=720p",
            "browse": "/api/v1/browse/{section}?page=1&sort=",
            "sections": "/api/v1/sections",
            "genres": "/api/v1/genres?page=1",
            "genre_listing": "/api/v1/genres/{slug}?page=1",
            "makers": "/api/v1/makers?page=1",
            "maker_listing": "/api/v1/makers/{slug}?page=1",
            "actresses": "/api/v1/actresses?page=1&cup=&height=&age=&debut=",
            "actress_listing": "/api/v1/actress/{slug}?page=1",
            "tags": "/api/v1/tags/{slug}?page=1",
            "related": "/api/v1/related/{code}",
            "player": "/api/v1/player/{code}",
            "random": "/api/v1/random",
            "image": "/api/v1/image?u=",
            "stats": "/api/v1/stats",
            "legacy": ["/resolve/{code}", "/search?q=", "/proxy/m3u8?u=",
                       "/proxy/segment?u=", "/download/{code}"],
        },
    }


@app.get("/health", tags=["meta"], summary="Health check")
async def health():
    return {"status": "ok", "time": int(time.time()), "cache": CACHE.stats(),
            "flaresolverr": bool(S.flaresolverr),
            "flaresolverr_ok": UP._fs_ok, "flaresolverr_fail": UP._fs_fail}


DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@app.get("/dashboard", tags=["meta"], response_class=HTMLResponse,
         summary="Dashboard web: overview, search, browse, player, API tester")
async def dashboard():
    try:
        with open(DASHBOARD_FILE, encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})
    except FileNotFoundError:
        raise HTTPException(404, "dashboard.html tidak ditemukan di direktori aplikasi")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Search & browse
# --------------------------------------------------------------------------- #

@app.get("/api/v1/search", tags=["katalog"], summary="Cari video")
async def api_search(
    q: str = Query(..., min_length=1, max_length=120, description="kata kunci / kode"),
    page: int = Query(1, ge=1, le=500),
    sort: Optional[str] = Query(None, description="|".join(SORTS)),
    lang: str = Query("en"),
):
    url = f"https://missav.ws/{_lang(lang)}/search/{quote(q)}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    data = await listing(url)
    return {"query": q, "page": page, "sort": _sort(sort), **data}


@app.get("/api/v1/sections", tags=["katalog"], summary="Daftar section browse")
async def api_sections():
    return {"count": len(S.sections),
            "sections": [{"name": k, "path": v, "url": f"/api/v1/browse/{k}"}
                         for k, v in S.sections.items()]}


@app.get("/api/v1/browse/{section}", tags=["katalog"], summary="Listing per section")
async def api_browse(
    section: str,
    page: int = Query(1, ge=1, le=2000),
    sort: Optional[str] = Query(None, description="|".join(SORTS)),
    filters: Optional[str] = Query(None, description="individual|multiple|english-subtitle"),
    lang: str = Query("en"),
):
    path = S.sections.get(section.lower())
    if not path:
        raise HTTPException(404, f"section '{section}' tidak dikenal; lihat /api/v1/sections")
    url = f"https://missav.ws/{_lang(lang)}/{path}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    if filters:
        url += f"&filters={quote(filters)}"
    data = await listing(url)
    return {"section": section, "page": page, "sort": _sort(sort),
            "filters": filters, **data}


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

@app.get("/api/v1/video/{code}", tags=["video"], summary="Detail video + stream + related")
async def api_video(code: str, lang: str = Query("en"),
                    related: bool = Query(True, description="sertakan video terkait")):
    return await get_video(code, lang, related)


@app.get("/api/v1/related/{code}", tags=["video"], summary="Video terkait saja")
async def api_related(code: str, limit: int = Query(24, ge=1, le=60)):
    code = code.strip("/").lower()
    v = await get_video(code, related=True)
    rel = [it for it in v.get("related", []) if it["code"] != code][:limit]
    return {"code": code, "count": len(rel), "items": rel}


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

@app.get("/api/v1/stream/{code}", tags=["streaming"],
         summary="Playlist HLS (URI sudah diarahkan ke gateway)")
async def api_stream(
    code: str,
    request: Request,
    quality: Optional[str] = Query(None, description="mis. 720p / 1080p / auto"),
    ext: str = Query("", max_length=12,
                     description="ekstensi palsu segmen utk ffmpeg (mis. seg.ts)"),
    lang: str = Query("en"),
):
    code = code.strip("/").lower()
    base = _abs_base(request)

    async def produce():
        html, _ = await detail_page(code, lang)
        streams = await resolve_streams(html, code, lang)
        if not streams:
            raise HTTPException(404, "sumber stream tidak ditemukan (layout berubah / geo-block)")
        return streams

    streams = await CACHE.get_or_set(f"streams:{code}:{_lang(lang)}", S.stream_ttl, produce)

    if quality and quality not in streams and quality not in ("auto", "best"):
        raise HTTPException(400, f"kualitas '{quality}' tidak ada; pilihan: {list(streams)}")
    if not quality or quality in ("auto", "best"):
        target = streams.get("auto") or next(v for k, v in streams.items() if k != "auto")
    else:
        target = streams[quality]

    try:
        r = await UP.cdn(target, timeout=S.page_timeout)
    except Exception as e:
        raise HTTPException(502, f"gagal ambil playlist: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"upstream playlist {r.status_code}")

    body = rewrite_playlist(r.text, target, base, suffix=ext)
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "public, max-age=60",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/api/v1/proxy/m3u8", tags=["streaming"], summary="Proxy playlist m3u8")
async def api_proxy_m3u8(u: str, request: Request,
                         ext: str = Query("", max_length=12,
                                          description="ekstensi palsu utk ffmpeg, mis. seg.ts")):
    url = unquote(u)
    if not _host_ok(url):
        raise HTTPException(400, "host tidak diizinkan")
    try:
        r = await UP.cdn(url, timeout=S.page_timeout)
    except Exception as e:
        raise HTTPException(502, f"upstream fetch failed: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"upstream {r.status_code}")
    body = rewrite_playlist(r.text, url, _abs_base(request), suffix=ext)
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "public, max-age=60",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/api/v1/proxy/segment", tags=["streaming"], summary="Proxy segment TS (Range OK)")
async def api_proxy_segment(u: str, request: Request):
    url = unquote(u)
    if not _host_ok(url):
        raise HTTPException(400, "host tidak diizinkan")
    extra = {}
    if "range" in request.headers:
        extra["Range"] = request.headers["range"]
    try:
        r = await UP.cdn(url, extra_headers=extra, timeout=30)
    except Exception as e:
        raise HTTPException(502, f"segment fetch failed: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"segment upstream {r.status_code}")
    body = r.content
    if not body:
        raise HTTPException(502, "segment kosong dari upstream")
    ctype = "video/mp4" if url.split("?")[0].endswith((".mp4", ".m4s")) else "video/mp2t"
    headers = {"Cache-Control": "public, max-age=86400, immutable",
               "Access-Control-Allow-Origin": "*",
               "Accept-Ranges": "bytes"}
    return Response(content=body, media_type=ctype, headers=headers,
                    status_code=206 if "range" in request.headers else 200)


@app.get("/api/v1/download/{code}", tags=["streaming"], summary="Remux MP4 via ffmpeg")
async def api_download(
    code: str,
    request: Request,
    quality: str = Query("720p", description="kualitas; fallback ke yang tersedia"),
    lang: str = Query("en"),
):
    code = code.strip("/").lower()
    html, _ = await detail_page(code, lang)
    streams = await resolve_streams(html, code, lang)
    if not streams:
        raise HTTPException(404, "sumber stream tidak ditemukan")
    url = streams.get(quality) or next(iter(streams.values()))
    if "surrit.com" not in url:
        raise HTTPException(502, "host stream tidak dikenal")
    fname = re.sub(r"[^a-zA-Z0-9_.-]", "_", code) + f"_{quality}.mp4"
    # lewat proxy sendiri (curl_cffi impersonate) supaya lolos CF.
    # &ext=seg.ts: ffmpeg menolak URI tanpa ekstensi yang dikenal (.jpeg).
    local = (f"http://127.0.0.1:{S.port}/api/v1/proxy/m3u8"
             f"?u={quote(url, safe='')}&ext=seg.ts")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-allowed_extensions", "ALL",
        "-i", local,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def stream():
        try:
            while True:
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()

    return StreamingResponse(stream(), media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Access-Control-Allow-Origin": "*"})


# --------------------------------------------------------------------------- #
# Index: genre / maker / actress / tag
# --------------------------------------------------------------------------- #

@app.get("/api/v1/genres", tags=["index"], summary="Daftar genre")
async def api_genres(page: int = Query(1, ge=1, le=100), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/genres?page={page}"

    async def produce():
        html = await UP.page(url, validate=lambda h: "/genres/" in h and "videos" in h)
        items = P.parse_genres(html)
        return {"count": len(items), "items": items,
                "pagination": P.parse_pagination(html)}

    return {"page": page, **await CACHE.get_or_set("genres:" + url, S.index_ttl, produce)}


@app.get("/api/v1/genres/{slug}", tags=["index"], summary="Video per genre")
async def api_genre_listing(slug: str, page: int = Query(1, ge=1, le=2000),
                            sort: Optional[str] = Query(None), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/genres/{quote(slug)}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    return {"genre": slug, "page": page, **await listing(url)}


@app.get("/api/v1/makers", tags=["index"], summary="Daftar maker/studio")
async def api_makers(page: int = Query(1, ge=1, le=100), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/makers?page={page}"

    async def produce():
        html = await UP.page(url, validate=lambda h: "/makers/" in h and "videos" in h)
        items = P.parse_index(html, "makers")
        return {"count": len(items), "items": items,
                "pagination": P.parse_pagination(html)}

    return {"page": page, **await CACHE.get_or_set("makers:" + url, S.index_ttl, produce)}


@app.get("/api/v1/makers/{slug}", tags=["index"], summary="Video per maker")
async def api_maker_listing(slug: str, page: int = Query(1, ge=1, le=2000),
                            sort: Optional[str] = Query(None), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/makers/{quote(slug)}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    return {"maker": slug, "page": page, **await listing(url)}


@app.get("/api/v1/actresses", tags=["index"], summary="Daftar aktris (+filter)")
async def api_actresses(
    page: int = Query(1, ge=1, le=500),
    sort: Optional[str] = Query(None, description="videos|debut"),
    height: Optional[str] = Query(None, description="mis. 156-160"),
    cup: Optional[str] = Query(None, description="A..Q"),
    age: Optional[str] = Query(None, description="mis. 20-30"),
    debut: Optional[str] = Query(None, description="tahun, mis. 2020"),
    lang: str = Query("en"),
):
    url = f"https://missav.ws/{_lang(lang)}/actresses?page={page}"
    if sort in ("videos", "debut"):
        url += f"&sort={sort}"
    for k, v in (("height", height), ("cup", cup), ("age", age), ("debut", debut)):
        if v:
            url += f"&{k}={quote(v)}"

    async def produce():
        html = await UP.page(url, validate=lambda h: "/actresses/" in h)
        return {"count": len(P.parse_actresses(html)),
                "items": P.parse_actresses(html),
                "filters": P.parse_actress_filters(html),
                "pagination": P.parse_pagination(html)}

    data = await CACHE.get_or_set("actresses:" + url, S.index_ttl, produce)
    return {"page": page, "sort": sort,
            "filter": {"height": height, "cup": cup, "age": age, "debut": debut},
            **data}


@app.get("/api/v1/actress/{slug}", tags=["index"], summary="Video per aktris")
async def api_actress_listing(slug: str, page: int = Query(1, ge=1, le=2000),
                              sort: Optional[str] = Query(None), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/actresses/{quote(slug)}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    data = await listing(url)
    if not data["items"]:
        raise HTTPException(404, f"aktris '{slug}' tidak ditemukan atau tidak punya video")
    return {"actress": unquote(slug), "page": page, **data}


@app.get("/api/v1/tags/{slug}", tags=["index"], summary="Video per tag")
async def api_tag_listing(slug: str, page: int = Query(1, ge=1, le=2000),
                          sort: Optional[str] = Query(None), lang: str = Query("en")):
    url = f"https://missav.ws/{_lang(lang)}/tags/{quote(slug)}?page={page}"
    if _sort(sort):
        url += f"&sort={sort}"
    return {"tag": unquote(slug), "page": page, **await listing(url)}


# --------------------------------------------------------------------------- #
# Random, image proxy, player, stats
# --------------------------------------------------------------------------- #

@app.get("/api/v1/random", tags=["video"], summary="Video acak (dari section new)")
async def api_random(lang: str = Query("en")):
    page = random.randint(1, 300)
    data = await listing(f"https://missav.ws/{_lang(lang)}/new?page={page}")
    if not data["items"]:
        raise HTTPException(503, "tidak ada item")
    return {"video": random.choice(data["items"])}


@app.get("/api/v1/image", tags=["meta"], summary="Proxy gambar CDN (cover)")
async def api_image(u: str):
    url = unquote(u)
    if not urlparse(url).netloc.lower().endswith("fourhoi.com"):
        raise HTTPException(400, "hanya fourhoi.com yang diizinkan")
    try:
        r = await UP.cdn(url, timeout=20)
    except Exception as e:
        raise HTTPException(502, f"image fetch failed: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"upstream {r.status_code}")
    ctype = r.headers.get("content-type") or "image/jpeg"
    return Response(r.content, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


PLAYER_HTML = """<!doctype html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 :root{{color-scheme:dark}} body{{margin:0;background:#0f1115;color:#e6e6e6;
 font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
 header{{padding:12px 16px;border-bottom:1px solid #23262d}}
 h1{{font-size:15px;margin:0 0 4px;font-weight:600}}
 .meta{{color:#9aa0a6;font-size:12px}}
 #wrap{{max-width:1100px;margin:0 auto;padding:16px}}
 video{{width:100%;background:#000;border-radius:8px;aspect-ratio:16/9}}
 .bar{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}
 button{{background:#1c2027;color:#e6e6e6;border:1px solid #2c313a;border-radius:6px;
 padding:6px 12px;cursor:pointer;font-size:13px}}
 button.active{{background:#2d6cdf;border-color:#2d6cdf}}
 code{{color:#9fd0a0}}
</style></head><body>
<header><h1>{title}</h1><div class="meta">{code} &middot; {duration} &middot; {date}</div></header>
<div id="wrap">
  <video id="v" controls playsinline autoplay poster="{poster}"></video>
  <div class="bar" id="q"></div>
  <p class="meta">Stream: <code>{base}/api/v1/stream/{code}</code></p>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
const qualities = {qualities};
const code = "{code}", base = "{base}";
let hls = null;
function play(q) {{
  document.querySelectorAll('#q button').forEach(b =>
    b.classList.toggle('active', b.dataset.q === q));
  const url = `${{base}}/api/v1/stream/${{code}}?quality=${{q}}`;
  const v = document.getElementById('v');
  if (window.Hls && Hls.isSupported()) {{
    if (hls) hls.destroy();
    hls = new Hls({{maxBufferLength: 30}});
    hls.loadSource(url); hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, () => v.play());
    hls.on(Hls.Events.ERROR, (e, d) => {{ if (d && d.fatal) console.error('hls', d); }});
  }} else {{ v.src = url; v.play(); }}
}}
const bar = document.getElementById('q');
qualities.forEach((q, i) => {{
  const b = document.createElement('button');
  b.textContent = q; b.dataset.q = q; b.onclick = () => play(q);
  bar.appendChild(b);
  if (i === 0) play(q);
}});
</script></body></html>"""


@app.get("/api/v1/player/{code}", tags=["streaming"], response_class=HTMLResponse,
         summary="Halaman player hls.js siap tonton")
async def api_player(code: str, request: Request, lang: str = Query("en")):
    code = code.strip("/").lower()
    v = await get_video(code, lang, related=False)
    qualities = v.get("qualities") or ["auto"]
    return HTMLResponse(PLAYER_HTML.format(
        title=(v.get("title") or code).replace("<", "&lt;"),
        code=code,
        duration=v.get("duration_text") or "-",
        date=v.get("release_date") or "-",
        poster=v.get("image") or "",
        qualities=json.dumps(qualities),
        base=_abs_base(request),
    ))


@app.get("/api/v1/stats", tags=["meta"], summary="Statistik cache & upstream")
async def api_stats():
    return {
        "uptime_seconds": int(time.time() - START),
        "cache": CACHE.stats(),
        "requests": {"served": GateMiddleware.hits, "blocked": GateMiddleware.blocked},
        "upstream": {"flaresolverr_ok": UP._fs_ok, "flaresolverr_fail": UP._fs_fail,
                     "proxy_pool": len(UP._proxies), "proxy_good": len(UP._good)},
        "config": {"page_ttl": S.page_ttl, "stream_ttl": S.stream_ttl,
                   "index_ttl": S.index_ttl, "ratelimit": S.ratelimit,
                   "auth": bool(S.api_key)},
    }


# --------------------------------------------------------------------------- #
# Legacy endpoints (kompatibel dengan missav_bot.py versi lama)
# --------------------------------------------------------------------------- #

@app.get("/resolve/{code}", tags=["legacy"], summary="[legacy] metadata + streams")
async def legacy_resolve(code: str, lang: str = Query("en")):
    v = await get_video(code, lang, related=False)
    return {
        "code": v["code"],
        "meta": {"title": v.get("title"), "description": v.get("description"),
                 "image": v.get("image"), "duration": v.get("duration")},
        "qualities": v["qualities"],
        "streams": {q: {"direct": u,
                        "proxied": f"/proxy/m3u8?u={quote(u, safe='')}"}
                    for q, u in v["streams"].items()},
    }


@app.get("/search", tags=["legacy"], summary="[legacy] pencarian")
async def legacy_search(q: str = Query(..., min_length=1, max_length=120),
                        page: int = Query(1, ge=1, le=500)):
    data = await api_search(q=q, page=page, sort=None, lang="en")
    return {"query": q, "page": page, "count": data["count"],
            "results": [{"code": it["code"], "title": it["title"], "url": it["url"]}
                        for it in data["items"]]}


@app.get("/titles", tags=["legacy"], summary="[legacy] judul untuk daftar kode")
async def legacy_titles(codes: str = Query(..., max_length=600)):
    wanted = [c.strip().lower() for c in codes.split(",") if c.strip()][:12]
    out: dict[str, str] = {}

    async def one(c: str):
        try:
            v = await get_video(c, related=False)
            out[c] = v.get("title") or c
        except Exception:
            out[c] = c

    await asyncio.gather(*(one(c) for c in wanted))
    return {"titles": out}


@app.get("/proxy/m3u8", tags=["legacy"], summary="[legacy] proxy playlist (path relatif)")
async def legacy_proxy_m3u8(u: str, request: Request):
    """Seperti /api/v1/proxy/m3u8, tapi URI ditulis relatif ("/proxy/...") agar
    missav_bot.py bisa me-rewrite ke URL player publiknya."""
    url = unquote(u)
    if not _host_ok(url):
        raise HTTPException(400, "host tidak diizinkan")
    try:
        r = await UP.cdn(url, timeout=S.page_timeout)
    except Exception as e:
        raise HTTPException(502, f"upstream fetch failed: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"upstream {r.status_code}")
    body = rewrite_playlist(r.text, url, "", relative=True)
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "public, max-age=60",
                             "Access-Control-Allow-Origin": "*"})


@app.get("/proxy/segment", tags=["legacy"], summary="[legacy] proxy segment")
async def legacy_proxy_segment(u: str, request: Request):
    return await api_proxy_segment(u, request)


@app.get("/download/{code}", tags=["legacy"], summary="[legacy] download MP4")
async def legacy_download(code: str, request: Request, q: str = Query("720p", max_length=8)):
    return await api_download(code, request, quality=q, lang="en")


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

@app.on_event("startup")
async def on_startup():
    log.info("missav-gw 2.0.0 up on :%s (cache=%s, fs=%s, proxies=%d)",
             S.port, S.cache_dir, bool(S.flaresolverr), len(UP._proxies))


@app.on_event("shutdown")
async def on_shutdown():
    await UP.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("missav_gateway:app", host=S.host, port=S.port,
                workers=int(os.getenv("GW_WORKERS", "2")))
