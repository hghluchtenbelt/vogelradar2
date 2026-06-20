"""FCM push notification sender for Vogelradar."""
from __future__ import annotations

import json
import math
from pathlib import Path

from database import get_push_subscribers, delete_push_subscriber

_SERVICE_ACCOUNT = Path(__file__).parent / "firebase-service-account.json"
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


def send_push_notifications(new_sightings: list[dict]) -> None:
    if not new_sightings or not _SERVICE_ACCOUNT.exists():
        return

    _init_firebase()
    from firebase_admin import messaging

    subscribers = get_push_subscribers()
    if not subscribers:
        return

    for sub in subscribers:
        wishlist = set(json.loads(sub["wishlist"]))
        max_dist = sub["max_dist"]

        for s in new_sightings:
            rarity = s.get("rarity", 3)
            match = (
                s["bird_name"] in wishlist
                or (rarity >= 4 and "__very_rare__" in wishlist)
                or (rarity >= 3 and "__rare__" in wishlist)
            )
            if not match:
                continue

            dist = _haversine(
                sub["lat"], sub["lng"], s["latitude"], s["longitude"]
            )
            if max_dist > 0 and dist > max_dist:
                continue

            dist_str = f"{round(dist)} km van jou · " if max_dist > 0 else ""
            msg = messaging.Message(
                notification=messaging.Notification(
                    title=f"🦅 {s['bird_name']} gespot!",
                    body=f"📍 {s.get('location', '')} · {dist_str}{s.get('date', '')}",
                ),
                data={
                    "url": s.get("url", ""),
                    "lat": str(s.get("latitude", "")),
                    "lng": str(s.get("longitude", "")),
                },
                token=sub["fcm_token"],
            )
            try:
                messaging.send(msg)
            except Exception as exc:
                print(f"[fcm] send error: {exc}", flush=True)
                err = str(exc).lower()
                if "not found" in err or "unregistered" in err:
                    delete_push_subscriber(sub["fcm_token"])
