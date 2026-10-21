#!/usr/bin/env bash
# Jalankan gateway versi DEV di port 8101 (tanpa ganggu service produksi 8100)
cd /opt/missav
export GW_PORT=8101
export GW_CACHE_DIR=/opt/missav/cache
export GW_WORKERS=1
export GW_FLARESOLVERR=http://127.0.0.1:8191
export GW_PROXY_FILE=/opt/missav/proxies.txt
# limit tinggi supaya smoke test loopback tidak kena 429 sendiri
export GW_RATELIMIT=${GW_RATELIMIT:-100000}
exec /opt/missav/venv/bin/python missav_gateway.py
