# kamerbot

Houdt het SSH-aanbod in Utrecht in de gaten, filtert op jouw eisen en reageert
automatisch op de beste kamers, met meldingen via Telegram.

## Hoe hij kiest

- **Filters** (`kamerbot/config.py`): maximaal €1000 brutto, alleen Utrecht, niet aan de
  Ina Boudier-Bakkerlaan of de Pythagoraslaan, geen huizen die alleen vrouwen zoeken, en
  uitsluitwoorden in het verhaaltje.
- **Reageren op de dag van de deadline:** SSH rangschikt op inschrijfduur, dus vroeg reageren
  levert geen betere plek op. De bot reageert pas in de laatste 18 uur (`REAGEER_VENSTER_UUR`),
  en kamers die het eerst verlopen gaan voor.
- **Kansgrens:** boven plek 30 (`SSH_MAX_POSITIE`) reageert hij niet. Zak je na reageren
  alsnog onder die grens, dan krijg je een melding.
- **Score:** binnen dezelfde deadline weegt je positie het zwaarst, daarna m², huur en of het
  een zelfstandige woning is.
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

Installeer hem daarna als vaste dienst (eenmalig, als root):

```bash
deploy/installeer.sh
```

De dienst plant zelf twee rondes per dag, elk op een willekeurig moment: overdag tussen 9:00 en
18:00, en 's avonds tussen 22:00 en 23:55 om te checken of je nog kans maakt vóór de deadline.
Hij start vanzelf na een herstart van de VPS.

## Bijwerken

Net als bij de ritbot: merge de PR op GitHub en stuur **/update** naar de bot. Hij haalt de
nieuwste versie op, herstart en meldt welke versie draait. Start de nieuwe versie niet, dan
zet hij de vorige vanzelf terug.

Andere commando's: **/status** (laatste en volgende ronde) en **/nu** (meteen een ronde draaien).
Log bekijken: `journalctl -u kamerbot -f`.

## Telegram

Maak een eigen bot via @BotFather (`TELEGRAM_TOKEN`). Gebruik niet de token van de ritbot: beide
diensten luisteren naar commando's, en dan pikken ze elkaars berichten weg. Druk één keer op Start
bij je bot en haal je chat-id op via @userinfobot (`TELEGRAM_CHAT_ID`).

## Tests

```bash
pytest
```

De fixtures in `tests/fixtures` zijn het SSH-aanbod van 18 oktober 2026, zonder persoonsgegevens.
