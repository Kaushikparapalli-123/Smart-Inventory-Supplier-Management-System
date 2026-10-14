"""
weather.py
Fetches weather data from Open-Meteo (open-meteo.com) — chosen
because it's free, requires no API key or signup, and covers both
historical archive data and forecasts, so there's nothing that can
expire or hit a paywall mid-project.

Every function here fails gracefully: if the network call doesn't
succeed for any reason (offline, API down, date out of range), it
returns an empty result rather than raising, so the rest of the app
keeps working off historical averages alone.

Note: this module makes real network calls, so it can only be
exercised on a machine with normal internet access.
"""

import requests
import time


HEAT_THRESHOLD_C = 35.0   # "extreme heat" — a common Australian heatwave reference
RAIN_THRESHOLD_MM = 5.0   # daily rainfall counted as a "rainy day" for demand purposes

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Avoid repeating slow network calls on every page change.
_HISTORICAL_CACHE = {}
_FORECAST_CACHE = {}
HISTORICAL_TTL_SECONDS = 6 * 60 * 60   # 6 hours
FORECAST_TTL_SECONDS = 10 * 60          # 10 minutes

def _cached(cache, key, ttl):
    item = cache.get(key)
    if not item:
        return None
    saved_at, value = item
    if time.monotonic() - saved_at > ttl:
        cache.pop(key, None)
        return None
    return value

def _save(cache, key, value):
    cache[key] = (time.monotonic(), value)
    return value

def clear_cache():
    _HISTORICAL_CACHE.clear()
    _FORECAST_CACHE.clear()


def _classify(rain_mm, max_temp_c):
    return {
        "rain_mm": rain_mm,
        "max_temp_c": max_temp_c,
        "is_rainy": rain_mm is not None and rain_mm >= RAIN_THRESHOLD_MM,
        "is_extreme_heat": max_temp_c is not None and max_temp_c >= HEAT_THRESHOLD_C,
    }


def get_historical_weather(start_date, end_date, lat, lon):
    """Return historical weather, cached so page navigation stays fast."""
    key = (start_date.isoformat(), end_date.isoformat(), round(float(lat), 4), round(float(lon), 4))
    cached = _cached(_HISTORICAL_CACHE, key, HISTORICAL_TTL_SECONDS)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            ARCHIVE_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "daily": "precipitation_sum,temperature_2m_max",
                "timezone": "auto",
            },
            timeout=3,
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]
        result = {
            d: _classify(r, t)
            for d, r, t in zip(daily["time"], daily["precipitation_sum"], daily["temperature_2m_max"])
        }
        return _save(_HISTORICAL_CACHE, key, result)
    except Exception as exc:
        print(f"Open-Meteo historical weather unavailable: {exc}")
        return {}


def get_forecast_weather(days_ahead, lat, lon):
    """Return forecast weather, cached for 10 minutes."""
    key = (int(days_ahead), round(float(lat), 4), round(float(lon), 4))
    cached = _cached(_FORECAST_CACHE, key, FORECAST_TTL_SECONDS)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "precipitation_sum,temperature_2m_max",
                "forecast_days": days_ahead,
                "timezone": "auto",
            },
            timeout=3,
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]
        result = {
            d: _classify(r, t)
            for d, r, t in zip(daily["time"], daily["precipitation_sum"], daily["temperature_2m_max"])
        }
        return _save(_FORECAST_CACHE, key, result)
    except Exception as exc:
        print(f"Open-Meteo forecast unavailable: {exc}")
        return {}
