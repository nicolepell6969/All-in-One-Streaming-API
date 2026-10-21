#!/usr/bin/env python3
"""Smoke test semua endpoint missav-gw. Pakai: python3 test_api.py [base]"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
CODE = "heyzo-3960"
ok = fail = 0


def get(path, timeout=180, headers=None):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers=headers or {})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read(), r.headers, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers, time.time() - t


def check(name, path, want=200, probe=None, timeout=180, headers=None):
    global ok, fail
    status, body, hdrs, dt = get(path, timeout, headers)
    good = status == want
    extra = ""
    if good and probe:
        try:
            data = json.loads(body)
            good, extra = probe(data)
        except Exception as e:
            good, extra = False, f"parse error: {e}"
    elif good:
        extra = f"{len(body)}B"
    if good:
        ok += 1
        print(f"  OK   {name:26s} {status} {dt:5.1f}s  {extra}")
    else:
        fail += 1
        print(f"  FAIL {name:26s} {status} {dt:5.1f}s  {str(body)[:160]}")


def has_items(d, min_n=1):
    n = d.get("count") or len(d.get("items") or [])
    first = (d.get("items") or [{}])[0]
    return n >= min_n, f"count={n} first={first.get('code') or first.get('name')}"


print(f"== {BASE}")
check("root", "/")
check("health", "/health", probe=lambda d: (d.get("status") == "ok", d.get("status")))
check("openapi", "/openapi.json", probe=lambda d: (len(d.get("paths", {})) > 15,
                                                  f"{len(d.get('paths', {}))} paths"))
check("search", "/api/v1/search?q=mfc", probe=has_items)
check("search+sort", "/api/v1/search?q=mfc&sort=today_views&page=2", probe=has_items)
check("sections", "/api/v1/sections", probe=lambda d: (d["count"] > 20, f"{d['count']} sections"))
check("browse new", "/api/v1/browse/new", probe=has_items)
check("browse fc2", "/api/v1/browse/fc2", probe=has_items)
check("browse today-hot", "/api/v1/browse/today-hot", probe=has_items)
check("browse bad", "/api/v1/browse/ngawur", want=404)
check("video detail", f"/api/v1/video/{CODE}",
      probe=lambda d: (bool(d.get("streams")) and d.get("duration") is not None,
                       f"q={d.get('qualities')} rel={len(d.get('related') or [])}"))
check("video 404", "/api/v1/video/zzzz-tidak-ada-999", want=404)
check("related", f"/api/v1/related/{CODE}?limit=5", probe=has_items)
check("genres", "/api/v1/genres", probe=has_items)
check("genre listing", "/api/v1/genres/Creampie", probe=has_items)
check("makers", "/api/v1/makers", probe=has_items)
check("maker listing", "/api/v1/makers/S1", probe=has_items)
check("actresses", "/api/v1/actresses", probe=has_items)
check("actresses filter", "/api/v1/actresses?cup=F&sort=videos", probe=has_items)
check("actress listing", "/api/v1/actress/Yamauchi%20Yayoi", probe=has_items)
check("tags", "/api/v1/tags/Creampie", probe=has_items)
check("random", "/api/v1/random", probe=lambda d: (bool(d.get("video")), d["video"]["code"]))
check("stats", "/api/v1/stats", probe=lambda d: ("cache" in d, str(d["cache"])))
check("player", f"/api/v1/player/{CODE}", probe=None)

# streaming
status, body, hdrs, dt = get(f"/api/v1/stream/{CODE}?quality=720p")
lines = body.decode(errors="replace").splitlines()
segs = [l for l in lines if "proxy/segment" in l]
master = [l for l in lines if "proxy/m3u8" in l]
good = status == 200 and (segs or master)
print(f"  {'OK  ' if good else 'FAIL'} stream 720p              {status} {dt:5.1f}s  "
      f"{len(segs)} segs, {len(master)} variants")
ok, fail = (ok + 1, fail) if good else (ok, fail + 1)

if segs:
    url = segs[0].strip()
    if url.startswith("/"):
        url = BASE + url
    status, body, hdrs, dt = get(url, timeout=90)
    good = status == 200 and len(body) > 10000
    print(f"  {'OK  ' if good else 'FAIL'} segment                  {status} {dt:5.1f}s  "
          f"{len(body)}B {hdrs.get('content-type')}")
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    req = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
    r = urllib.request.urlopen(req, timeout=60)
    data = r.read()
    good = r.status == 206 and len(data) == 1024
    print(f"  {'OK  ' if good else 'FAIL'} segment range            {r.status} {len(data)}B")
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)

check("image proxy", "/api/v1/image?u=" + urllib.parse.quote(
    "https://fourhoi.com/heyzo-3960/cover-t.jpg", safe=""), timeout=40)

# legacy
check("legacy resolve", f"/resolve/{CODE}",
      probe=lambda d: (bool(d.get("streams")), str(d.get("qualities"))))
check("legacy search", "/search?q=heyzo", probe=lambda d: (d["count"] > 0, f"count={d['count']}"))
check("legacy titles", f"/titles?codes={CODE}", probe=lambda d: (CODE in d["titles"], "ok"))

print(f"\n  {ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
