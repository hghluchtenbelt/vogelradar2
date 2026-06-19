# Vogelradar 🦅

Mobiele, kaart-first weergave van recente zeldzame vogelwaarnemingen in Nederland, met data van [waarneming.nl](https://waarneming.nl).

![Vogelradar](https://vogel-radar.nl)

## Wat doet het

- Toont zeldzame vogelwaarnemingen op een interactieve kaart
- Sorteert op afstand tot jouw locatie (GPS)
- Filtert op zeldzaamheid (algemeen → zeer zeldzaam) en tijdvenster
- **Wensvogels** — stel meldingen in voor specifieke soorten of alle zeldzame vogels binnen een zelf in te stellen afstand
- Data wordt elk uur automatisch bijgewerkt vanuit waarneming.nl

## Stack

| Onderdeel | Technologie |
|---|---|
| Frontend | Single-file HTML (Leaflet, CartoDB Voyager tiles) |
| Backend | FastAPI + SQLite |
| Scraper | curl-cffi (Anubis PoW solver) + BeautifulSoup |

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
api.py               # FastAPI — serveert /birds.json en de HTML
scraper.py           # Waarneming.nl scraper met Anubis PoW solver
database.py          # SQLite opslag
updater.py           # Standalone scrape script (ook als achtergrond-thread)
species_data.js      # 972 vogelsoorten uit waarneming.nl (voor wensvogels)
```
