# kamerbot

Houdt het SSH-aanbod in Utrecht in de gaten, filtert op jouw eisen en reageert
automatisch op de beste kamers, met meldingen via Telegram.

## Hoe hij kiest

- **Filters** (`kamerbot/config.py`): maximaal €1000 brutto, alleen Utrecht, niet aan de
  Ina Boudier-Bakkerlaan of de Pythagoraslaan, geen huizen die alleen vrouwen zoeken, en
  uitsluitwoorden in het verhaaltje.
- **Reageren op elk passend aanbod:** SSH rangschikt op inschrijfduur, dus intrekken en later
  opnieuw reageren kost niets. De bot reageert meteen; wil je pas in de laatste X uur reageren,
  zet dan `REAGEER_VENSTER_UUR` (onder 24 doet de 24-uursvoorrang niets).
- **Fietstijd:** alleen kamers binnen 10 minuten fietsen van Utrecht Centraal (`MAX_FIETSMINUTEN`),
  geschat uit de hemelsbrede afstand via PDOK. Onvindbaar adres = gaat gewoon door.
- **Voorrang:** kamers die binnen 24 uur sluiten gaan voor, daarna de laagste plek op de wachtlijst.
  Een bestaande reactie wordt alleen ingetrokken voor een kamer die niet later sluit; een kamer die
  later sluit wacht tot een volgende ronde.
  Zijn er meer kamers dan plekken (max 7), dan valt de kamer met de slechtste plek af; is dat een
  bestaande reactie, dan trekt hij die in, ook als je die zelf op de site plaatste
  (`INTREKKEN_HANDMATIG=0` om die te laten staan). Reacties op kamers die al gesloten zijn tellen
  bij SSH nog mee in de 7, maar die raakt de bot niet aan.
  Geen plekgrens meer, tenzij je `SSH_MAX_POSITIE` zet.
- **Bezichtigingen alleen in de top 5** (`BEZICHTIGING_MAX_PLEK`, 0 = uit): bij een kamer van het
  type Bezichtiging reageert de bot alleen als je op plek 1 t/m 5 staat. Zak je eruit, dan trekt hij
  de reactie in, ook als je die zelf plaatste (tenzij `INTREKKEN_HANDMATIG=0`). Elke ronde kijkt hij
  opnieuw; kom je weer in de top 5, dan reageert hij weer. Hospiteren valt hier niet onder.
- **Taalmodel** (optioneel, `ANTHROPIC_API_KEY`): Claude leest het verhaaltje, zegt of je past bij
  wie ze zoeken (cijfer 0–10 met een reden in de melding) en haalt de hospiteertijd eruit.
  Past het niet, dan slaat hij de kamer over. Elke tekst wordt maar één keer gelezen.
- **Score:** binnen dezelfde deadline weegt je positie het zwaarst, daarna het cijfer van het
  taalmodel, m², huur en of het een zelfstandige woning is.
- **Agenda** (optioneel, `GOOGLE_SERVICE_ACCOUNT_JSON` + `GCAL_AGENDA_ID`): alleen een
  hospiteeravond waarvoor SSH je echt heeft uitgenodigd (status in "Mijn reacties") komt in je agenda,
  met een 🎉-melding (tijd uit het verhaaltje, anders 19:30–22:00 met "(tijd onbekend)"). Niet gekozen,
  geen top 15 of ingetrokken: dan haalt hij zijn eigen afspraak weg en meldt welke. Avonden die al geweest
  zijn laat hij staan, en afspraken die je zelf maakte raakt hij niet aan.
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
18:00, en 's avonds tussen 22:00 en 23:30 om te checken of je nog kans maakt vóór de deadline.
Mislukt een ronde (bijv. inloggen), dan probeert hij het 10 minuten later opnieuw.
Hij start vanzelf na een herstart van de VPS.

## Bijwerken

Net als bij de ritbot: merge de PR op GitHub en stuur **/update** naar de bot. Hij haalt de
nieuwste versie op, herstart en meldt welke versie draait. Start de nieuwe versie niet, dan
zet hij de vorige vanzelf terug.

Andere commando's: **/status** (laatste en volgende ronde), **/nu** (meteen een ronde draaien) en **/overzicht** (waar je nu op gereageerd hebt en op welke plek je staat, eerst wat het eerst sluit). **/keuzes** laat per kamer in het aanbod zien wat de laatste ronde besloot, gegroepeerd op reden (gekozen, blijft staan, nog te veel tijd over, plek te hoog, zoekt een vrouw, uitgesloten locatie, te duur, te ver fietsen, enz.). De ronde schrijft dat zelf weg in `data/keuzes.json`, dus het zijn precies de redenen waarop de bot handelde; stuur /nu voor een verse stand.
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
