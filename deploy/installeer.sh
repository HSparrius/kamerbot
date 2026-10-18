#!/usr/bin/env bash
# Eenmalig op de VPS (als root, vanuit de kamerbot-map): kamerbot als vaste dienst installeren,
# met /update vanuit Telegram. Opnieuw draaien kan altijd.
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "Draai dit als root."; exit 1; }
[[ -f $APP/.env ]] || { echo "Geen .env in $APP."; exit 1; }

chmod +x "$APP/deploy/update.sh"
for unit in kamerbot.service kamerbot-update.path kamerbot-update.service; do
  sed "s#@APP@#$APP#g" "$APP/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
done

# De oude cron-regels zijn niet meer nodig: de dienst plant zelf twee rondes per dag.
if crontab -l 2>/dev/null | grep -q 'python -m kamerbot'; then
  crontab -l | grep -v 'python -m kamerbot' | crontab -
  echo "Oude cron-regels voor kamerbot verwijderd."
fi

systemctl daemon-reload
systemctl enable --now kamerbot.service kamerbot-update.path
sleep 3
systemctl --no-pager --lines=5 status kamerbot.service || true
echo
echo "Klaar. Stuur /status naar je bot in Telegram."
