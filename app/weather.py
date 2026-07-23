"""
Weather forecast strip (spec §10) — Open-Meteo, free and keyless, avoiding
a new paid vendor for a nice-to-have differentiator. Cached in
weather_cache for a few hours so a busy grid page doesn't hammer the API
on every load.
"""

from datetime import datetime, timedelta

import requests

from app import config
from app.db import get_db


def get_week_forecast(venue_id: int, venue_lat, venue_lng, dates: list[str]) -> dict:
    """Returns {date_str: {"temperature_c": float, "weather_code": int} or None}."""
    db = get_db()
    result = {}
    stale_dates = []
    cutoff = (datetime.now() - timedelta(hours=config.WEATHER_CACHE_HOURS)).isoformat()

    for d in dates:
        cached = db.execute(
            "SELECT * FROM weather_cache WHERE venue_id = ? AND forecast_date = ? AND fetched_at >= ?",
            (venue_id, d, cutoff),
        ).fetchone()
        if cached:
            result[d] = {"temperature_c": cached["temperature_c"], "weather_code": cached["weather_code"]}
        else:
            stale_dates.append(d)
            result[d] = None

    if stale_dates and venue_lat is not None and venue_lng is not None:
        try:
            resp = requests.get(
                config.OPEN_METEO_URL,
                params={
                    "latitude": venue_lat, "longitude": venue_lng,
                    "daily": "temperature_2m_max,weather_code",
                    "timezone": "auto",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                daily = resp.json().get("daily", {})
                fetched_dates = daily.get("time", [])
                temps = daily.get("temperature_2m_max", [])
                codes = daily.get("weather_code", [])
                for i, fetched_date in enumerate(fetched_dates):
                    if fetched_date in stale_dates:
                        temp = temps[i] if i < len(temps) else None
                        code = codes[i] if i < len(codes) else None
                        db.execute(
                            """INSERT INTO weather_cache (venue_id, forecast_date, temperature_c, weather_code, fetched_at)
                               VALUES (?, ?, ?, ?, datetime('now'))
                               ON CONFLICT(venue_id, forecast_date) DO UPDATE SET
                               temperature_c = excluded.temperature_c, weather_code = excluded.weather_code,
                               fetched_at = excluded.fetched_at""",
                            (venue_id, fetched_date, temp, code),
                        )
                        result[fetched_date] = {"temperature_c": temp, "weather_code": code}
                db.commit()
        except requests.RequestException:
            pass  # best-effort — a stale/missing forecast just means no icon for that day

    return result
