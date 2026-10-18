#!/usr/bin/env bash
# Kamerbot bijwerken op de VPS. Gestart door systemd (kamerbot-update.path) zodra de bot na /update
# in Telegram data/update.request schrijft. Zelfde opzet als bij de ritbot.
#
# Stappen: nieuwste main ophalen, pakketten bijwerken, bot herstarten en wachten tot hij meldt dat
# hij draait (data/started). Lukt dat niet binnen de wachttijd, dan terug naar de vorige versie.
# .env en data/ staan niet in git en worden nooit aangeraakt.
set -uo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$APP/data"
NOTICE_DIR="$DATA/notices"
WAIT_S="${KAMERBOT_WAIT_S:-90}"

notice() { # melding voor Hidde; de bot stuurt hem door
  mkdir -p "$NOTICE_DIR"
  printf '%s\n' "$1" > "$NOTICE_DIR/update-$(date +%s).txt"
}

started_after() { # is de bot na tijdstip $1 (epoch) gestart?
  [[ -f "$DATA/started" ]] && (( $(stat -c %Y "$DATA/started") >= $1 ))
}

units() { # systemd-units uit deze versie neerzetten, zodat /update ook die wijzigingen meeneemt
  local unit
  for unit in kamerbot.service kamerbot-update.path kamerbot-update.service; do
    sed "s#@APP@#$APP#g" "$APP/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
}

install_rev() { # zet versie $1 neer en herstart; 0 als hij binnen WAIT_S draait
  git -C "$APP" reset -q --hard "$1" || return 1
  "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" || return 1
  units
  local t0
  t0=$(date +%s)
  rm -f "$DATA/started"
  systemctl restart kamerbot
  for _ in $(seq "$WAIT_S"); do
    sleep 1
    started_after "$t0" && return 0
  done
  return 1
}

rm -f "$DATA/update.request"
cd "$APP" || exit 1
prev=$(git rev-parse HEAD)
if ! git fetch -q origin main; then
  notice "❌ Bijwerken mislukt: kon de nieuwe versie niet ophalen bij GitHub. Er is niets veranderd."
  exit 1
fi
new=$(git rev-parse origin/main)
if [[ "$new" == "$prev" ]]; then
  notice "✅ Je hebt al de nieuwste versie (${prev:0:7})."
  exit 0
fi
subject=$(git log -1 --format=%s "$new")
if install_rev "$new"; then
  n=$(git rev-list --count "$prev..$new")
  notice "✅ Bijgewerkt naar ${new:0:7} ($n wijziging(en)). Laatste: $subject"
  exit 0
fi
log=$(journalctl -u kamerbot -n 15 --no-pager -o cat 2>/dev/null | tail -n 15)
if install_rev "$prev"; then
  notice "❌ De nieuwe versie (${new:0:7}) startte niet; de vorige (${prev:0:7}) draait weer.
Laatste regels uit het log:
$log"
else
  notice "❌ Bijwerken mislukt en ook de vorige versie start niet. Kijk op de VPS: journalctl -u kamerbot -n 50"
fi
exit 1
