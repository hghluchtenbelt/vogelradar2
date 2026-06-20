import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "vogelradar.db"


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
      rarity 4+3 → last 7 days
      rarity 1+2 → last 24 hours
    """
    now = datetime.utcnow()
    cut7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cut24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT url, bird_name, location, date, obs_time,
                   count, photo, latitude, longitude, scraped_at,
                   COALESCE(rarity, 3) AS rarity
            FROM   sightings
            WHERE  (COALESCE(rarity,3) >= 3 AND date >= ?)
            ORDER  BY scraped_at DESC
            """,
            (cut7,),
        ).fetchall()
        rows += conn.execute(
            """
            SELECT url, bird_name, location, date, obs_time,
                   count, photo, latitude, longitude, scraped_at,
                   COALESCE(rarity, 3) AS rarity
            FROM   sightings
            WHERE  COALESCE(rarity,3) IN (1,2) AND scraped_at >= ?
            ORDER  BY scraped_at DESC
            """,
            (cut24,),
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
