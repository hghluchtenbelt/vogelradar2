"""FCM push notification sender for Vogelradar."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from database import (
    get_push_subscribers, delete_push_subscriber,
    already_notified, record_notification,
)
from species import wish_keys

# Override with FIREBASE_CREDENTIALS when the file is mounted elsewhere
# (e.g. a secret mount in a container). Push is skipped if the file is absent.
_SERVICE_ACCOUNT = Path(os.environ.get("FIREBASE_CREDENTIALS")
                        or Path(__file__).parent / "firebase-service-account.json")
_RARITY_NUM = {
    "common": 1, "uncommon": 2, "rare": 3, "very_rare": 4
}


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _init_firebase():
    import firebase_admin
    from firebase_admin import credentials
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(_SERVICE_ACCOUNT))
        firebase_admin.initialize_app(cred)


def _messaging():
    """The firebase messaging module, initialised on first use."""
    _init_firebase()
    from firebase_admin import messaging
    return messaging


# FCM error classes that mean "this token will never work again".
_DEAD_TOKEN_ERRORS = {"UnregisteredError", "SenderIdMismatchError"}


def _is_dead_token(exc: Exception) -> bool:
    if type(exc).__name__ in _DEAD_TOKEN_ERRORS:
        return True
    err = str(exc).lower()
    return (
        "not found" in err
        or "unregistered" in err
        # A junk token is an invalid argument; without this it would be
        # retried on every run forever.
        or "not a valid fcm registration token" in err
    )


def _send(messaging, msg, token: str) -> str:
    """Send one FCM message. Returns 'ok', 'dead' (token gone) or 'error'."""
    try:
        messaging.send(msg)
        return "ok"
    except Exception as exc:
        print(f"[fcm] send error: {exc}", flush=True)
        if _is_dead_token(exc):
            delete_push_subscriber(token)
            return "dead"
        return "error"


def _is_coord(value, limit: float) -> bool:
    # bool is an int subclass; NaN fails the comparison, inf fails the bound.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and -limit <= value <= limit)


def _parse_subscriber(sub: dict) -> tuple[set[str], float, float, int]:
    """Validate one stored row. Raises ValueError/TypeError when it cannot
    be used, so a single bad registration never reaches the send loop."""
    lat, lng = sub["lat"], sub["lng"]
    if not (_is_coord(lat, 90) and _is_coord(lng, 180)):
        raise ValueError("coordinates missing or out of range")
    wishlist = json.loads(sub["wishlist"])
    if not isinstance(wishlist, list):
        raise TypeError("wishlist is not a list")
    max_dist = sub["max_dist"]
    if not isinstance(max_dist, int):
        raise TypeError("max_dist is not an integer")
    if max_dist < 0:
        raise ValueError("max_dist is negative")
    return {w for w in wishlist if isinstance(w, str)}, lat, lng, max_dist


def _matches(s: dict, wishlist: set[str]) -> bool:
    rarity = s.get("rarity", 3)
    return (
        # A wish for a species also covers its subspecies: "Notenkraker"
        # matches a report named "Dunsnavelnotenkraker".
        not wishlist.isdisjoint(wish_keys(s["bird_name"]))
        or (rarity >= 4 and "__very_rare__" in wishlist)
        or (rarity >= 3 and "__rare__" in wishlist)
    )


def _notify_subscriber(
    messaging, token: str, wishlist: set[str],
    lat: float, lng: float, max_dist: int, sightings: list[dict],
) -> None:
    for s in sightings:
        if not _matches(s, wishlist):
            continue
        dist = _haversine(lat, lng, s["latitude"], s["longitude"])
        if max_dist > 0 and dist > max_dist:
            continue
        if already_notified(token, s["bird_name"],
                            s["latitude"], s["longitude"]):
            continue

        dist_str = f"{round(dist)} km van jou · " if max_dist > 0 else ""
        when = s.get("date", "")
        obs_time = s.get("obs_time", "")
        if obs_time:
            when = f"{when} · {obs_time}" if when else obs_time
        body = f"📍 {s.get('location', '')} · {dist_str}{when}"
        msg = messaging.Message(
            data={
                "title": f"{s['bird_name']} gespot!",
                "body": body,
                "bird_name": s["bird_name"],
                "url": s.get("url", ""),
                "lat": str(s.get("latitude", "")),
                "lng": str(s.get("longitude", "")),
            },
            token=token,
        )
        status = _send(messaging, msg, token)
        if status == "ok":
            record_notification(token, s["bird_name"],
                                s["latitude"], s["longitude"])
        elif status == "dead":
            return


def send_push_notifications(new_sightings: list[dict]) -> None:
    if not new_sightings or not _SERVICE_ACCOUNT.exists():
        return

    subscribers = get_push_subscribers()
    if not subscribers:
        return
    messaging = _messaging()

    # A sighting without usable coordinates or name cannot be matched.
    sightings = [
        s for s in new_sightings
        if s.get("bird_name")
        and _is_coord(s.get("latitude"), 90)
        and _is_coord(s.get("longitude"), 180)
    ]

    for sub in subscribers:
        token = sub["fcm_token"]
        try:
            wishlist, lat, lng, max_dist = _parse_subscriber(sub)
        except (ValueError, TypeError, KeyError) as exc:
            # The row can never be served; remove it instead of failing on
            # it every run. Tokens are never logged.
            print(f"[fcm] dropping malformed subscriber: {exc}", flush=True)
            delete_push_subscriber(token)
            continue
        try:
            _notify_subscriber(messaging, token, wishlist, lat, lng,
                               max_dist, sightings)
        except Exception as exc:
            # One subscriber must never stop the others from being served.
            print(f"[fcm] subscriber skipped after error: {exc}", flush=True)
