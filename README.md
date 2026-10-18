# kamerbot

Houdt het SSH-aanbod in Utrecht in de gaten, filtert op jouw eisen en reageert
automatisch op de beste kamers, met meldingen via Telegram.

## Hoe hij kiest

- **Filters** (`kamerbot/config.py`): maximaal €1000 brutto, alleen Utrecht, niet aan de
  Ina Boudier-Bakkerlaan of de Pythagoraslaan, geen huizen die alleen vrouwen zoeken, en
  uitsluitwoorden in het verhaaltje.
- **Wachten tot de deadline nadert:** SSH rangschikt op inschrijfduur, dus vroeg reageren
  levert geen betere plek op. De bot reageert pas in de laatste 18 uur (`REAGEER_VENSTER_UUR`).
  Zo houd je plekken vrij voor betere kamers die later binnenkomen.
- **Score:** je positie weegt het zwaarst, daarna m², huur en of het een zelfstandige woning is.
  Boven positie 15 (`SSH_MAX_POSITIE`) reageert hij niet.
- **Plekken:** hij vult alleen de vrije plekken (7 min je actieve reacties).

## Installatie op de VPS

```bash
git clone git@github.com:HSparrius/kamerbot.git && cd kamerbot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env   # vul in, chmod 600 .env
```

Test eerst één ronde met `AUTO_REAGEREN=0`, zodat hij alleen meldt:

```bash
set -a; . ./.env; set +a; python -m kamerbot
```

Daarna in `crontab -e`:

```
*/30 * * * * cd ~/kamerbot && set -a && . ./.env && set +a && .venv/bin/python -m kamerbot >> data/kamerbot.log 2>&1
```

Werkt alles, zet dan `AUTO_REAGEREN=1`.

## Telegram

Maak een bot via @BotFather (`TELEGRAM_TOKEN`). Stuur hem een bericht en haal je chat-id op via
`https://api.telegram.org/bot<TOKEN>/getUpdates` (`TELEGRAM_CHAT_ID`). Gebruik je de bot van de
ritbot al, dan kun je diens token en chat-id hergebruiken.

## Tests

```bash
pytest
```

De fixtures in `tests/fixtures` zijn het SSH-aanbod van 18 oktober 2026, zonder persoonsgegevens.
