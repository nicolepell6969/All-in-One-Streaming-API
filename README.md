# missav-gw v2 — API Gateway missav.ws

Gateway REST lengkap hasil reverse engineering situs **missav.ws** (recon 2026-10-20).
Menyediakan: pencarian, detail video, daftar kualitas, streaming HLS siap pakai,
download MP4, browse section/studio, index genre/maker/aktris/tag, player web,
proxy cover, dan statistik.

Dokumentasi interaktif: **`http://<host>:8100/docs`** (Swagger) dan `/redoc`.
Daftar endpoint ringkas: **`GET /`**.
**Dashboard web: `http://<host>:8100/dashboard`** — overview statistik, pencarian,
browse, player inline, dan API tester (klik endpoint → jalankan → lihat JSON).

---

## 1. Hasil bedah situs (apa yang dipakai gateway)

| Aspek | Temuan |
|---|---|
| Backend | Laravel/PHP di balik Cloudflare; frontend Tailwind + Alpine.js + Vite |
| HTML listing | Dirender server-side. Tiap kartu: `fourhoi.com/<code>/cover-t.jpg`, `<video ... preview.mp4>`, badge subtitle, durasi `H:MM:SS`, link `/en/<code>` |
| Halaman detail | Blok `<span>Code:</span>` berisi Code, Title, Actress, Genre, Maker, Tag + `og:video:duration` + `og:video:release_date` |
| Sumber stream | `<script>` berisi **Dean-Edwards packer berlapis** yang berakhir di `var source='https://surrit.com/<uuid>/<q>.m3u8'` |
| Format HLS | Master playlist `#EXT-X-STREAM-INF RESOLUTION=...` → varian 360p/480p/720p/1080p → segmen TS **tanpa enkripsi** |
| Nama segmen | Disamarkan jadi `video0.jpeg`, `video1.jpeg`, … (isinya MPEG-TS) |
| Proteksi missav.ws | Cloudflare memblokir IP datacenter (403) → butuh browser sungguhan (FlareSolverr) |
| Proteksi surrit.com | Cukup `Referer`/`Origin` missav + TLS fingerprint Chrome (curl_cffi). **Tidak** butuh proxy |
| Anti-bot JS | Domain-guard antar mirror (`missav.ws`, `missav.ai`, …), token `dmXXX` di URL, recombee untuk rekomendasi |
| Kualitas real | Halaman hanya memberi 1 URL master; kualitas asli harus di-expand dari master playlist |

## 2. Arsitektur

```
                      ┌─────────────────────── gateway (FastAPI) ───────────────────────┐
klien ──► /api/v1/* ──┤ GateMiddleware (API key opsional + rate limit)                  │
                      │ Cache (memori+disk, TTL, single-flight)                          │
                      │ Upstream.page():  FlareSolverr ► proxy pool ► curl_cffi direct   │
                      │ Upstream.cdn() :  curl_cffi impersonate Chrome (surrit/fourhoi)  │
                      └──────────────────────────────────────────────────────────────────┘
```

* **missav.ws** (HTML) → `FlareSolverr` (browser asli, selalu berhasil) → fallback
  proxy publik dari `proxies.txt` (sering mengembalikan halaman "shell" tanpa
  konten → ditolak validator) → curl_cffi langsung (403 dari CF).
* **surrit.com / fourhoi.com** (CDN) → curl_cffi `impersonate="chrome"` +
  `Referer` — cukup, tidak perlu proxy.
* Cache disk di `/opt/missav/cache` supaya restart tidak kehilangan hasil.

## 3. Endpoint

Semua di bawah prefix `/api/v1` (kecuali legacy). `lang` = `en|ja|cn|zh|ko|ms|th|de|fr|vi|id|fil|pt`.

### Katalog

| Method | Path | Keterangan |
|---|---|---|
| GET | `/api/v1/search?q=<kw>&page=1&sort=&lang=en` | Pencarian. `sort`: `released_at, published_at, saved, today_views, weekly_views, monthly_views, views` |
| GET | `/api/v1/sections` | Daftar section browse yang tersedia |
| GET | `/api/v1/browse/{section}?page=1&sort=&filters=` | `new, release, uncensored-leak, english-subtitle, chinese-subtitle, today-hot, weekly-hot, monthly-hot, vr, fc2, heyzo, tokyohot, siro, luxu, gana, maan, scute, ara, 1pondo, caribbeancom, caribbeancompr, 10musume, pacopacomama, gachinco, xxxav, marriedslash, naughty4610, naughty0930, madou, twav, furuke, klive, clive` |
| GET | `/api/v1/genres?page=1` | Index genre (nama, slug, jumlah video) |
| GET | `/api/v1/genres/{slug}?page=1&sort=` | Video per genre |
| GET | `/api/v1/makers?page=1` | Index maker/studio |
| GET | `/api/v1/makers/{slug}?page=1` | Video per maker |
| GET | `/api/v1/actresses?page=1&sort=videos\|debut&height=&cup=&age=&debut=` | Index aktris + filter body |
| GET | `/api/v1/actress/{slug}?page=1` | Video per aktris |
| GET | `/api/v1/tags/{slug}?page=1` | Video per tag |
| GET | `/api/v1/random` | Satu video acak |

Bentuk item pada semua listing:

```json
{
  "code": "heyzo-3960",
  "title": "HEYZO-3960 Please sleep with my wife ...",
  "url": "https://missav.ws/en/heyzo-3960",
  "thumb": "https://fourhoi.com/heyzo-3960/cover-t.jpg",
  "thumb_large": "https://fourhoi.com/heyzo-3960/cover-n.jpg",
  "preview": "https://fourhoi.com/heyzo-3960/preview.mp4",
  "duration": 3317, "duration_text": "0:55:17",
  "has_english_subtitle": true, "has_chinese_subtitle": false,
  "uncensored": true, "hash": "8be44de2-..."
}
```

### Detail & video

| Method | Path | Keterangan |
|---|---|---|
| GET | `/api/v1/video/{code}?related=true&lang=en` | Detail lengkap: metadata, aktris, genre, tag, maker, **daftar kualitas**, URL stream, related |
| GET | `/api/v1/related/{code}?limit=24` | Video terkait saja |

```json
{
  "code": "heyzo-3960",
  "title": "...", "release_date": "2026-10-17",
  "duration": 3317, "duration_text": "0:55:17",
  "image": "https://fourhoi.com/heyzo-3960/cover-n.jpg",
  "actresses": [{"name": "Yamauchi Yayoi", "slug": "Yamauchi%20Yayoi", "url": "..."}],
  "maker": "Heyzo", "genres": [{"name": "Wife", "slug": "Wife", "url": "..."}],
  "tags": [],
  "qualities": ["720p", "480p", "360p", "auto"],
  "streams": {"720p": "https://surrit.com/<uuid>/720p/video.m3u8", "auto": "..."},
  "stream_url": "/api/v1/stream/heyzo-3960",
  "download_url": "/api/v1/download/heyzo-3960",
  "related": [ "…24 item…" ]
}
```

### Streaming

| Method | Path | Keterangan |
|---|---|---|
| GET | `/api/v1/stream/{code}?quality=720p&ext=` | Playlist HLS. Semua URI di-rewrite ke gateway (absolut) → siap hls.js / mpv / VLC |
| GET | `/api/v1/proxy/m3u8?u=<url>&ext=` | Proxy playlist mentah (URI di-rewrite) |
| GET | `/api/v1/proxy/segment?u=<url>` | Pipe segmen TS, mendukung `Range` (206) |
| GET | `/api/v1/download/{code}?quality=720p` | Remux MP4 via ffmpeg, di-stream langsung (tanpa simpan disk) |
| GET | `/api/v1/player/{code}` | Halaman HTML player hls.js dengan tombol pilih kualitas |

| GET | `/api/v1/image?u=<fourhoi url>` | Proxy cover/preview (allowlist `fourhoi.com`) |
| GET | `/api/v1/stats` | Statistik cache & upstream |
| GET | `/health`, `/`, `/docs`, `/openapi.json`, `/redoc` | Meta |

**Catatan ffmpeg:** host menyamarkan segmen sebagai `.jpeg`, sedangkan ffmpeg
menolak ekstensi itu (`allowed_segment_extensions`). Gateway menambahkan
`&ext=seg.ts` pada URI segmen (parameter `ext` di `/stream` dan `/proxy/m3u8`)
dan memakai `-allowed_extensions ALL` saat download.

**Catatan player eksternal:** `mpv`/`VLC`/`ffplay` bisa langsung membuka
`http://<host>:8100/api/v1/stream/<code>?quality=720p`.

### Legacy (kompatibel `missav_bot.py` versi lama)

| Path | Keterangan |
|---|---|
| `/resolve/{code}` | `{meta, qualities, streams: {q: {direct, proxied}}}` |
| `/search?q=` | `{results: [{code, title, url}]}` |
| `/titles?codes=a,b,c` | Judul untuk prefetch |
| `/proxy/m3u8?u=`, `/proxy/segment?u=` | Sekarang memakai rewrite + allowlist baru |
| `/download/{code}?q=720p` | Alias `/api/v1/download` |

## 4. Menjalankan

```bash
# dev (port 8101, tidak mengganggu produksi 8100)
/opt/missav/dev_run.sh

# produksi (systemd, port 8100)
sudo systemctl restart missav-gw
sudo systemctl status missav-gw
```

### Konfigurasi (environment)

| Env | Default | Keterangan |
|---|---|---|
| `GW_PORT` | `8100` | Port |
| `GW_WORKERS` | `2` | Jumlah worker uvicorn |
| `GW_API_KEY` | *(kosong)* | Jika diisi, semua endpoint (kecuali `/health`, `/`, `/docs`) wajib header `X-API-Key` atau `?api_key=` |
| `GW_FLARESOLVERR` | *(kosong)* | URL FlareSolverr, mis. `http://127.0.0.1:8191` |
| `GW_PROXY_FILE` | *(kosong)* | File `host:port` per baris untuk fallback proxy |
| `GW_PAGE_TTL` | `900` | TTL cache halaman listing (detik) |
| `GW_STREAM_TTL` | `1800` | TTL cache detail video + streams |
| `GW_INDEX_TTL` | `21600` | TTL cache genre/maker/actress index |
| `GW_RATELIMIT` | `240` | Request per menit per IP |
| `GW_CACHE_DIR` | `/opt/missav/cache` | Cache disk |
| `GW_PUBLIC_URL` | *(kosong)* | Base URL untuk URI playlist bila di balik reverse proxy/tunnel |
| `GW_FS_CONCURRENCY` | `3` | Batas request FlareSolverr bersamaan |
| `GW_PROXY_ATTEMPTS` | `6` | Jumlah proxy dicoba sebelum menyerah |

## 5. Contoh pemakaian

```bash
# cari
curl -s 'localhost:8100/api/v1/search?q=mfc&page=1' | jq '.items[0]'

# detail + kualitas
curl -s 'localhost:8100/api/v1/video/heyzo-3960' | jq '{title, qualities, streams}'

# putar di mpv
mpv 'http://localhost:8100/api/v1/stream/heyzo-3960?quality=720p'

# buka player web di browser
xdg-open 'http://localhost:8100/api/v1/player/heyzo-3960'

# download MP4 (stream, ctrl-c aman)
curl -o heyzo-3960.mp4 'localhost:8100/api/v1/download/heyzo-3960?quality=720p'
```

Player HTML minimal (hls.js):

```html
<video id="v" controls></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
  const hls = new Hls();
  hls.loadSource('http://localhost:8100/api/v1/stream/heyzo-3960?quality=720p');
  hls.attachMedia(document.getElementById('v'));
</script>
```

## 6. Performa & cache (hasil ukur)

* Request pertama ke missav (cold) **~12 detik** karena FlareSolverr memuat
  halaman lewat browser. Setelah itu hit cache → **< 50 ms**.
* Segmen CDN **~30 ms** (langsung ke surrit, tanpa proxy).
* Download 360p (durasi 15:52) selesai **~18 detik**, hasil 56 MB MP4 valid.
* `single-flight` menjamin request identik yang bersamaan tidak memicu dua
  render browser sekaligus.

## 7. Struktur file

| File | Isi |
|---|---|
| `missav_gateway.py` | Aplikasi FastAPI, endpoint, cache, upstream fetcher |
| `missav_parse.py` | Semua parser HTML (listing, detail, genre, actress, packer m3u8) |
| `dashboard.html` | Dashboard web (disajikan di `/dashboard`) |
| `missav_bot.py` | Bot Telegram + web player (pakai endpoint legacy, tetap kompatibel) |
| `test_api.py` | Smoke test semua endpoint: `python3 test_api.py http://127.0.0.1:8100` |
| `requirements.txt` | Dependensi Python |
| `.env.example` | Contoh konfigurasi environment (salin jadi `.env`) |
| `dev_run.sh` | Launcher dev di port 8101 |
| `cache/` | Cache disk TTL *(tidak di-commit)* |
| `proxies.txt` | Pool proxy fallback *(tidak di-commit)* |

### File yang sengaja TIDAK di-commit

`.gitignore` mengecualikan data sensitif dan runtime:

- `.env`, `player_url.txt` — bisa berisi token/alamat spesifik server
- `proxies.txt`, `surrit_*.txt` — berisi IP:port milik pihak lain, jangan disebar
- `cache/`, `*.mp4` — data runtime, ukuran besar
- `__pycache__/`, `venv/` — artefak build
- `*.bak*`, `*_v1_backup.py` — salinan kerja lokal

## 8. Instalasi dari nol

```bash
git clone <repo-url> missav
cd missav

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
# isi TG_BOT_TOKEN (dari @BotFather) di .env

# FlareSolverr (sangat disarankan — missav.ws dilindungi Cloudflare)
docker run -d --name flaresolverr -p 127.0.0.1:8191:8191 \
  --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest
```

Jangan lupa `ffmpeg` untuk fitur download MP4 dan fallback player:

```bash
sudo apt install -y ffmpeg
```

Menjalankan tanpa systemd:

```bash
./venv/bin/python missav_gateway.py     # gateway  :8100
./venv/bin/python missav_bot.py         # bot + web player :8200
```

## 9. Troubleshooting

| Gejala | Penyebab / Solusi |
|---|---|
| `502 upstream missav tidak bisa diakses` | FlareSolverr mati. `curl 127.0.0.1:8191` harus balas `{"msg": "FlareSolverr is ready!"}`. Proxy publik hampir selalu gagal untuk missav. |
| `429 rate limited` | Naikkan `GW_RATELIMIT`. |
| `404 video ... tidak ditemukan` | Kode salah, atau halaman benar-benar 404. |
| Playlist kosong / 0 segmen | Sumber berubah; cek `/api/v1/video/<code>` → `streams`. |
| `ffmpeg: not in allowed_segment_extensions` | Pakai `?ext=seg.ts` di `/stream` atau `/proxy/m3u8`. |
| Kualitas hanya `360p` untuk FC2 | Video sumbernya memang hanya 360p; master playlist sudah di-expand. |
| URI playlist menunjuk `127.0.0.1` saat diakses dari luar | Set `GW_PUBLIC_URL=https://domain-anda`. |

## 10. Bot Telegram + Web Player (`missav_bot.py`)

Bot Telegram yang memasang tombol "▶️ Putar 720p" — tombolnya membuka **web player**
(halaman hls.js) yang streaming langsung dari gateway.

```bash
# 1. Buat bot dari @BotFather → ambil token
# 2. Jalankan:
TG_BOT_TOKEN='123:ABC' python3 missav_bot.py
# player web otomatis jalan di port 8200
```

Env bot: `TG_BOT_TOKEN` (wajib), `GW_BASE`, `PLAYER_PORT` (8200),
`PLAYER_PUBLIC_URL` (wajib kalau server di belakang NAT), `GW_RATELIMIT` dst milik gateway.

## 11. Catatan hukum

Gateway ini hanya *proxy* ke konten publik yang sudah ada di missav.ws/surrit.com;
tidak menyimpan/meng-host video. Gunakan untuk keperluan pribadi; tanggung jawab
konten sepenuhnya milik pemilik situs asal.

## 12. Catatan codec: dashboard & Camoufox

Dashboard memutar video lewat hls.js + MSE, yang butuh **H.264/AAC**. Browser
Firefox yang dibangun tanpa codec proprietary (mis. Camoufox yang dipakai
browser-tool Hermes) melaporkan:

```
MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E,mp4a.40.2"')  -> false
MediaSource.isTypeSupported('video/webm; codecs="vp9"')                  -> true
```

Akibatnya muncul `bufferAppendError`. Dashboard mendeteksi ini lebih dulu dan
menampilkan pesan + URL playlist untuk mpv/VLC, bukan error mentah.
Untuk menonton di browser pakai Chrome/Edge/Safari. API dan streaming tidak
terpengaruh — hanya pemutaran di dalam Camoufox.
