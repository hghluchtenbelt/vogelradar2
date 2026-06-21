import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "vogelradar.db"

# Dutch province codes as they appear in a location like "... (UT)"
_PROVINCES = {
    "GR": "Groningen", "FR": "Friesland", "DR": "Drenthe",
    "OV": "Overijssel", "FL": "Flevoland", "GE": "Gelderland",
    "UT": "Utrecht", "NH": "Noord-Holland", "ZH": "Zuid-Holland",
    "ZL": "Zeeland", "NB": "Noord-Brabant", "LI": "Limburg",
}
_PROV_RE = re.compile(r"\(([A-Z]{2})\)\s*$")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscribers (
                fcm_token  TEXT PRIMARY KEY,
                lat        REAL,
                lng        REAL,
                wishlist   TEXT DEFAULT '[]',
                max_dist   INTEGER DEFAULT 50,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sightings (
                url        TEXT PRIMARY KEY,
                bird_name  TEXT NOT NULL,
                location   TEXT,
                date       TEXT,
                obs_time   TEXT DEFAULT '',
                count      INTEGER DEFAULT 1,
                photo      INTEGER DEFAULT 0,
                latitude   REAL NOT NULL,
                longitude  REAL NOT NULL,
                scraped_at TEXT NOT NULL,
                rarity     INTEGER DEFAULT 3
            )
            """
        )
        # Add columns that may be missing from older DBs
        existing = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(sightings)").fetchall()
        }
        for col, defn in [
            ("obs_time", "TEXT DEFAULT ''"),
            ("count", "INTEGER DEFAULT 1"),
            ("photo", "INTEGER DEFAULT 0"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE sightings ADD COLUMN {col} {defn}")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scraped_at ON sightings(scraped_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rarity_date ON sightings(rarity, date)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_notifications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                token     TEXT NOT NULL,
                bird_name TEXT NOT NULL,
                lat       REAL NOT NULL,
                lng       REAL NOT NULL,
                sent_at   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_notifications(sent_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                day             TEXT PRIMARY KEY,
                unique_species  INTEGER,
                total_sightings INTEGER,
                rare            INTEGER,
                very_rare       INTEGER,
                updated_at      TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS location_gemeente (
                location TEXT PRIMARY KEY,
                gemeente TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gemeente_daily (
                day            TEXT,
                gemeente       TEXT,
                unique_species INTEGER,
                rare           INTEGER,
                very_rare      INTEGER,
                PRIMARY KEY (day, gemeente)
            )
            """
        )
        conn.commit()


def insert_sightings(sightings: list[dict]) -> int:
    if not sightings:
        return 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    rows = [{**s, "scraped_at": now} for s in sightings]
    with _connect() as conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO sightings
                (url, bird_name, location, date, obs_time, count,
                 photo, latitude, longitude, scraped_at, rarity)
            VALUES
                (:url, :bird_name, :location, :date, :obs_time, :count,
                 :photo, :latitude, :longitude, :scraped_at, :rarity)
            """,
            rows,
        )
        conn.commit()
        return cur.rowcount


def get_sightings(days_back: int = 7) -> list[dict]:
    """
    Tiered time window per rarity:
      rarity 4+3 (zeldzaam/zeer zeldzaam) → last 7 days
      rarity 2   (vrij zeldzaam)          → last 10 hours
      rarity 1   (algemeen)               → last 6 hours
    """
    now = datetime.utcnow()
    cut7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cut10 = (now - timedelta(hours=10)).isoformat(timespec="seconds")
    cut6  = (now - timedelta(hours=6)).isoformat(timespec="seconds")
    cols = """url, bird_name, location, date, obs_time,
                   count, photo, latitude, longitude, scraped_at,
                   COALESCE(rarity, 3) AS rarity"""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {cols}
            FROM   sightings
            WHERE  COALESCE(rarity,3) >= 3 AND date >= ?
            ORDER  BY scraped_at DESC
            """,
            (cut7,),
        ).fetchall()
        rows += conn.execute(
            f"""
            SELECT {cols}
            FROM   sightings
            WHERE  COALESCE(rarity,3) = 2 AND scraped_at >= ?
            ORDER  BY scraped_at DESC
            """,
            (cut10,),
        ).fetchall()
        rows += conn.execute(
            f"""
            SELECT {cols}
            FROM   sightings
            WHERE  COALESCE(rarity,3) = 1 AND scraped_at >= ?
            ORDER  BY scraped_at DESC
            """,
            (cut6,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_urls() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT url FROM sightings").fetchall()
    return {r["url"] for r in rows}


def is_empty() -> bool:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0


def get_latest_update() -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(scraped_at) FROM sightings").fetchone()
    return row[0] if row else None


def upsert_push_subscriber(
    token: str, lat: float, lng: float,
    wishlist: str, max_dist: int,
) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO push_subscribers
                (fcm_token, lat, lng, wishlist, max_dist, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fcm_token) DO UPDATE SET
                lat=excluded.lat, lng=excluded.lng,
                wishlist=excluded.wishlist,
                max_dist=excluded.max_dist,
                updated_at=excluded.updated_at
            """,
            (token, lat, lng, wishlist, max_dist, now),
        )
        conn.commit()


def delete_push_subscriber(token: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM push_subscribers WHERE fcm_token = ?", (token,)
        )
        conn.commit()


def get_push_subscribers() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT fcm_token, lat, lng, wishlist, max_dist
            FROM push_subscribers
            WHERE wishlist != '[]'
            """
        ).fetchall()
    return [dict(r) for r in rows]


def prune_empty_subscribers() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM push_subscribers WHERE wishlist = '[]'")
        conn.commit()


def prune_old_sightings(keep_days: int = 15) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sightings WHERE scraped_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount


def already_notified(token: str, bird_name: str, lat: float, lng: float,
                     radius_km: float = 5.0, window_hours: float = 1.0) -> bool:
    import math
    cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lat, lng FROM sent_notifications WHERE token=? AND bird_name=? AND sent_at>=?",
            (token, bird_name, cutoff),
        ).fetchall()
    for r in rows:
        dlat = math.radians(r["lat"] - lat)
        dlng = math.radians(r["lng"] - lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(r["lat"]))*math.sin(dlng/2)**2
        if 6371 * 2 * math.asin(math.sqrt(a)) <= radius_km:
            return True
    return False


def record_notification(token: str, bird_name: str, lat: float, lng: float) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_notifications (token, bird_name, lat, lng, sent_at) VALUES (?,?,?,?,?)",
            (token, bird_name, lat, lng, now),
        )
        conn.execute("DELETE FROM sent_notifications WHERE sent_at < ?", (cutoff,))
        conn.commit()


def record_daily_stats() -> None:
    """Snapshot today's stats; called each scraper run. Today's row updates
    live and freezes once the day passes, building lasting history."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now = datetime.utcnow().isoformat(timespec="seconds")
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT COUNT(DISTINCT bird_name) AS uniq,
                   COUNT(*)                  AS total,
                   SUM(CASE WHEN COALESCE(rarity,3)=3 THEN 1 ELSE 0 END) AS rare,
                   SUM(CASE WHEN COALESCE(rarity,3)=4 THEN 1 ELSE 0 END) AS vr
            FROM   sightings
            WHERE  date = ?
            """,
            (today,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO daily_stats
                (day, unique_species, total_sightings,
                 rare, very_rare, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                unique_species  = excluded.unique_species,
                total_sightings = excluded.total_sightings,
                rare            = excluded.rare,
                very_rare       = excluded.very_rare,
                updated_at      = excluded.updated_at
            """,
            (today, r["uniq"] or 0, r["total"] or 0,
             r["rare"] or 0, r["vr"] or 0, now),
        )
        conn.commit()


def get_daily_stats(days: int = 30) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_stats WHERE day >= ? ORDER BY day DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_area_ranking(days: int = 7) -> list[dict]:
    """Ranking per province over the last `days`: unique species and
    number of rare / very_rare sightings."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT location, bird_name, COALESCE(rarity,3) AS rarity
            FROM   sightings
            WHERE  date >= ?
            """,
            (cutoff,),
        ).fetchall()

    areas: dict[str, dict] = {}
    for r in rows:
        m = _PROV_RE.search(r["location"] or "")
        if not m:
            continue
        prov = _PROVINCES.get(m.group(1))
        if not prov:
            continue
        a = areas.setdefault(
            prov, {"species": set(), "rare": 0, "very_rare": 0}
        )
        a["species"].add(r["bird_name"])
        if r["rarity"] == 3:
            a["rare"] += 1
        elif r["rarity"] == 4:
            a["very_rare"] += 1

    ranking = [
        {
            "area": prov,
            "unique_species": len(a["species"]),
            "rare": a["rare"],
            "very_rare": a["very_rare"],
        }
        for prov, a in areas.items()
    ]
    ranking.sort(
        key=lambda x: (-x["unique_species"], -(x["rare"] + x["very_rare"]))
    )
    return ranking


def map_unmapped_locations() -> int:
    """Resolve the gemeente for every location not yet cached, via local
    point-in-polygon. Returns how many new locations were mapped."""
    from gemeente import gemeente_for
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT location, latitude, longitude
            FROM   sightings s
            WHERE  location IS NOT NULL AND location != ''
              AND  NOT EXISTS (SELECT 1 FROM location_gemeente g
                               WHERE g.location = s.location)
            GROUP  BY location
            """
        ).fetchall()
        n = 0
        for r in rows:
            gem = gemeente_for(r["latitude"], r["longitude"]) or ""
            conn.execute(
                "INSERT OR REPLACE INTO location_gemeente (location, gemeente)"
                " VALUES (?, ?)",
                (r["location"], gem),
            )
            n += 1
        conn.commit()
    return n


def _rank_query(group_col: str, join: str, days: int, limit: int) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    sql = f"""
        SELECT {group_col} AS name,
               COUNT(DISTINCT s.bird_name) AS uniq,
               SUM(CASE WHEN COALESCE(s.rarity,3)=3 THEN 1 ELSE 0 END) AS rare,
               SUM(CASE WHEN COALESCE(s.rarity,3)=4 THEN 1 ELSE 0 END) AS vr
        FROM   sightings s {join}
        WHERE  s.date >= ? AND {group_col} != ''
        GROUP  BY {group_col}
        ORDER  BY uniq DESC, (rare + vr) DESC
        LIMIT  ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return [
        {"name": r["name"], "unique_species": r["uniq"],
         "rare": r["rare"], "very_rare": r["vr"]}
        for r in rows
    ]


def get_gemeente_ranking(days: int = 7, limit: int = 10) -> list[dict]:
    return _rank_query(
        "g.gemeente",
        "JOIN location_gemeente g ON g.location = s.location",
        days, limit,
    )


def get_hotspot_ranking(days: int = 7, limit: int = 10) -> list[dict]:
    return _rank_query("s.location", "", days, limit)


def record_gemeente_daily() -> None:
    """Snapshot today's per-gemeente stats (cheap; ~one row per active
    gemeente per day) for the time-series dashboard."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT g.gemeente AS gem,
                   COUNT(DISTINCT s.bird_name) AS uniq,
                   SUM(CASE WHEN COALESCE(s.rarity,3)=3 THEN 1 ELSE 0 END) AS rare,
                   SUM(CASE WHEN COALESCE(s.rarity,3)=4 THEN 1 ELSE 0 END) AS vr
            FROM   sightings s
            JOIN   location_gemeente g ON g.location = s.location
            WHERE  s.date = ? AND g.gemeente != ''
            GROUP  BY g.gemeente
            """,
            (today,),
        ).fetchall()
        for r in rows:
            conn.execute(
                """
                INSERT INTO gemeente_daily
                    (day, gemeente, unique_species, rare, very_rare)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day, gemeente) DO UPDATE SET
                    unique_species = excluded.unique_species,
                    rare           = excluded.rare,
                    very_rare      = excluded.very_rare
                """,
                (today, r["gem"], r["uniq"], r["rare"], r["vr"]),
            )
        conn.commit()


def get_gemeente_history(gemeente: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT day, unique_species, rare, very_rare
            FROM   gemeente_daily WHERE gemeente = ? ORDER BY day
            """,
            (gemeente,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_hotspots_in_gemeente(
    gemeente: str, days: int = 7, limit: int = 25
) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.location AS name,
                   COUNT(DISTINCT s.bird_name) AS uniq,
                   SUM(CASE WHEN COALESCE(s.rarity,3)=3 THEN 1 ELSE 0 END) AS rare,
                   SUM(CASE WHEN COALESCE(s.rarity,3)=4 THEN 1 ELSE 0 END) AS vr
            FROM   sightings s
            JOIN   location_gemeente g ON g.location = s.location
            WHERE  g.gemeente = ? AND s.date >= ?
            GROUP  BY s.location
            ORDER  BY uniq DESC, (rare + vr) DESC
            LIMIT  ?
            """,
            (gemeente, cutoff, limit),
        ).fetchall()
    return [
        {"name": r["name"], "unique_species": r["uniq"],
         "rare": r["rare"], "very_rare": r["vr"]}
        for r in rows
    ]
