"""
Scraper for rare bird observations from waarneming.nl.

waarneming.nl is protected by Anubis (proof-of-work bot protection).
We solve the SHA-256 PoW challenge once per session to obtain a JWT
cookie, then reuse that session for all subsequent requests.

PoW algorithm (reverse-engineered from Anubis 1.24.0 main.mjs):
  Input  : UTF-8 bytes of  randomData + str(nonce)
  Hash   : SHA-256
  Pass   : first floor(difficulty/2) bytes == 0x00
           if difficulty is odd: high nibble of next byte == 0
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import threading
import time
from datetime import date, timedelta
from typing import Callable

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://waarneming.nl"
RARITY_LEVELS = [3, 2, 1]  # 3=zeldzaam, 2=vrij zeldzaam, 1=vrij algemeen
SPECIES_GROUP = 1           # birds

_IMPERSONATE = "chrome120"
_N_WORKERS = 4
_thread_local = threading.local()

# Dutch month names → zero-padded month number
_DUTCH_MONTHS = {
    "jan": "01", "januari": "01",
    "feb": "02", "februari": "02",
    "mrt": "03", "mar": "03", "maart": "03",
    "apr": "04", "april": "04",
    "mei": "05", "may": "05",
    "jun": "06", "juni": "06",
    "jul": "07", "juli": "07",
    "aug": "08", "augustus": "08",
    "sep": "09", "september": "09",
    "okt": "10", "oct": "10", "oktober": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}

_RARITY_TEXT = {
    "zeer zeldzaam": 4,
    "zeldzaam": 3,
    "vrij zeldzaam": 3,
    "vrij algemeen": 2,
    "algemeen": 1,
}


def _get_worker_session() -> cffi_requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_authenticated_session()
    return _thread_local.session


# ── Public entry point ────────────────────────────────────────────


def fetch_rare_birds(
    days_back: int = 7,
    rarity_levels: list[int] | None = None,
    days_back_by_rarity: dict[int, int] | None = None,
    known_urls: set[str] | None = None,
    on_observations: Callable[[list[dict]], None] | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> list[dict]:
    """
    Scrape bird observations and return them (or flush via on_observations).

    days_back_by_rarity overrides days_back per rarity level.
    known_urls are skipped — only new detail pages are fetched.
    on_observations(batch) is called every 50 results for incremental
    persistence; when provided the return value is an empty list.
    """
    if rarity_levels is None:
        rarity_levels = RARITY_LEVELS

    if progress_callback:
        progress_callback(0.0, "Verbinding maken met waarneming.nl…")

    session = _make_authenticated_session()

    if progress_callback:
        progress_callback(0.02, "Observatie-URLs ophalen…")

    seen_paths: set[str] = set()
    all_paths: list[str] = []
    total_levels = len(rarity_levels)
    for li, rarity in enumerate(rarity_levels):
        rarity_days = (days_back_by_rarity or {}).get(rarity, days_back)
        if progress_callback:
            progress_callback(
                0.02 + 0.08 * li / total_levels,
                f"URLs ophalen rarity={rarity} ({rarity_days}d)…",
            )
        paths = _gather_obs_paths(
            session, rarity_days, rarity, expand_species=True,
            progress_callback=progress_callback,
            pct_start=0.02 + 0.08 * li / total_levels,
            pct_end=0.02 + 0.08 * (li + 1) / total_levels,
        )
        for p in paths:
            if p not in seen_paths:
                seen_paths.add(p)
                all_paths.append(p)

    known = known_urls or set()
    new_paths = [p for p in all_paths if f"{BASE_URL}{p}" not in known]
    skipped = len(all_paths) - len(new_paths)

    if not new_paths:
        if progress_callback:
            progress_callback(1.0, f"Alles al up-to-date ({skipped} al bekend).")
        return []

    if progress_callback:
        skip_note = f" ({skipped} overgeslagen)" if skipped else ""
        progress_callback(0.05, f"{len(new_paths)} nieuwe observaties te laden{skip_note}…")

    def _fetch_one(path: str) -> dict | None:
        s = _get_worker_session()
        obs = _scrape_observation(s, path)
        time.sleep(0.35)
        return obs

    _FLUSH_EVERY = 50
    observations: list[dict] = []
    pending: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=_N_WORKERS) as pool:
        for i, obs in enumerate(pool.map(_fetch_one, new_paths)):
            if obs:
                if on_observations:
                    pending.append(obs)
                    if len(pending) >= _FLUSH_EVERY:
                        on_observations(pending)
                        pending.clear()
                else:
                    observations.append(obs)
            if progress_callback:
                progress_callback(
                    0.05 + 0.95 * (i + 1) / len(new_paths),
                    f"Laden {i + 1} / {len(new_paths)}…",
                )

    if on_observations and pending:
        on_observations(pending)

    total = len(observations) if not on_observations else "?"
    if progress_callback:
        progress_callback(1.0, f"Klaar — {total} observaties geladen.")

    return observations


# ── Anubis PoW ────────────────────────────────────────────────────


def _make_authenticated_session() -> cffi_requests.Session:
    session = cffi_requests.Session()
    session.headers.update({"Cookie": "django_language=nl"})
    seed_url = (
        f"{BASE_URL}/fieldwork/observations/daylist/"
        f"?date={date.today()}&species_group={SPECIES_GROUP}"
        f"&rarity={RARITY_LEVELS[0]}&search="
    )
    _solve_anubis(session, seed_url)
    return session


def _solve_anubis(session: cffi_requests.Session, url: str) -> None:
    r = session.get(url, impersonate=_IMPERSONATE)

    m = re.search(
        r'<script id="anubis_challenge" type="application/json">(.*?)</script>',
        r.text,
        re.DOTALL,
    )
    if not m:
        return
    outer = json.loads(m.group(1))
    if outer is None:
        return

    challenge = outer["challenge"]
    difficulty: int = outer["rules"]["difficulty"]
    random_data: str = challenge["randomData"]
    challenge_id: str = challenge["id"]

    n_zero = difficulty // 2
    half = difficulty % 2 != 0
    t0 = time.time()
    nonce = 0
    while True:
        digest = hashlib.sha256((random_data + str(nonce)).encode()).digest()
        ok = all(digest[i] == 0 for i in range(n_zero))
        if ok and half:
            ok = (digest[n_zero] >> 4) == 0
        if ok:
            break
        nonce += 1

    elapsed_ms = int((time.time() - t0) * 1000)

    session.get(
        f"{BASE_URL}/.within.website/x/cmd/anubis/api/pass-challenge",
        params={
            "id": challenge_id,
            "response": digest.hex(),
            "nonce": nonce,
            "redir": url,
            "elapsedTime": elapsed_ms,
        },
        impersonate=_IMPERSONATE,
        allow_redirects=True,
    )


def _get(session: cffi_requests.Session, url: str) -> BeautifulSoup | None:
    try:
        r = session.get(url, impersonate=_IMPERSONATE, timeout=15)
        r.raise_for_status()
        if "anubis_challenge" in r.text:
            _solve_anubis(session, url)
            r = session.get(url, impersonate=_IMPERSONATE, timeout=15)
        return BeautifulSoup(r.content, "lxml")
    except Exception:
        return None


# ── URL gathering ─────────────────────────────────────────────────


def _gather_obs_paths(
    session: cffi_requests.Session,
    days_back: int,
    rarity: int = 3,
    expand_species: bool = True,
    progress_callback: Callable[[float, str], None] | None = None,
    pct_start: float = 0.0,
    pct_end: float = 0.1,
) -> list[str]:
    all_paths: list[str] = []
    for i in range(days_back):
        target = date.today() - timedelta(days=i)
        if progress_callback:
            progress_callback(
                pct_start + (pct_end - pct_start) * i / days_back,
                f"Daylist rarity={rarity} {target}…",
            )
        all_paths.extend(
            _get_obs_paths_for_date(session, target, rarity, expand_species)
        )

    seen: set[str] = set()
    return [p for p in all_paths if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]


def _get_obs_paths_for_date(
    session: cffi_requests.Session,
    target_date: date,
    rarity: int = 3,
    expand_species: bool = True,
) -> list[str]:
    url = (
        f"{BASE_URL}/fieldwork/observations/daylist/"
        f"?date={target_date}"
        f"&species_group={SPECIES_GROUP}&rarity={rarity}&search="
    )
    soup = _get(session, url)
    if not soup:
        return []

    paths: list[str] = []
    for row in soup.find_all("tr", class_=["even", "odd"]):
        for a in row.find_all("a", href=True):
            href: str = a["href"]
            if "/species/" in href and "/observations/" in href:
                if expand_species:
                    paths.extend(_get_species_obs_paths(session, href))
            elif href.startswith("/observation/"):
                paths.append(href)
    return paths


def _get_species_obs_paths(session: cffi_requests.Session, path: str) -> list[str]:
    soup = _get(session, f"{BASE_URL}{path}")
    if not soup:
        return []

    latest: dict[str, dict] = {}
    for row in soup.find_all("tr", class_=["even", "odd"]):
        links = row.find_all("a", href=True)
        obs_path = next(
            (a["href"] for a in links if a["href"].startswith("/observation/")),
            None,
        )
        if not obs_path:
            continue
        loc_link = next((a for a in links if "/locations/" in a["href"]), None)
        location = loc_link.get_text(strip=True) if loc_link else obs_path
        first_td = row.find("td")
        row_date = first_td.get_text(strip=True) if first_td else ""
        if location not in latest or row_date > latest[location]["date"]:
            latest[location] = {"date": row_date, "path": obs_path}

    return [v["path"] for v in latest.values()]


# ── Detail scraping ───────────────────────────────────────────────


def _scrape_observation(session: cffi_requests.Session, path: str) -> dict | None:
    url = f"{BASE_URL}{path}"
    soup = _get(session, url)
    if not soup:
        return None

    gps_span = soup.find("span", class_="teramap-coordinates-coords")
    if not gps_span:
        return None
    try:
        parts = gps_span.get_text(strip=True).split(", ")
        lat, lon = float(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return None

    title_tag = soup.find("title")
    bird_name = title_tag.text.split("-")[0].strip() if title_tag else "Onbekend"

    location = _text_after_icon(soup, "fas fa-map-marker-alt fa-fw") or "Onbekend"

    obs_date_raw = _text_after_icon(soup, "fas fa-calendar-alt fa-fw") or ""
    obs_date, obs_time = _parse_date_time(obs_date_raw)

    # Count: try common icon patterns, fall back to looking for "Aantal" label
    count = _extract_count(soup)

    # Photo: check if a photo/media block is present on the page
    photo = bool(
        soup.find("div", class_=re.compile(r"photo|media|gallery", re.I)) or
        soup.find("a", href=re.compile(r"\.(jpg|jpeg|png|webp)", re.I))
    )

    rarity = _extract_rarity(soup)

    return {
        "bird_name": bird_name,
        "location": location,
        "date": obs_date,
        "obs_time": obs_time,
        "count": count,
        "photo": photo,
        "latitude": lat,
        "longitude": lon,
        "url": url,
        "rarity": rarity,
    }


# ── Helpers ───────────────────────────────────────────────────────


def _parse_date_time(raw: str) -> tuple[str, str]:
    """Return (YYYY-MM-DD, HH:MM) from a raw Dutch date string."""
    if not raw:
        return "", ""

    # Extract time HH:MM
    tm = re.search(r"\b(\d{1,2}):(\d{2})\b", raw)
    time_str = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else ""

    # DD-MM-YYYY or DD/MM/YYYY
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", raw)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}", time_str

    # Already ISO: YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", raw)
    if m:
        return m.group(0), time_str

    # DD MONTH YYYY (Dutch long or short month name)
    m = re.search(r"\b(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})\b", raw)
    if m:
        mon = _DUTCH_MONTHS.get(m.group(2).lower(), "")
        if mon:
            return f"{m.group(3)}-{mon}-{m.group(1).zfill(2)}", time_str

    return "", time_str


def _extract_count(soup: BeautifulSoup) -> int:
    for icon_cls in [
        "fas fa-hashtag fa-fw",
        "fas fa-sort-amount-up fa-fw",
        "fas fa-layer-group fa-fw",
    ]:
        text = _text_after_icon(soup, icon_cls)
        if text:
            m = re.match(r"(\d+)", text.strip())
            if m:
                return int(m.group(1))

    # Look for "Aantal" label in the detail table
    el = soup.find(string=re.compile(r"^\s*Aantal\s*$", re.I))
    if el:
        td = el.find_next("td")
        if td:
            m = re.match(r"(\d+)", td.get_text(strip=True))
            if m:
                return int(m.group(1))

    return 1


def _extract_rarity(soup: BeautifulSoup) -> int:
    icon = soup.find("i", class_=lambda c: c and "rare-" in c)
    if icon:
        for cls in icon.get("class", []):
            if cls.startswith("rare-"):
                try:
                    return int(cls.split("-")[1])
                except ValueError:
                    pass
    block = soup.find(class_="pull-right")
    if block:
        text = block.get_text(" ", strip=True).lower()
        for phrase, val in _RARITY_TEXT.items():
            if phrase in text:
                return val
    return 3


def _text_after_icon(soup: BeautifulSoup, icon_class: str) -> str | None:
    icon = soup.find("i", class_=icon_class)
    if not icon:
        return None
    td = icon.find_next("td")
    if not td:
        return None
    a = td.find("a")
    return (a or td).get_text(strip=True) or None
