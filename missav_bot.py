"""
missav-gw bot — Telegram bot streaming via gateway.

Konsep:
- User kirim kode (mis. fc2-ppv-4985048), link missav, atau kata kunci.
- Hasil pencarian tampil sebagai daftar bernomor (kode + durasi + cuplikan
  judul) dengan tombol interaktif per item — satu pesan, di-edit, tidak spam.
- Kartu video memuat detail dari /api/v1/video: judul, kode, durasi, tanggal
  rilis, aktris, genre, maker, tag.
- Tombol kualitas membuka web player (hls.js) yang streaming via gateway.

Jalankan: python3 missav_bot.py
Env: TG_BOT_TOKEN (wajib), GW_BASE (default http://localhost:8100),
     GW_PUBLIC_BASE (opsional, untuk link download publik),
     PLAYER_PUBLIC_URL (default http://<ip-host>:8200), PLAYER_PORT (8200),
     PLAYER_URL_FILE (default /opt/missav/player_url.txt)
"""
from __future__ import annotations

import asyncio
import html as html_escape
import json
import logging
import os
import re
import socket
import time
import urllib.parse
import uuid

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiohttp import web

# --------------------------------------------------------------------------- #

GW_BASE = os.getenv("GW_BASE", "http://localhost:8100").rstrip("/")
GW_PUBLIC_BASE = os.getenv("GW_PUBLIC_BASE", "").rstrip("/")
# Situs punya terjemahan judul per bahasa (en/id/ja/cn/...). Referensi bot
# memakai judul Indonesia, jadi 'id' jadi default; kalau kosong -> fallback en.
BOT_LANG = os.getenv("BOT_LANG", "id").strip().lower()
PLAYER_PORT = int(os.getenv("PLAYER_PORT", "8200"))
PLAYER_PUBLIC_URL = os.getenv("PLAYER_PUBLIC_URL", "").rstrip("/")
TOKEN = os.getenv("TG_BOT_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("missav-bot")

client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
dp = Dispatcher()

PAGE_SIZE = 5           # 5 item/halaman -> halaman 5 mulai dari nomor 21
SNIPPET = 58            # panjang cuplikan judul di daftar
BTN_CODE = 20           # panjang kode di label tombol


async def _gw_get(url: str, **kw):
    """GET ke gateway dengan 2x percobaan (gateway kadang lambat saat FS antre)."""
    last = None
    for _ in range(2):
        try:
            return await client.get(url, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
    raise last


def e(s) -> str:
    """Escape untuk parse_mode=HTML. Kode video sering mengandung '_'
    (mis. musume-102026_01) yang merusak parse_mode=Markdown."""
    return html_escape.escape(str(s if s is not None else ""), quote=False)


# --------------------------------------------------------------------------- #
# Gateway client
# --------------------------------------------------------------------------- #


_NAV_SLUGS = {
    "en", "cn", "ja", "ko", "ms", "th", "de", "fr", "vi", "id", "fil", "pt", "zh",
    "tags", "tag", "genres", "genre", "makers", "maker", "actresses", "actress",
    "search", "people", "vip", "new", "release", "uncensored-leak", "saved",
    "playlists", "history", "klive", "clive", "today-hot", "weekly-hot",
    "monthly-hot", "ranking", "login", "register", "dmca", "terms",
}


def extract_code(text: str) -> str | None:
    """Ambil kode video dari teks/link. Kata bebas ('sekolah') BUKAN kode —
    itu harus jalan ke pencarian, bukan resolve yang pasti 404."""
    text = text.strip()
    # URL missav: /en/<kode>, /dm159/en/<kode>, /cn/<kode>
    m = re.search(
        r"missav\.[a-z]+/(?:dm\d+/)?(?:[a-z]{2}(?:\d+)?/)?([a-z0-9][a-z0-9_.-]{3,})",
        text, re.I)
    if m:
        code = m.group(1).lower().rstrip(".-_")
        # buang path navigasi (/tags/, /genres/, slug bahasa, ...)
        if code not in _NAV_SLUGS:
            return code
        return None
    t = text.lower()
    if re.fullmatch(r"fc2-ppv-\d{5,}", t):
        return t
    if re.fullmatch(r"[a-z]{2,6}-\d{2,5}([a-z0-9_-]{0,24})?", t):
        return t
    return None


async def gw_search(q: str, page: int = 1, sort: str | None = None) -> dict | None:
    """Cari di locale BOT_LANG. Fallback ke 'en' HANYA kalau request gagal —
    himpunan item sama di semua locale (hanya judulnya diterjemahkan), jadi
    hasil kosong tidak perlu diulang (menghemat ~12 detik per pencarian)."""
    for lang in dict.fromkeys([BOT_LANG, "en"]):
        params = {"q": q, "page": page, "lang": lang}
        if sort:
            params["sort"] = sort
        try:
            r = await _gw_get(f"{GW_BASE}/api/v1/search", params=params)
            if r.status_code != 200:
                log.warning("search %r hal %s lang %s -> %s", q, page, lang, r.status_code)
                continue
            return r.json()
        except Exception as exc:
            log.warning("search %r lang %s gagal: %s", q, lang, exc)
    return None


async def gw_browse(section: str, page: int = 1) -> dict | None:
    try:
        r = await _gw_get(f"{GW_BASE}/api/v1/browse/{section}",
                          params={"page": page, "lang": BOT_LANG})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def gw_genre(slug: str, page: int = 1) -> dict | None:
    try:
        r = await _gw_get(f"{GW_BASE}/api/v1/genres/{urllib.parse.quote(slug)}",
                          params={"page": page, "lang": BOT_LANG})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def gw_video(code: str) -> dict | None:
    try:
        r = await _gw_get(f"{GW_BASE}/api/v1/video/{code}",
                          params={"lang": BOT_LANG})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def gw_random() -> dict | None:
    try:
        r = await _gw_get(f"{GW_BASE}/api/v1/random")
        if r.status_code != 200:
            return None
        return r.json().get("video")
    except Exception:
        return None


async def gw_genres() -> list:
    try:
        r = await _gw_get(f"{GW_BASE}/api/v1/genres")
        if r.status_code != 200:
            return []
        return r.json().get("items", [])
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Sesi hasil (token pendek -> hindari batas 64 byte callback_data)
# --------------------------------------------------------------------------- #

_sessions: dict[str, dict] = {}
_MAX_SESSIONS = 300


def _new_session(kind: str, title: str, items: list, page: int, **extra) -> str:
    if len(_sessions) > _MAX_SESSIONS:                 # buang yang paling lama
        for k in sorted(_sessions, key=lambda x: _sessions[x]["ts"])[:100]:
            _sessions.pop(k, None)
    tok = uuid.uuid4().hex[:8]
    _sessions[tok] = {"kind": kind, "title": title, "items": items,
                      "page": page, "ts": time.time(), **extra}
    return tok


def _get_session(tok: str) -> dict | None:
    s = _sessions.get(tok)
    if s:
        s["ts"] = time.time()
    return s


# --------------------------------------------------------------------------- #
# Format pesan
# --------------------------------------------------------------------------- #


def _clean_title(title: str, code: str) -> str:
    """Buang prefix kode di judul ('HEYZO-3960 Judul…') supaya daftar tidak
    menampilkan kode dua kali."""
    t = (title or "").strip()
    if not t:
        return ""
    up, cu = t.upper(), (code or "").upper()
    for prefix in (cu, cu.replace("-UNCENSORED-LEAK", ""),
                   cu.replace("-CHINESE-SUBTITLE", ""),
                   cu.replace("-ENGLISH-SUBTITLE", "")):
        if prefix and up.startswith(prefix):
            t = t[len(prefix):].lstrip(" -–—:·|")
            break
    return t


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[:n].rstrip(" ,;:-–—") + "…"


def _fmt_item(n: int, it: dict) -> str:
    code = (it.get("code") or "").upper()
    dur = it.get("duration_text")
    head = f"{n}. {code}" + (f" ({dur})" if dur else "")
    snip = _clip(_clean_title(it.get("title"), it.get("code") or ""), SNIPPET)
    out = f"<b>{e(head)}</b>"
    if snip:
        out += f"\n└ {e(snip)}"
    return out


def _results_text(title: str, page: int, items: list) -> str:
    start = (page - 1) * PAGE_SIZE
    body = "\n".join(_fmt_item(start + i + 1, it) for i, it in enumerate(items))
    return f"🔍 <b>Hasil MissAV: {e(title)}</b> <i>(Hal. {page})</i>\n\n{body}"


def _results_kb(tok: str, items: list, page: int,
                total_pages: int | None) -> InlineKeyboardMarkup:
    start = (page - 1) * PAGE_SIZE
    rows, row = [], []
    for i, it in enumerate(items):
        n = start + i + 1
        code = (it.get("code") or "").upper()
        label = code if len(code) <= BTN_CODE else code[:BTN_CODE] + "…"
        row.append(InlineKeyboardButton(text=f"#{n} {label}",
                                        callback_data=f"p:{tok}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="« Prev", callback_data=f"s:{tok}:{page-1}"))
    if total_pages is None or page < total_pages:
        nav.append(InlineKeyboardButton(text="Next »", callback_data=f"s:{tok}:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _gw_public() -> str:
    """Base URL gateway yang bisa dijangkau browser user (untuk link download)."""
    if GW_PUBLIC_BASE:
        return GW_PUBLIC_BASE
    base = _public_base()                       # mis. http://203.0.113.10:8200
    host = urllib.parse.urlsplit(base).hostname
    port = "8100"
    tail = GW_BASE.rsplit("/", 1)[-1]
    if ":" in tail and tail.rsplit(":", 1)[-1].isdigit():
        port = tail.rsplit(":", 1)[-1]
    return f"http://{host}:{port}" if host else GW_BASE


def _card_text(v: dict) -> str:
    code = (v.get("code") or "").upper()
    raw = v.get("title") or code
    # Kode sudah tampil di baris 🆔, jadi prefix kode di judul dibuang.
    title = _clean_title(raw, v.get("code") or "") or raw
    lines = [f"🎬 <b>{e(title)}</b>", "", f"🆔 <code>{e(code)}</code>"]

    facts = []
    if v.get("duration_text"):
        facts.append(f"⏱ {e(v['duration_text'])}")
    if v.get("release_date"):
        facts.append(f"📅 {e(v['release_date'])}")
    if facts:
        lines.append("   ".join(facts))

    act = [a.get("name") for a in (v.get("actresses") or []) if a.get("name")]
    if act:
        shown = ", ".join(act[:3]) + (f" +{len(act) - 3}" if len(act) > 3 else "")
        lines.append(f"👤 {e(shown)}")

    gen = [g.get("name") for g in (v.get("genres") or []) if g.get("name")]
    if gen:
        shown = ", ".join(gen[:6]) + (f" +{len(gen) - 6}" if len(gen) > 6 else "")
        lines.append(f"🏷 {e(shown)}")

    if v.get("maker"):
        lines.append(f"🏢 {e(v['maker'])}")

    tags = [t.get("name") for t in (v.get("tags") or []) if t.get("name")]
    if tags:
        shown = ", ".join(tags[:6]) + (f" +{len(tags) - 6}" if len(tags) > 6 else "")
        lines.append(f"🔖 {e(shown)}")

    desc = (v.get("description") or "").strip()
    # Situs mengisi og:description dengan slogan generik; jangan tampilkan
    # sebagai deskripsi video — itu informasi palsu.
    if desc and not desc.lower().startswith("best japan av porn site"):
        lines += ["", f"📝 {e(_clip(desc, 220))}"]

    qs = v.get("qualities") or []
    if qs:
        lines += ["", f"🎞 <i>Kualitas: {e(', '.join(qs))}</i>"]
    return "\n".join(lines)


def _card_kb(v: dict, back: str | None) -> InlineKeyboardMarkup:
    code = v.get("code") or ""
    public = _public_base()
    qs = [q for q in (v.get("qualities") or []) if q != "auto"] or (v.get("qualities") or [])

    rows, row = [], []
    for q in qs:
        row.append(InlineKeyboardButton(
            text=f"▶️ {q}",
            url=f"{public}/p/{code}?q={urllib.parse.quote(q)}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    best = qs[0] if qs else "720p"
    rows.append([InlineKeyboardButton(
        text=f"⬇️ Download {best}",
        url=f"{_gw_public()}/api/v1/download/{urllib.parse.quote(code)}"
            f"?quality={urllib.parse.quote(best)}")])
    rows.append([InlineKeyboardButton(
        text="🌐 Buka di missav", url=f"https://missav.ws/en/{code}")])
    if back:
        rows.append([InlineKeyboardButton(text="🔙 Kembali ke hasil",
                                          callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Pengiriman hasil (satu pesan, di-edit — tidak spam)
# --------------------------------------------------------------------------- #


async def _emit(msg: Message, text: str, kb=None, edit: bool = True) -> Message:
    """Tampilkan teks di pesan `msg`, atau pesan baru kalau edit tidak mungkin.

    SELALU kembalikan Message yang benar-benar hidup. Ini krusial: kalau
    `msg` berupa foto, pesannya dihapus dan diganti yang baru — memakai
    referensi lama setelah itu akan gagal diam-diam ("message not found")
    dan membuat UI nyangkut di placeholder.

    PENTING: tidak memakai msg.reply(), karena bergantung pada pesan asli
    masih ada. Setelah dihapus, reply gagal dengan
    "message to be replied not found".
    """
    # 1. Pesan teks biasa -> edit in place (paling hemat, tanpa pesan baru)
    if not msg.photo:
        if edit:
            try:
                await msg.edit_text(text, parse_mode="HTML", reply_markup=kb,
                                    disable_web_page_preview=True)
                return msg
            except TelegramBadRequest as exc:
                # "message is not modified" = isi sudah sama, anggap sukses
                if "not modified" in str(exc).lower():
                    return msg
                log.warning("edit teks gagal: %s", exc)
            except Exception as exc:
                log.warning("edit teks gagal: %s", exc)
        return await _send(msg, text, kb)

    # 2. Pesan foto -> kirim teks baru, hapus foto, kembalikan pesan baru
    try:
        new_msg = await msg.bot.send_message(
            chat_id=msg.chat.id, text=text, parse_mode="HTML",
            reply_markup=kb, disable_web_page_preview=True)
        try:
            await msg.delete()
        except Exception:
            pass
        return new_msg
    except Exception as exc:
        log.warning("foto->teks gagal: %s", exc)
        return await _send(msg, text, kb)


async def _send(msg: Message, text: str, kb=None) -> Message:
    """Kirim pesan baru (fallback kalau pesan lama sudah tidak bisa diedit)."""
    try:
        return await msg.answer(text, parse_mode="HTML", reply_markup=kb,
                                disable_web_page_preview=True)
    except Exception:
        return await msg.bot.send_message(
            chat_id=msg.chat.id, text=text, parse_mode="HTML",
            reply_markup=kb, disable_web_page_preview=True)


async def _fetch_photo_bytes(photo: str) -> bytes | None:
    """Unduh byte gambar lewat gateway (allowlist fourhoi) sebagai cadangan."""
    try:
        r = await client.get(f"{GW_BASE}/api/v1/image",
                             params={"u": photo}, timeout=30)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as exc:
        log.warning("unduh poster gagal: %s", exc)
    return None


async def _emit_photo(msg: Message, photo: str, caption: str,
                      kb=None, edit: bool = True) -> Message:
    """Kartu video sebagai FOTO + caption. Kembalikan pesan yang hidup.

    Telegram tidak bisa mengubah pesan teks menjadi foto (edit_media hanya
    berlaku bila sumbernya sudah media). Jadi:
      - pesan sudah foto  -> edit_media (caption berubah, tetap 1 pesan)
      - pesan masih teks  -> kirim foto, hapus teks lama (anti-spam)
    Kalau URL poster ditolak, byte-nya diunduh lewat gateway lalu diunggah.
    """
    body = caption[:1024]                     # batas caption Telegram
    from aiogram.types import BufferedInputFile, InputMediaPhoto

    if msg.photo and edit:
        try:
            await msg.edit_media(
                media=InputMediaPhoto(media=photo, caption=body, parse_mode="HTML"),
                reply_markup=kb)
            return msg
        except Exception as exc:
            log.warning("edit_media URL gagal: %s", exc)
        raw = await _fetch_photo_bytes(photo)
        if raw:
            try:
                await msg.edit_media(
                    media=InputMediaPhoto(
                        media=BufferedInputFile(raw, filename="cover.jpg"),
                        caption=body, parse_mode="HTML"),
                    reply_markup=kb)
                return msg
            except Exception as exc:
                log.warning("edit_media unggah gagal: %s", exc)

    # Kirim foto baru (pesan sumber teks, atau edit_media gagal semua)
    sent = None
    try:
        sent = await msg.answer_photo(photo, caption=body, parse_mode="HTML",
                                      reply_markup=kb)
        log.info("poster terkirim via URL: %s", photo)
    except Exception as exc:
        log.warning("answer_photo URL gagal (%s) — coba unggah byte", exc)
        raw = await _fetch_photo_bytes(photo)
        if raw:
            try:
                sent = await msg.answer_photo(
                    BufferedInputFile(raw, filename="cover.jpg"),
                    caption=body, parse_mode="HTML", reply_markup=kb)
                log.info("poster terkirim via unggah byte (%d B)", len(raw))
            except Exception as exc2:
                log.warning("answer_photo unggah gagal: %s", exc2)

    if sent is None:                          # poster gagal total -> kirim teks
        return await _send(msg, caption, kb)

    # Rapatkan: buang pesan lama supaya tidak ada duplikat
    if edit:
        try:
            await msg.delete()
        except Exception:
            pass
    return sent


async def run_search(msg: Message, q: str, page: int = 1,
                     edit: bool = True) -> Message:
    """Cari lalu tampilkan daftar. Placeholder di-edit, bukan pesan baru.

    Mengembalikan Message yang hidup agar pemanggil bisa mengeditnya lagi.
    """
    t0 = time.monotonic()
    data = await gw_search(q, page)
    ms = int((time.monotonic() - t0) * 1000)

    if not data:
        log.info("search %r hal %s GAGAL %dms", q, page, ms)
        return await _emit(msg, "❌ Pencarian gagal. Coba lagi sebentar.",
                           edit=edit)

    items = (data.get("items") or [])[:PAGE_SIZE]
    log.info("search %r hal %s -> %d item %dms", q, page, len(items), ms)
    if not items:
        return await _emit(
            msg, f"🔍 Tidak ada hasil untuk <b>{e(q)}</b>. Coba kata kunci lain.",
            edit=edit)

    total = (data.get("pagination") or {}).get("total_pages")
    tok = _new_session("search", q, items, page, total_pages=total)
    return await _emit(msg, _results_text(q, page, items),
                       _results_kb(tok, items, page, total), edit=edit)


async def show_card(msg: Message, code: str, back: str | None,
                    edit: bool) -> Message:
    """Tampilkan kartu video (poster + detail). Kembalikan pesan yang hidup."""
    t0 = time.monotonic()
    v = await gw_video(code)
    ms = int((time.monotonic() - t0) * 1000)
    log.info("video %s -> %s %dms", code, "ok" if v else "gagal", ms)

    if not v or not v.get("qualities"):
        return await _emit(
            msg, f"❌ <code>{e(code.upper())}</code> tidak ditemukan "
                 f"atau tidak punya sumber stream.", edit=edit)

    text, kb = _card_text(v), _card_kb(v, back)
    # Poster: cover-n.jpg (800x450). Fallback ke cover-t.jpg.
    photo = v.get("image") or \
        f"https://fourhoi.com/{(v.get('code') or code).lower()}/cover-n.jpg"
    return await _emit_photo(msg, photo, text, kb, edit=edit)


async def show_browse(msg: Message, title: str, data: dict | None,
                      section: str, page: int) -> Message:
    if not data or not data.get("items"):
        return await _emit(msg, "❌ Tidak ada item untuk section ini.")
    items = data["items"][:PAGE_SIZE]
    total = (data.get("pagination") or {}).get("total_pages")
    tok = _new_session("browse", title, items, page, section=section,
                       total_pages=total)
    return await _emit(msg, _results_text(title, page, items),
                       _results_kb(tok, items, page, total))


# --------------------------------------------------------------------------- #
# Web player (aiohttp) — halaman HLS player memakai gateway proxy
# --------------------------------------------------------------------------- #

PLAYER_TMPL = """<!DOCTYPE html>
<html lang="id"><head>
<meta charset="utf-8">
<!-- viewport-fit=cover: dukung notch/takik iPhone -->
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e14">
<meta name="color-scheme" content="dark">
<title>{title} — missav player</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<style>
 :root{{
   --bg:#0b0e14; --panel:#1c2333; --line:#2a3350; --txt:#e7eaf0; --dim:#9aa3b2;
   --accent:#4f7cff; --radius:10px;
   /* Fluid: skala dari HP kecil (320px) sampai layar lebar */
   --pad:clamp(8px, 2.2vw, 20px);
   --gap:clamp(6px, 1.4vw, 10px);
   --btn-pad-y:clamp(9px, 1.6vw, 13px);
   --btn-pad-x:clamp(10px, 2.4vw, 18px);
   --btn-font:clamp(12.5px, 0.95vw + 7px, 15px);
   --title-font:clamp(13px, 1.1vw + 8px, 17px);
   --safe-b:env(safe-area-inset-bottom, 0px);
   --safe-l:env(safe-area-inset-left, 0px);
   --safe-r:env(safe-area-inset-right, 0px);
 }}
 *{{box-sizing:border-box}}
 html,body{{margin:0;padding:0}}
 body{{
   background:var(--bg); color:var(--txt);
   font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
   -webkit-text-size-adjust:100%;
   -webkit-tap-highlight-color:transparent;
   overflow-x:hidden;
 }}
 .wrap{{
   width:100%; max-width:1200px; margin:0 auto;
   padding:var(--pad);
   padding-left:max(var(--pad), var(--safe-l));
   padding-right:max(var(--pad), var(--safe-r));
   padding-bottom:max(var(--pad), var(--safe-b));
 }}
 /* ---- video ---- */
 .stage{{position:relative; width:100%}}
 video{{
   width:100%; height:auto; display:block;
   background:#000; border-radius:var(--radius);
   aspect-ratio:16/9; object-fit:contain;
   /* sisakan ruang untuk judul + tombol + padding, supaya tidak perlu scroll */
   max-height:calc(100vh - 235px);
 }}

 /* ---- judul ---- */
 h1{{
   font-size:var(--title-font); font-weight:600; color:var(--dim);
   margin:calc(var(--gap) + 2px) 0 0; line-height:1.45;
   display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
   overflow:hidden; word-break:break-word;
 }}
 @media(min-width:768px){{
   h1{{-webkit-line-clamp:3; margin-top:calc(var(--gap) + 4px)}}
 }}

 /* ---- tombol lompat: grid rapi yang mengikuti lebar layar ---- */
 .skip{{
   display:grid; gap:var(--gap); margin-top:calc(var(--gap) + 2px);
   grid-template-columns:repeat(3, minmax(0,1fr));   /* HP: 3 kolom -> 2 baris */
 }}
 @media(min-width:768px){{
   .skip{{grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px}}
 }}
 @media(min-width:1200px){{
   .skip{{grid-template-columns:repeat(6, minmax(88px,max-content)); justify-content:start}}
 }}
 .skip button{{
   display:flex; align-items:center; justify-content:center;
   width:100%; min-height:44px;                 /* target sentuh nyaman (WCAG) */
   padding:var(--btn-pad-y) var(--btn-pad-x);
   background:var(--panel); color:var(--txt);
   border:1px solid var(--line); border-radius:8px;
   font:inherit; font-size:var(--btn-font); font-weight:500;
   font-variant-numeric:tabular-nums;
   cursor:pointer; user-select:none;
   transition:background .15s, border-color .15s, transform .08s;
   touch-action:manipulation;
 }}
 .skip button:hover{{background:#273153; border-color:var(--accent); color:#fff}}
 .skip button:active{{transform:scale(.97); background:#2d3f66}}
 .skip button:focus-visible{{outline:2px solid var(--accent); outline-offset:2px}}

 /* tombol "maju" sedikit lebih menonjol */
 .skip button.fwd{{border-color:#33507f}}
 .skip button.fwd:hover{{background:#2d4a7a}}

 /* ---- pesan error / kodec ---- */
 #err{{
   display:none; margin-top:var(--gap);
   color:#ff8b8b; font-size:clamp(12px,0.8vw + 8px,13.5px);
   line-height:1.55; word-break:break-word;
   background:#1a1010; border:1px solid #4a2020; border-radius:8px;
   padding:10px 12px;
 }}
 #err code{{display:inline-block; margin-top:6px; color:#9fd0a0;
   background:#0d1117; padding:3px 7px; border-radius:5px; word-break:break-all}}

 /* ---- toast ---- */
 #toast{{
   position:absolute; right:10px; top:10px;
   background:rgba(12,16,24,.92); color:#fff;
   border:1px solid rgba(255,255,255,.16); border-radius:8px;
   padding:5px 11px; font-size:clamp(11.5px,0.7vw + 8px,12.5px);
   font-variant-numeric:tabular-nums; pointer-events:none;
   opacity:0; transform:translateY(-4px);
   transition:opacity .2s, transform .2s;
 }}
 #toast.on{{opacity:1; transform:translateY(0)}}

 /* ---- lanskap HP: tombol jadi 1 baris rapat, video tetap terlihat ---- */
 @media(max-height:480px) and (orientation:landscape){{
   .wrap{{padding:6px; max-width:100%; height:100vh; display:flex;
          flex-direction:column; overflow:hidden}}
   .stage{{flex:0 0 auto; min-height:0}}
   video{{max-height:calc(100vh - 74px); border-radius:6px}}
   h1{{display:none}}                 /* judul disembunyikan agar video maksimal */
   .skip{{grid-template-columns:repeat(6, minmax(0,1fr)); gap:5px;
          margin-top:6px; flex:0 0 auto}}
   .skip button{{min-height:44px; padding:6px 4px;
     font-size:clamp(11px,1.6vh,13px); border-radius:6px}}
   #err{{flex:0 0 auto; margin-top:4px; padding:6px 8px}}
 }}
 @media(max-height:400px) and (orientation:landscape){{
   video{{max-height:calc(100vh - 66px)}}
   .skip button{{min-height:40px}}
   .skip{{margin-top:4px}}
 }}
</style></head><body>
<div class="wrap">
  <div class="stage" id="stage">
    <video id="v" controls autoplay playsinline preload="metadata"></video>
    <div id="toast"></div>
  </div>
  <h1>{title}</h1>
  <div class="skip" id="skip">
    <button data-skip="-600" title="Mundur 10 menit" aria-label="Mundur 10 menit">« 10m</button>
    <button data-skip="-60" title="Mundur 1 menit" aria-label="Mundur 1 menit">« 1m</button>
    <button data-skip="-10" title="Mundur 10 detik" aria-label="Mundur 10 detik">« 10s</button>
    <button data-skip="10" class="fwd" title="Maju 10 detik" aria-label="Maju 10 detik">10s »</button>
    <button data-skip="60" class="fwd" title="Maju 1 menit" aria-label="Maju 1 menit">1m »</button>
    <button data-skip="600" class="fwd" title="Maju 10 menit" aria-label="Maju 10 menit">10m »</button>
  </div>
  <div id="err"></div>
</div>
<script>
const DATA = {variants};
/* DATA.map berisi pasangan nama-kualitas -> url; DATA.code = kode video */
const variants = DATA.map || DATA;
const CODE = DATA.code || '';
const Q = (new URLSearchParams(location.search).get('q') || 'auto');
const CUR = variants[Q] ? Q : Object.keys(variants)[0];
const HLS_URL = '/gw/proxy/m3u8?u=' + encodeURIComponent(variants[CUR]);
const MP4_URL = '/mp4/' + encodeURIComponent(CODE) + '?q=' + CUR;
const v = document.getElementById('v');
const toast = document.getElementById('toast');
let hls = null, toastTimer = null;

/* Catatan: JANGAN pakai MediaSource.isTypeSupported('avc1...') sebagai
   syarat boleh memutar. Di iOS nilainya false, padahal Safari sanggup
   memutar H.264 lewat HLS native. Guard seperti itu pernah membuat
   iPhone menampilkan "codec tidak didukung" dan video tidak bisa diputar. */

function showErr(html) {{
  const el = document.getElementById('err');
  el.style.display = 'block';
  el.innerHTML = html;
}}
function fmt(sec) {{
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0');
}}
function flash(text) {{
  toast.textContent = text;
  toast.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('on'), 1100);
}}
/* Lompat relatif; kalau durasi belum diketahui (masih buffering),
   simpan permintaan dan terapkan saat metadata siap. */
let pendingSeek = null;
function skip(delta) {{
  if (!isFinite(v.duration) || v.duration === 0) {{
    pendingSeek = (pendingSeek || 0) + delta;
    flash('menunggu buffer…');
    return;
  }}
  const target = Math.min(Math.max(0, v.currentTime + delta),
                          Math.max(0, v.duration - 0.5));
  v.currentTime = target;
  const label = Math.abs(delta) >= 60 ? (delta / 60) + 'm' : delta + 's';
  flash((delta > 0 ? '+' : '') + label + ' → ' + fmt(target));
}}
document.querySelectorAll('.skip button').forEach(b => {{
  b.addEventListener('click', e => {{
    e.preventDefault(); e.stopPropagation();
    skip(parseInt(b.dataset.skip, 10));
  }});
}});
/* Pintasan keyboard: ← → 10 detik, Shift+← → 1 menit, J/L 10 detik */
document.addEventListener('keydown', e => {{
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'ArrowLeft')  {{ skip(e.shiftKey ? -60 : -10); e.preventDefault(); }}
  if (e.key === 'ArrowRight') {{ skip(e.shiftKey ?  60 :  10); e.preventDefault(); }}
  if (e.key === 'j' || e.key === 'J') skip(-10);
  if (e.key === 'l' || e.key === 'L') skip(10);
}});
/* Terapkan seek tertunda setelah durasi tersedia */
v.addEventListener('durationchange', () => {{
  if (pendingSeek !== null && isFinite(v.duration) && v.duration > 0) {{
    const d = pendingSeek; pendingSeek = null;
    skip(d);
  }}
}});

function play(src) {{
  if (hls) {{ try {{ hls.destroy(); }} catch(e) {{}} hls = null; }}
  if (window.Hls && Hls.isSupported()) {{
    hls = new Hls({{ maxBufferLength: 30, manifestLoadingTimeOut: 45000,
      manifestLoadingMaxRetry: 6, manifestLoadingRetryDelay: 1000,
      levelLoadingTimeOut: 45000, fragLoadingTimeOut: 45000 }});
    hls.loadSource(src);
    hls.attachMedia(v);
    hls.on(Hls.Events.ERROR, (e, d) => {{
      if (d.fatal) tryFallback('hlsjs-fatal-' + d.details);
    }});
  }} else {{
    v.src = src;
    v.load();
  }}
}}

/* =================================================================== *
 * Pemutaran berlapis — TIDAK ada guard kodek yang memblokir.
 * Penting: di iOS, MediaSource.isTypeSupported('avc1...') = false,
 * tetapi Safari bisa memutar H.264 lewat HLS native. Jadi jangan
 * memakai isTypeSupported sebagai syarat boleh-tidaknya memutar.
 *
 *   1. hls.js (MSE)    -> Chrome, Edge, Firefox, Safari desktop
 *   2. HLS native      -> Safari iOS/macOS, browser TV
 *   3. MP4 progresif   -> jaring pengaman terakhir (Range/seek)
 * =================================================================== */
let tried = {{ hlsjs: false, native: false, mp4: false }};
let watchdog = null;

function clearWatch() {{ if (watchdog) {{ clearTimeout(watchdog); watchdog = null; }} }}

function deepPlay() {{
  const p = v.play();
  if (p && p.catch) p.catch(() => {{}});
}}

function tryNative() {{
  if (tried.native) return Promise.resolve(false);
  tried.native = true;
  return new Promise(res => {{
    let done = false;
    const finish = ok => {{ if (done) return; done = true; clearTimeout(t); res(ok); }};
    const t = setTimeout(() => finish(false), 12000);
    const onOk = () => finish(true);
    v.addEventListener('loadedmetadata', onOk, {{ once: true }});
    v.addEventListener('canplay', onOk, {{ once: true }});
    v.addEventListener('error', () => finish(false), {{ once: true }});
    v.src = HLS_URL;
    v.load();
    deepPlay();
  }});
}}

function tryMp4() {{
  tried.mp4 = true;
  v.src = MP4_URL;
  v.load();
  deepPlay();
}}

async function tryFallback(reason) {{
  clearWatch();
  if (hls) {{ try {{ hls.destroy(); }} catch(e) {{}} hls = null; }}
  if (!tried.native) {{
    const ok = await tryNative();
    if (ok) return;
  }}
  if (!tried.mp4) tryMp4();
}}

v.addEventListener('loadedmetadata', clearWatch);
v.addEventListener('canplay', clearWatch);
v.addEventListener('error', () => {{
  const c = v.error ? v.error.code : 0;
  // Safari tidak selalu mengisi v.error saat HLS native gagal, jadi
  // tetap turun ke lapis berikutnya alih-alih menampilkan error.
  if (!tried.mp4) {{
    clearWatch();
    if (hls) {{ try {{ hls.destroy(); }} catch(e) {{}} hls = null; }}
    if (!tried.native) {{ tryNative().then(ok => {{ if (!ok && !tried.mp4) tryMp4(); }}); }}
    else tryMp4();
  }}
}});

window.addEventListener('load', () => {{
  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isSafari = /^((?!chrome|android|crios|fxios|edg).)*safari/i.test(ua);
  const hasMSE = !!(window.MediaSource || window.ManagedMediaSource);

  if (isIOS) {{
    // iPhone/iPad: native HLS selalu tersedia dan paling andal.
    // Jangan lewat hls.js walau isSupported() bilang true (kerap menipu).
    tried.hlsjs = true;
    tryNative().then(ok => {{ if (!ok) tryMp4(); }});
  }} else if (hasMSE && window.Hls && Hls.isSupported()) {{
    tried.hlsjs = true;
    play(HLS_URL);
  }} else {{
    tryNative().then(ok => {{ if (!ok) tryMp4(); }});
  }}

  // Kalau macet (tidak ada progress), turun lapis otomatis.
  clearWatch();
  watchdog = setTimeout(() => {{
    if (v.readyState < 2) {{
      if (!tried.native) tryFallback('macet');
      else if (!tried.mp4 && !isIOS) tryMp4();
    }}
  }}, 15000);
}});
</script>
</body></html>
"""

PLAYER_URL_FILE = os.getenv("PLAYER_URL_FILE", "/opt/missav/player_url.txt")


def _public_base() -> str:
    # URL tunnel bisa berubah tiap restart cloudflared; file ini di-refresh cron
    # supaya tombol player selalu menunjuk alamat yang hidup.
    try:
        if os.path.exists(PLAYER_URL_FILE):
            u = open(PLAYER_URL_FILE).read().strip()
            if u.startswith("http"):
                return u.rstrip("/")
    except Exception:
        pass
    if PLAYER_PUBLIC_URL:
        return PLAYER_PUBLIC_URL
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{ip}:{PLAYER_PORT}"


async def player_handler(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    q = request.query.get("q", "auto")
    try:
        r = await _gw_get(f"{GW_BASE}/resolve/{code}",
                          params={"lang": BOT_LANG})
        data = r.json()
        if r.status_code != 200 or "streams" not in data:
            return await _error_page(request, 404,
                                     f"Video “{code}” tidak ditemukan")
    except Exception as exc:
        log.warning("player %s: gateway error %s", code, exc)
        return await _error_page(request, 502, "Gateway tidak merespons")
    try:
        streams = data.get("streams", {})
        meta = data.get("meta", {}) or {}
        # Judul: buang prefix kode ("IPZZ-935 Ayah tiriku..." -> "Ayah tiriku...")
        raw_title = str(meta.get("title") or code)
        clean = _clean_title(raw_title, code)
        title = html_escape.escape(clean or raw_title)
        variants = {qname: s["direct"] for qname, s in streams.items()}
        if not variants:
            return await _error_page(request, 404, "Video tidak punya sumber stream")
        # Kualitas datang dari tombol bot Telegram (?q=...). Player tidak punya
        # tombol resolusi sendiri, jadi cukup validasi & pakai varian itu.
        if q not in variants:
            q = next(iter(variants))
        page = PLAYER_TMPL.format(
            title=title,
            variants=json.dumps({"code": code, "map": variants}).replace("</", "<\\/"))
        return web.Response(text=page, content_type="text/html")
    except Exception as exc:
        log.exception("player %s gagal render: %s", code, exc)
        return web.Response(status=500, text=f"Gagal memuat player: {exc}")


async def gw_proxy_m3u8(request: web.Request) -> web.Response:
    """Passthrough ke gateway /proxy/m3u8, rewrite URI jadi path relatif.

    Dua penyesuaian penting untuk Safari/iOS:
    1. Path RELATIF (bukan absolut) — supaya tidak beda origin/CORS.
    2. Segmen memakai path berakhiran `.ts` (`/gw/seg/N.ts`). Safari native
       HLS menolak segmen tanpa ekstensi dikenal, dan ini satu-satunya jalur
       pemutaran di iPhone (iOS tidak punya MediaSource untuk hls.js).
    """
    u = request.query.get("u", "")
    r = await client.get(f"{GW_BASE}/proxy/m3u8", params={"u": u})
    body = r.text
    lines = []
    idx = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            lines.append(line)
            continue
        if line.startswith("#"):
            # EXT-X-KEY / EXT-X-MAP juga bisa menunjuk ke segmen
            if "URI=" in line:
                m = re.search(r'URI="([^"]+)"', line)
                if m and "/proxy/" in m.group(1):
                    new = _to_ts_path(m.group(1), idx)
                    idx += 1
                    line = line.replace(m.group(1), new)
            lines.append(line)
            continue
        # baris URI segmen
        if "/proxy/segment" in line or "/proxy/" in line:
            lines.append(_to_ts_path(line, idx))
            idx += 1
        elif "://" in line:
            m = re.match(r"https?://[^/]+(/proxy/\S+)", line)
            lines.append(_to_ts_path(m.group(1), idx) if m else line)
            idx += 1
        else:
            lines.append(line)
    return web.Response(text="\n".join(lines),
                        content_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store",
                                 "Access-Control-Allow-Origin": "*"})


def _to_ts_path(uri: str, idx: int) -> str:
    """Ubah URI segmen apa pun jadi /gw/seg/<n>.ts?u=<url-asli>."""
    q = ""
    if "?" in uri:
        uri, q = uri.split("?", 1)
    # q sudah terpisah dari '?', jadi pola tanpa [?&] di depan
    m = re.search(r"(?:^|&)u=([^&]+)", q)
    orig = m.group(1) if m else ""
    return f"/gw/seg/{idx}.ts" + (f"?u={orig}" if orig else "")


async def gw_seg_ts(request: web.Request) -> web.StreamResponse:
    """Segmen dengan path berakhiran .ts — diterima Safari native HLS."""
    return await gw_proxy_segment(request)


async def gw_mp4(request: web.Request) -> web.StreamResponse:
    """MP4 progresif yang bisa di-seek — jaring pengaman universal.

    Dipakai browser yang tidak bisa HLS/MSE (Safari iOS lawas, browser TV,
    WebView Android, dll). Server mengunduh & remux ke MP4 (cache per kode+
    kualitas), lalu menyajikan dengan dukungan HTTP Range supaya bisa
    di-seek dan tidak perlu menunggu unduhan penuh.
    """
    code = request.match_info["code"]
    q = request.query.get("q", "360p")
    cache_dir = os.getenv("PLAYER_CACHE_DIR", "/opt/missav/cache/player")
    os.makedirs(cache_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{code}_{q}")
    path = os.path.join(cache_dir, safe + ".mp4")
    lock_path = path + ".lock"

    if not os.path.exists(path):
        # hanya satu proses ffmpeg per file
        if os.path.exists(lock_path):
            return web.Response(status=409, text="Sedang disiapkan, coba lagi.")
        open(lock_path, "w").close()
        try:
            r = await client.get(f"{GW_BASE}/api/v1/download/{code}",
                                 params={"quality": q})
            if r.status_code != 200:
                return web.Response(status=502, text="Gagal menyiapkan MP4")
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, path)
        finally:
            try:
                os.remove(lock_path)
            except OSError:
                pass

    size = os.path.getsize(path)
    rng = request.headers.get("Range")
    start, end = 0, size - 1
    status = 200
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            status = 206

    length = end - start + 1
    tr = web.StreamResponse(status=status, headers={
        "Content-Type": "video/mp4",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    })
    if status == 206:
        tr.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    try:
        await tr.prepare(request)
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                await tr.write(chunk)
                left -= len(chunk)
        await tr.write_eof()
    except (ConnectionResetError, BrokenPipeError, ConnectionError,
            asyncio.CancelledError):
        return tr
    return tr


async def gw_proxy_segment(request: web.Request) -> web.StreamResponse:
    """Passthrough streaming ke gateway /proxy/segment."""
    u = request.query.get("u", "")
    rng = request.headers.get("Range")
    headers = {"Range": rng} if rng else {}
    async with client.stream("GET", f"{GW_BASE}/proxy/segment", params={"u": u},
                             headers=headers) as resp:
        if resp.status_code >= 400:
            return web.Response(status=502, text="upstream segment error")
        tr = web.StreamResponse(status=resp.status_code)
        tr.content_type = resp.headers.get("content-type", "video/mp2t")
        tr.headers["Cache-Control"] = "public, max-age=86400"
        try:
            await tr.prepare(request)
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            # Klien sudah pergi sebelum respons siap (umum saat seek/skip).
            # Bukan error: kembalikan tr tanpa menulis apa pun.
            return tr
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                await tr.write(chunk)
            await tr.write_eof()
        except (ConnectionResetError, BrokenPipeError, ConnectionError,
                asyncio.CancelledError):
            # Pemutar menutup koneksi (umum saat seek/skip). WAJIB kembalikan
            # `tr`, bukan bare return: aiohttp akan gagal dengan
            # "'NoneType' object has no attribute 'prepare'" dan mencatat 500.
            return tr
    return tr


async def _error_page(request: web.Request, status: int = 500,
                       message: str = "") -> web.Response:
    """Halaman error yang ramah, bukan teks '500 Internal Server Error' mentah."""
    html = f"""<!DOCTYPE html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gagal memuat — missav player</title>
<style>
 body{{margin:0;background:#0b0e14;color:#e7eaf0;
   font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
 .w{{max-width:560px;margin:12vh auto;padding:24px;background:#121722;
   border:1px solid #232b3d;border-radius:12px}}
 h1{{font-size:17px;margin:0 0 10px}}
 p{{color:#8b95a8;margin:0 0 14px}}
 code{{background:#1c2333;padding:2px 6px;border-radius:4px;font-size:12px}}
 a{{color:#4f7cff}}
</style></head><body><div class="w">
<h1>⚠️ {html_escape.escape(message or "Gagal memuat halaman")}</h1>
<p>Kode HTTP <code>{status}</code>. Coba lagi beberapa saat, atau buka video
langsung di missav.</p>
<p><a href="javascript:location.reload()">Muat ulang</a></p>
</div></body></html>"""
    return web.Response(text=html, status=status, content_type="text/html")


async def _on_error(app, handler):
    """Pasang handler error global: 404/500 tidak lagi berupa teks polos."""
    async def middleware(request: web.Request, *args, **kwargs):
        try:
            resp = await handler(request, *args, **kwargs)
        except web.HTTPException as exc:
            if exc.status in (404, 500):
                return await _error_page(request, exc.status,
                                         "Halaman tidak ditemukan"
                                         if exc.status == 404 else "Terjadi kesalahan")
            raise
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            # Klien memutus koneksi (seek/skip/refresh). Bukan error server —
            # jangan dicatat sebagai traceback.
            raise
        except Exception as exc:
            log.exception("unhandled error di %s: %s", request.path, exc)
            return await _error_page(request, 500, "Terjadi kesalahan internal")
        # JANGAN sentuh respons non-HTML: segmen video (.ts/.m3u8) harus lewat
        # apa adanya, kalau diganti HTML stream-nya rusak.
        ctype = (resp.content_type or "") if isinstance(resp, web.Response) else ""
        if isinstance(resp, web.Response) and resp.status >= 500 \
                and "text/html" in ctype:
            return await _error_page(request, resp.status, "Gagal memuat halaman")
        return resp
    return middleware


def build_app() -> web.Application:
    a = web.Application(middlewares=[_on_error])
    a.router.add_get("/p/{code}", player_handler)
    a.router.add_get("/mp4/{code}", gw_mp4)
    a.router.add_get("/gw/proxy/m3u8", gw_proxy_m3u8)
    a.router.add_get("/gw/proxy/segment", gw_proxy_segment)
    a.router.add_get("/gw/seg/{name}", gw_seg_ts)
    a.router.add_get("/", lambda r: web.Response(text="missav-gw player OK"))
    return a


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #

WELCOME = (
    "🎬 <b>missav streaming bot</b>\n\n"
    "Kirim <b>kode video</b> (mis. <code>fc2-ppv-4985048</code>), "
    "<b>link missav</b>, atau <b>kata kunci</b>.\n"
    "Hasil pencarian tampil sebagai daftar bernomor + tombol interaktif.\n\n"
    "<b>Perintah</b>\n"
    "/search &lt;kata kunci&gt; — cari judul\n"
    "/rand — video acak\n"
    "/hot — paling banyak ditonton hari ini\n"
    "/new — rilis terbaru\n"
    "/genres — jelajah per genre\n"
    "/help — bantuan ini"
)


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Hot hari ini", callback_data="sec:today-hot:1"),
         InlineKeyboardButton(text="🆕 Rilis terbaru", callback_data="sec:new:1")],
        [InlineKeyboardButton(text="🎭 Genre", callback_data="genlist:1"),
         InlineKeyboardButton(text="🎲 Acak", callback_data="rnd:1")],
    ])


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(WELCOME, parse_mode="HTML", reply_markup=_main_menu())


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await start(m)


@dp.message(Command("rand"))
async def rand(m: Message):
    wait = await m.answer("🎲 Mengambil video acak…")
    v = await gw_random()
    if not v:
        await _emit(wait, "❌ Gagal mengambil video acak. Coba lagi.")
        return
    await show_card(wait, v["code"], None, edit=True)


@dp.message(Command("hot"))
async def hot_cmd(m: Message):
    wait = await m.answer("⏳ Memuat paling banyak ditonton…")
    data = await gw_browse("today-hot", 1)
    await show_browse(wait, "🔥 Paling banyak ditonton", data, "today-hot", 1)


@dp.message(Command("new"))
async def new_cmd(m: Message):
    wait = await m.answer("⏳ Memuat rilis terbaru…")
    data = await gw_browse("new", 1)
    await show_browse(wait, "🆕 Rilis terbaru", data, "new", 1)


def _genre_kb(items: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for g in items[:24]:
        row.append(InlineKeyboardButton(text=_clip(g["name"], 18),
                                        callback_data=f"gen:{g['slug']}:1"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("genres"))
async def genres_cmd(m: Message):
    wait = await m.answer("⏳ Memuat daftar genre…")
    items = await gw_genres()
    if not items:
        await _emit(wait, "❌ Gagal memuat genre.")
        return
    await _emit(wait, "🎭 <b>Pilih genre</b>", _genre_kb(items))


@dp.message(Command("search"))
async def search_cmd(m: Message, command: CommandObject | None = None):
    q = (command.args or "").strip() if command else ""
    if not q:
        await m.answer("Pakai: <code>/search kata kunci</code>", parse_mode="HTML")
        return
    wait = await m.answer(f"🔍 Mencari <b>{e(q)}</b>…", parse_mode="HTML")
    await run_search(wait, q, 1, edit=True)


# --- callback: halaman hasil ------------------------------------------------ #

@dp.callback_query(F.data.startswith("s:"))
async def cb_page(cb: CallbackQuery):
    _, tok, page_s = cb.data.split(":")
    page = max(1, int(page_s))
    s = _get_session(tok)
    if not s:
        await cb.answer("Sesi kedaluwarsa, ulangi pencarian.", show_alert=True)
        return
    await cb.answer()
    # live = pesan yang benar-benar hidup; cb.message bisa sudah dihapus
    live = await _emit(cb.message, "⏳ Memuat halaman…")

    if s["kind"] == "search":
        data = await gw_search(s["title"], page)
    elif s["kind"] == "genre":
        data = await gw_genre(s["section"], page)
    else:
        data = await gw_browse(s["section"], page)

    if not data or not data.get("items"):
        await _emit(live, "❌ Halaman kosong.")
        return

    items = data["items"][:PAGE_SIZE]
    total = (data.get("pagination") or {}).get("total_pages")
    s["items"], s["page"], s["total_pages"] = items, page, total
    await _emit(live, _results_text(s["title"], page, items),
                _results_kb(tok, items, page, total))


# --- callback: pilih item --------------------------------------------------- #

@dp.callback_query(F.data.startswith("p:"))
async def cb_pick(cb: CallbackQuery):
    _, tok, idx_s = cb.data.split(":")
    s = _get_session(tok)
    if not s:
        await cb.answer("Sesi kedaluwarsa, ulangi pencarian.", show_alert=True)
        return
    i = int(idx_s)
    if i >= len(s["items"]):
        await cb.answer("Item tidak tersedia.", show_alert=True)
        return
    code = s["items"][i]["code"]
    await cb.answer("Memuat…")
    live = await _emit(cb.message, f"⏳ Memuat <code>{e(code.upper())}</code>…")
    await show_card(live, code, back=f"s:{tok}:{s['page']}", edit=True)


# --- callback: section & genre --------------------------------------------- #

@dp.callback_query(F.data.startswith("sec:"))
async def cb_section(cb: CallbackQuery):
    _, section, page_s = cb.data.split(":")
    page = max(1, int(page_s))
    await cb.answer()
    live = await _emit(cb.message, "⏳ Memuat…")
    data = await gw_browse(section, page)
    titles = {"today-hot": "🔥 Paling banyak ditonton", "new": "🆕 Rilis terbaru",
              "weekly-hot": "📈 Mingguan", "monthly-hot": "🗓 Bulanan",
              "uncensored-leak": "🔓 Uncensored leak"}
    await show_browse(live, titles.get(section, section), data, section, page)


@dp.callback_query(F.data.startswith("gen:"))
async def cb_genre(cb: CallbackQuery):
    _, slug, page_s = cb.data.split(":")
    page = max(1, int(page_s))
    await cb.answer()
    live = await _emit(cb.message, "⏳ Memuat…")
    data = await gw_genre(slug, page)
    if not data or not data.get("items"):
        await _emit(live, "❌ Genre kosong.")
        return
    items = data["items"][:PAGE_SIZE]
    total = (data.get("pagination") or {}).get("total_pages")
    title = f"🎭 Genre: {slug}"
    tok = _new_session("genre", title, items, page, section=slug, total_pages=total)
    await _emit(live, _results_text(title, page, items),
                _results_kb(tok, items, page, total))


@dp.callback_query(F.data.startswith("genlist:"))
async def cb_genlist(cb: CallbackQuery):
    await cb.answer()
    live = await _emit(cb.message, "⏳ Memuat daftar genre…")
    items = await gw_genres()
    if not items:
        await _emit(live, "❌ Gagal memuat genre.")
        return
    await _emit(live, "🎭 <b>Pilih genre</b>", _genre_kb(items))


@dp.callback_query(F.data.startswith("rnd:"))
async def cb_random(cb: CallbackQuery):
    await cb.answer("Mengambil video acak…")
    live = await _emit(cb.message, "🎲 Mengambil video acak…")
    v = await gw_random()
    if not v:
        await _emit(live, "❌ Gagal mengambil video acak.")
        return
    await show_card(live, v["code"], None, edit=True)


# --- teks bebas ------------------------------------------------------------- #

@dp.message(F.text)
async def on_text(m: Message):
    text = (m.text or "").strip()
    code = extract_code(text)
    if code:
        wait = await m.answer(f"⏳ Memuat <code>{e(code.upper())}</code>…",
                              parse_mode="HTML")
        v = await gw_video(code)
        if v and v.get("qualities"):
            await show_card(wait, code, None, edit=True)
            return
        # bukan kode valid → coba sebagai kata kunci
        await run_search(wait, text, 1, edit=True)
        return

    if len(text) >= 2:
        wait = await m.answer(f"🔍 Mencari <b>{e(text)}</b>…", parse_mode="HTML")
        await run_search(wait, text, 1, edit=True)
        return

    await m.answer("Kirim kode video, link missav, atau kata kunci (min. 2 huruf).")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


async def main():
    if not TOKEN:
        raise SystemExit("Set TG_BOT_TOKEN env dulu (token dari @BotFather).")
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PLAYER_PORT)
    await site.start()
    log.info("Player web listening on %s", _public_base())
    bot = Bot(TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot polling started (page size %d)", PAGE_SIZE)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
