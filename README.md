# Vogelradar 🦅

Mobiele, kaart-first weergave van recente zeldzame vogelwaarnemingen in Nederland, met data van [waarneming.nl](https://waarneming.nl).

## Wat doet het

- Toont zeldzame vogelwaarnemingen op een interactieve kaart
- Sorteert op afstand tot jouw locatie (GPS)
- Filtert op zeldzaamheid (algemeen → zeer zeldzaam) en tijdvenster
- **Wensvogels** — stel meldingen in voor specifieke soorten of alle zeldzame vogels binnen een zelf in te stellen afstand
- **Push notificaties** — ontvang een melding ook als de app gesloten is, via Firebase Cloud Messaging
- Data wordt elk uur automatisch bijgewerkt vanuit waarneming.nl

## Stack

| Onderdeel | Technologie |
|---|---|
| Frontend | Single-file HTML (Leaflet, CartoDB Voyager tiles) |
| Backend | FastAPI + SQLite |
| Scraper | curl-cffi (Anubis PoW solver) + BeautifulSoup |
| Push | Firebase Cloud Messaging (FCM) via firebase-admin |
| Android app | Capacitor |

## Lokaal draaien

```bash
git clone https://github.com/hghluchtenbelt/vogelradar2.git
cd vogelradar2
./dev.sh
```

`dev.sh` maakt automatisch een virtualenv aan, installeert dependencies, doet een eerste scrape en start de server op `http://localhost:8000`.

## Projectstructuur

```
vogelradar.html      # Volledige frontend (één bestand)
api.py               # FastAPI — serveert /birds.json, de HTML en /register-token
scraper.py           # Waarneming.nl scraper met Anubis PoW solver
database.py          # SQLite opslag (waarnemingen + push subscribers)
updater.py           # Standalone scrape script (ook als achtergrond-thread)
notifications.py     # FCM push notificaties via firebase-admin
species_data.js      # 972 vogelsoorten uit waarneming.nl (voor wensvogels)
```
