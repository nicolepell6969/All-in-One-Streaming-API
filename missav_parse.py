"""
missav_parse — parser HTML missav.ws (recon 2026-10-20).

Struktur halaman (diverifikasi di HTML mentah maupun varian yang sudah dirender JS):
  * Listing (home, /new, /release, /search, /genres/<g>, /actresses/<a>, ...):
        <a href="https://missav.ws/en/<code>" alt="<code>">
          <video data-src="https://fourhoi.com/<code>/preview.mp4">
          <img  data-src="https://fourhoi.com/<code>/cover-t.jpg" alt="<judul>">
        <a ...><span class="... bg-red-800 ...">English subtitle</span></a>
        <span class="... bg-gray-800 ...">H:MM:SS</span>
        (judul lengkap di blok x-html: "CODE Title - Actress")
  * Halaman video: blok <div class="text-secondary"><span>Code:</span>...
        berisi Release date / Code / Title / Tag, link actress ke /en/actresses/<slug>,
        plus og:* meta (og:video:duration, og:video:release_date, og:image).

Parser regex-based (tanpa lxml/bs4) supaya ringan dan tahan variasi atribut
(`href` vs `:href`, HTML ter-escape di dalam atribut x-init).
"""
from __future__ import annotations

import html as _html
import re
from typing import Optional

CDN = "https://fourhoi.com"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_MENU_SLUGS = {
    "vip", "saved", "playlists", "actresses", "genres", "makers", "history",
    "klive", "clive", "release", "uncensored-leak", "chinese-subtitle",
    "english-subtitle", "contact", "upload", "articles", "new", "today-hot",
    "weekly-hot", "monthly-hot", "dmca", "terms", "privacy", "rss", "feed",
    "login", "register", "forget", "logout", "legacy", "search", "ranking",
    "vr", "en", "zh", "cn", "ja", "ko", "ms", "th", "de", "fr", "vi", "id",
    "fil", "pt", "actresses-ranking",
}


def clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = _html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _hms_to_sec(s: str) -> Optional[int]:
    parts = [p for p in s.strip().split(":") if p != ""]
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def fmt_duration(sec: Optional[int]) -> Optional[str]:
    if not sec:
        return None
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def parse_code_from_url(url: str) -> Optional[str]:
    """https://missav.ws/en/fc2-ppv-1#hash -> fc2-ppv-1"""
    m = re.search(r"/(?:dm\d+/)?(?:[a-z]{2}/)?([a-z0-9][a-z0-9_.-]{3,})/?$", url or "", re.I)
    return m.group(1).lower() if m else None


# --------------------------------------------------------------------------- #
# Listing (search / home / genre / actress / new / ...)
# --------------------------------------------------------------------------- #

def parse_items(html: str) -> list[dict]:
    """Ambil semua kartu video dari halaman listing (dedupe by code, urutan asli).

    Tiap kartu dibatasi blok `class="thumbnail group"` -> kartu berikutnya, jadi
    field tidak bocor antar kartu. Tiap item: code, title, url, thumb,
    thumb_large, preview, duration, duration_text, has_english_subtitle,
    has_chinese_subtitle, uncensored, hash.
    """
    starts = [m.start() for m in re.finditer(r'class="thumbnail group"', html)]
    out: dict[str, dict] = {}
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else min(len(html), s + 4000)
        card = html[s:end]
        m = re.search(r"fourhoi\.com/([a-z0-9][a-z0-9_.-]{3,})/cover-t\.jpg", card, re.I)
        if not m:
            continue
        code = m.group(1).lower()
        img_tag = card[max(0, m.start() - 900):m.start()]
        alt = None
        for a in re.finditer(r'alt="([^"]*)"', card):
            val = _html.unescape(a.group(1)).strip()
            if val and val.lower() != code:
                alt = val
        title = ""
        tm = re.search(r'truncate">\s*<a[^>]*>([\s\S]{0,600}?)</a>', card)
        if tm:
            title = clean(tm.group(1))
        if not title or title == "item.full_title":
            title = alt or ""
        dur = dtxt = None
        dm = re.search(
            r'bg-gray-800[^>]*>\s*<span[^>]*>(\d*)</span>:<span[^>]*>(\d{1,2})</span>:<span[^>]*>(\d{1,2})</span>',
            card)
        if dm:
            dtxt = f"{dm.group(1) or '0'}:{dm.group(2)}:{dm.group(3)}"
            dur = int(dm.group(1) or 0) * 3600 + int(dm.group(2)) * 60 + int(dm.group(3))
        else:
            dt = re.search(r">\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*<", card)
            if dt:
                dtxt = dt.group(1)
                dur = _hms_to_sec(dtxt)
        hh = re.search(r"#([0-9a-f\-]{36})", card) or re.search(r"setPreview\('([0-9a-f\-]{36})'\)", card)
        item = {
            "code": code,
            "title": title,
            "url": f"https://missav.ws/en/{code}",
            "thumb": f"{CDN}/{code}/cover-t.jpg",
            "thumb_large": f"{CDN}/{code}/cover-n.jpg",
            "preview": f"{CDN}/{code}/preview.mp4",
            "duration": dur,
            "duration_text": dtxt,
            "has_english_subtitle": "English subtitle" in card,
            "has_chinese_subtitle": "Chinese subtitle" in card,
            "uncensored": ("is_uncensored_leak" in card or ">Uncensored<" in card
                           or code.endswith("-uncensored-leak")),
            "hash": hh.group(1) if hh else None,
        }
        if code not in out:
            out[code] = item
        elif len(item["title"]) > len(out[code].get("title") or ""):
            out[code]["title"] = item["title"]
    return list(out.values())


def parse_pagination(html: str) -> dict:
    cur = total = None
    m = re.search(r'name="page"\s+value="(\d+)"', html)
    if m:
        cur = int(m.group(1))
    m = re.search(r'id="price-currency">\s*/\s*(\d+)\s*<', html)
    if m:
        total = int(m.group(1))
    if total is None:
        pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", html)]
        total = max(pages) if pages else cur
    return {"page": cur, "total_pages": total,
            "has_next": bool(cur and total and cur < total)}


def parse_listing(html: str) -> dict:
    items = parse_items(html)
    return {"count": len(items), "items": items, "pagination": parse_pagination(html)}


# --------------------------------------------------------------------------- #
# Halaman detail
# --------------------------------------------------------------------------- #

def _meta(html: str, prop: str) -> Optional[str]:
    m = re.search(rf'property="og:{prop}" content="([^"]*)"', html)
    return _html.unescape(m.group(1)) if m else None


def _field_block(html: str, label: str) -> Optional[str]:
    """Isi satu field detail: <span>Label:</span> ... </div>  (tanpa div bersarang)."""
    m = re.search(r"<span>" + re.escape(label) + r":</span>([\s\S]{0,4000}?)</div>", html)
    return m.group(1) if m else None


def _field_text(html: str, label: str) -> Optional[str]:
    blk = _field_block(html, label)
    if blk is None:
        return None
    txt = clean(blk)
    return txt or None


def _field_links(html: str, label: str, kind: str) -> list[dict]:
    """Ambil link (actress/genre/tags/makers) dari satu field."""
    blk = _field_block(html, label)
    if not blk:
        return []
    out: list[dict] = []
    pat = r'<a[^>]+href="[^"]*?/' + kind + r'/([^"]+)"[^>]*>([^<]*)</a>'
    for m in re.finditer(pat, blk):
        slug = _html.unescape(m.group(1))
        name = clean(m.group(2)) or slug
        rec = {"name": name, "slug": slug}
        if kind in ("actresses", "makers", "genres", "tags"):
            rec["url"] = f"https://missav.ws/en/{kind}/{slug}"
        if rec not in out:
            out.append(rec)
    return out


def parse_detail(html: str, code: Optional[str] = None) -> dict:
    code_found = _field_text(html, "Code") or code
    title = _field_text(html, "Title") or _meta(html, "title")
    release = None
    m = re.search(r'<time datetime="([^"]+)"', html)
    if m:
        release = m.group(1)[:10]
    if not release:
        release = _meta(html, "video:release_date")
    duration = None
    m = re.search(r'property="og:video:duration" content="(\d+)"', html)
    if m:
        duration = int(m.group(1))

    tags = _field_links(html, "Tag", "tags")
    if not tags:
        tags = [{"name": _html.unescape(t), "slug": _html.unescape(t)}
                for t in re.findall(r'property="og:video:tag" content="([^"]*)"', html)]
    genres = _field_links(html, "Genre", "genres")
    actresses = _field_links(html, "Actress", "actresses")
    maker_links = _field_links(html, "Maker", "makers")
    maker = maker_links[0]["name"] if maker_links else _field_text(html, "Maker")
    label = _field_text(html, "Label")
    series = _field_text(html, "Series")
    return {
        "code": (code_found or code or "").lower() or None,
        "title": title,
        "description": _meta(html, "description"),
        "release_date": release,
        "duration": duration,
        "duration_text": fmt_duration(duration),
        "image": _meta(html, "image") or f"{CDN}/{(code_found or code or '').lower()}/cover-n.jpg",
        "actresses": actresses,
        "maker": maker,
        "label": label,
        "series": series,
        "genres": genres,
        "tags": tags,
    }


# --------------------------------------------------------------------------- #
# Genre & actress index
# --------------------------------------------------------------------------- #

def parse_genres(html: str) -> list[dict]:
    out, seen = [], set()
    for m in re.finditer(
        r'<a href="https://missav\.ws/(?:dm\d+/)?en/genres/([^"?#]+)"[^>]*class="text-nord13"[^>]*>([^<]+)</a>\s*<p class="text-nord10">\s*<a[^>]*>\s*([\d,]+) videos',
        html):
        slug = _html.unescape(m.group(1))
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"name": clean(m.group(2)), "slug": slug,
                    "count": int(m.group(3).replace(",", "")),
                    "url": f"https://missav.ws/en/genres/{slug}"})
    return out


def parse_actresses(html: str) -> list[dict]:
    """Halaman /en/actresses — <li> kartu: link /en/actresses/<slug>, nama (alt img),
    avatar fourhoi.com/actress/<id>-t.jpg, jumlah video, tahun debut."""
    out, seen = [], set()
    for blk in re.findall(r"<li>([\s\S]{0,1500}?)</li>", html):
        if "/actresses/" not in blk:
            continue
        m = re.search(r'href="https://missav\.ws/(?:dm\d+/)?en/actresses/([^"?#]+)"', blk)
        if not m:
            continue
        slug = _html.unescape(m.group(1))
        if slug in seen or slug.startswith("ranking"):
            continue
        seen.add(slug)
        avatar = re.search(r'fourhoi\.com/actress/([^"]+?)-t\.jpg', blk)
        alt = re.search(r'alt="([^"]*)"', blk)
        name = (clean(alt.group(1)) if alt else "") or _html.unescape(slug)
        cnt = re.search(r"([\d,]+) videos", blk)
        debut = re.search(r"Debut:\s*(\d{4})", blk)
        out.append({"name": name, "slug": slug,
                    "count": int(cnt.group(1).replace(",", "")) if cnt else None,
                    "debut": int(debut.group(1)) if debut else None,
                    "avatar": f"https://fourhoi.com/actress/{avatar.group(1)}-t.jpg" if avatar else None,
                    "url": f"https://missav.ws/en/actresses/{slug}"})
    return out


def parse_actress_filters(html: str) -> dict:
    """Opsi filter halaman actress: height (131-135), cup (A..Q), age (20-30), debut (2024).
    Dipakai endpoint /actresses?height=&cup=&... supaya nilai valid diketahui klien."""
    out: dict[str, list[str]] = {}
    for field in ("height", "cup", "age", "debut"):
        m = re.search(r'x-model="' + field + r'"[\s\S]{0,8000}?</select>', html)
        if not m:
            continue
        vals = [v for v in re.findall(r'<option value="([^"]*)"', m.group(0)) if v]
        out[field] = vals
    return out


def parse_index(html: str, kind: str) -> list[dict]:
    """Index maker / genre: <a href=".../en/<kind>/<slug>" class="text-nord13">Nama</a>
    diikuti <a ...>123 videos</a>."""
    out, seen = [], set()
    pat = (r'<a href="https://missav\.ws/(?:dm\d+/)?en/' + kind +
           r'/([^"?#]+)"[^>]*class="text-nord13"[^>]*>([^<]+)</a>\s*<p class="text-nord10">\s*<a[^>]*>\s*([\d,]+) videos')
    for m in re.finditer(pat, html):
        slug = _html.unescape(m.group(1))
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"name": clean(m.group(2)), "slug": slug,
                    "count": int(m.group(3).replace(",", "")),
                    "url": f"https://missav.ws/en/{kind}/{slug}"})
    return out


def parse_nav(html: str) -> list[dict]:
    out, seen = [], set()
    for u, t in re.findall(
        r'<a href="(https://missav\.ws/[^"]+)"[^>]*class="block[^"]*"[^>]*>\s*([^<]{2,60}?)\s*</a>',
        html):
        if u in seen:
            continue
        seen.add(u)
        out.append({"name": _html.unescape(t).strip(), "url": u})
    return out


# --------------------------------------------------------------------------- #
# Stream sources (Dean-Edwards packer berlapis)
# --------------------------------------------------------------------------- #

_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
    re.DOTALL)


def _to_base(n: int, base: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        d = n % base
        out = (chars[d] if d < 36 else chr(d + 29)) + out
        n //= base
    return out


def _unpack(packed: str, base: int, count: int, keys: str) -> str:
    k = keys.split("|")
    mapping = {_to_base(i, base): (k[i] if i < len(k) else _to_base(i, base))
               for i in range(count)}
    return re.sub(r"\b\w+\b", lambda m: mapping.get(m.group(0), m.group(0)), packed)


def extract_sources(html: str) -> dict[str, str]:
    text = html
    for _ in range(10):
        m = _PACKER_RE.search(text)
        if not m:
            break
        try:
            text = text.replace(m.group(0), _unpack(m.group(1), int(m.group(2)),
                                                    int(m.group(3)), m.group(4)), 1)
        except Exception:
            break
    sources: dict[str, str] = {}
    for name, url in re.findall(r"(\w+)=['\"](https?://[^'\"]+?\.m3u8)['\"]", text):
        sources[name] = url
    if not sources:
        for u in re.findall(r"https?://[^\s'\"\\]+\.m3u8", text):
            sources["auto"] = u
            break
    return sources


def rank_sources(src: dict[str, str]) -> dict[str, str]:
    """source1280 -> 1080p, source842 -> 720p, source -> auto."""
    out: dict[str, str] = {}
    if "source1280" in src:
        out["1080p"] = src["source1280"]
    if "source842" in src:
        out["720p"] = src["source842"]
    if "source" in src:
        out["auto"] = src["source"]
    for k, v in src.items():
        if k not in ("source1280", "source842", "source") and k not in out:
            out[k] = v
    return out or src
