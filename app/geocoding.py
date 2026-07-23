"""
Postcode -> lat/lng, via postcodes.io (free, no API key, UK-only). Called
once at venue setup (spec §6.1: "a one-off geocoding lookup... turns each
venue's postcode into coordinates at venue setup") and re-callable if the
postcode is edited later. Best-effort: a failed/unreachable lookup leaves
venue.latitude/longitude NULL rather than blocking setup — the clock-in
distance check simply can't confirm location for that venue until it's
retried (mirrors the "decline gracefully" philosophy applied elsewhere).
"""

import requests

from app import config


def geocode_postcode(postcode: str):
    """Returns (lat, lng) or None."""
    postcode = (postcode or "").strip()
    if not postcode:
        return None
    try:
        resp = requests.get(f"{config.POSTCODES_IO_URL}/postcodes/{requests.utils.quote(postcode)}", timeout=5)
        if resp.status_code != 200:
            return None
        result = resp.json().get("result")
        if not result:
            return None
        return result["latitude"], result["longitude"]
    except (requests.RequestException, ValueError, KeyError):
        return None
