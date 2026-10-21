#!/usr/bin/env bash
# deploy.sh — sinkron, validasi, deploy, lalu push ke GitHub.
#
# Alur:
#   1. missav_bot_v2.py (sumber kerja) -> missav_bot.py (nama kanonik di repo)
#   2. Validasi sintaks Python + scan rahasia  (batal kalau ada temuan)
#   3. Restart service systemd
#   4. Smoke test endpoint
#   5. Commit + push ke GitHub
#
# Pemakaian:
#   ./deploy.sh "pesan commit"
#   ./deploy.sh --no-restart "hanya sinkron dan push"
#   ./deploy.sh --dry-run   "cek saja tanpa mengubah apa pun"

set -uo pipefail
cd "$(dirname "$0")"

NO_RESTART=0
DRY=0
MSG=""
for arg in "$@"; do
    case "$arg" in
        --no-restart) NO_RESTART=1 ;;
        --dry-run)    DRY=1 ;;
        *)            MSG="$arg" ;;
    esac
done

GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
ok()   { echo "${GREEN}  OK${OFF}   $*"; }
bad()  { echo "${RED}  GAGAL${OFF} $*"; }
warn() { echo "${YEL}  !${OFF}    $*"; }
step() { echo; echo "${DIM}== $* ==${OFF}"; }

[ "$DRY" = "1" ] && warn "MODE DRY-RUN: tidak ada yang diubah"

# ---------- 1. Sinkron nama file ----------
step "1. Sinkron sumber"
if ! diff -q missav_bot_v2.py missav_bot.py >/dev/null 2>&1; then
    if [ "$DRY" = "1" ]; then
        warn "missav_bot_v2.py beda dengan missav_bot.py (akan disalin)"
    else
        cp missav_bot_v2.py missav_bot.py
        ok "missav_bot_v2.py -> missav_bot.py"
    fi
else
    ok "sudah identik"
fi

# ---------- 2. Validasi ----------
step "2. Validasi"
FAIL=0
for f in missav_bot.py missav_gateway.py missav_parse.py test_api.py; do
    if ./venv/bin/python -m py_compile "$f" 2>/dev/null; then
        ok "sintaks $f"
    else
        bad "sintaks $f"; FAIL=1
    fi
done

# JS di dalam template player
./venv/bin/python - <<'PYEOF' >/tmp/.pjs 2>/dev/null
import importlib.util, re
spec = importlib.util.spec_from_file_location("b", "missav_bot.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
html = m.PLAYER_TMPL.format(title="x", variants='{"code":"x","map":{"360p":"u"}}')
js = re.search(r"<script>\n(.*?)\n</script>", html, re.S).group(1)
open("/tmp/.player_check.js", "w").write(js)
print("ok")
PYEOF
if [ -s /tmp/.player_check.js ] && command -v node >/dev/null 2>&1; then
    if node --check /tmp/.player_check.js 2>/dev/null; then
        ok "sintaks JS player"
    else
        bad "sintaks JS player"; FAIL=1
    fi
fi

# Scan rahasia pada isi yang akan di-commit.
# Pola dirakit dari potongan string agar skrip ini tidak mendeteksi dirinya
# sendiri saat ikut di-commit.
git add -A 2>/dev/null
SCAN=0
P1='gh''p_';  P2='github_''pat_';  P3='AK''IA[0-9A-Z]{16}'
P4='PRIVATE'''' KEY'; P5='xo''x[baprs]-'; P6='s''k-[A-Za-z0-9]{20}'
P7='[0-9]{8,10}:[A-Za-z0-9_-]{30,}'
for pat in "$P7" "$P1" "$P2" "$P3" "$P4" "$P5" "$P6"; do
    n=$(git grep --cached -InE "$pat" 2>/dev/null | wc -l)
    if [ "$n" != "0" ]; then
        bad "rahasia terdeteksi (pola #$n): $n baris"; SCAN=1; FAIL=1
        git grep --cached -InE "$pat" 2>/dev/null | head -3
    fi
done
[ "$SCAN" = "0" ] && ok "scan rahasia bersih"

# File sensitif tidak boleh ikut ter-stage
for f in proxies.txt player_url.txt .env; do
    if git ls-files --cached --error-unmatch "$f" >/dev/null 2>&1; then
        bad "$f ikut ter-stage — periksa .gitignore"; FAIL=1
    fi
done
ok "file sensitif tetap di luar repo"

if [ "$FAIL" != "0" ]; then
    echo; bad "validasi gagal — deploy dibatalkan"
    exit 1
fi

# ---------- 3. Restart service ----------
step "3. Deploy service"
if [ "$DRY" = "1" ] || [ "$NO_RESTART" = "1" ]; then
    warn "restart dilewati"
else
    for svc in missav-gw missav-bot; do
        if sudo -n systemctl restart "$svc" 2>/dev/null; then
            sleep 3
            if systemctl is-active --quiet "$svc"; then
                ok "$svc aktif"
            else
                bad "$svc gagal start"; exit 1
            fi
        else
            bad "tidak bisa restart $svc"; exit 1
        fi
    done
    # Bot butuh ~15-20 detik menyambung ke Telegram sebelum web player
    # membuka port 8200. Tunggu port benar-benar siap, bukan sekadar
    # systemd bilang "active", supaya smoke test tidak dapat status 000.
    printf '       menunggu port 8200'
    for i in $(seq 1 40); do
        if curl -s -m 2 -o /dev/null "http://127.0.0.1:8200/" 2>/dev/null; then
            echo " — siap (${i}s)"; ok "web player menerima koneksi"; READY=1; break
        fi
        printf '.'; sleep 1
    done
    [ "${READY:-0}" = "1" ] || { echo; warn "port 8200 belum siap setelah 40s"; }
fi

# ---------- 4. Smoke test ----------
step "4. Smoke test"
if [ "$DRY" = "1" ]; then
    warn "dilewati"
else
    h=$(curl -s -m 30 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/health)
    [ "$h" = "200" ] && ok "gateway /health 200" || { bad "gateway /health $h"; exit 1; }
    # Player bisa 10-20 detik saat cache dingin (resolve ke upstream).
    # Status 000 = timeout curl, jadi beri waktu cukup sebelum menyimpulkan.
    p=$(curl -s -m 120 -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:8200/p/heyzo-3960?q=360p")
    case "$p" in
        200) ok "player 200" ;;
        000) warn "player timeout (cache dingin?) — cek manual" ;;
        *)   warn "player $p" ;;
    esac
    e=$(sudo -n journalctl -u missav-bot --since "-1min" --no-pager 2>/dev/null \
        | grep -cE 'ERROR|Traceback')
    [ "$e" = "0" ] && ok "log bot bersih" || warn "$e baris error di log"
fi

# ---------- 5. Commit & push ----------
step "5. Push ke GitHub"
if git diff --cached --quiet 2>/dev/null; then
    ok "tidak ada perubahan untuk di-commit"
else
    [ -z "$MSG" ] && MSG="update $(date '+%Y-%m-%d %H:%M')"
    if [ "$DRY" = "1" ]; then
        warn "akan commit: $MSG"
        git diff --cached --stat | tail -8
    else
        git commit -q -m "$MSG"
        if git push -q origin main 2>&1 | tail -3; then
            ok "commit: $MSG"
            ok "pushed -> $(git rev-parse --short HEAD)"
        else
            bad "push gagal"; exit 1
        fi
    fi
fi

echo
echo "${GREEN}selesai${OFF}"
