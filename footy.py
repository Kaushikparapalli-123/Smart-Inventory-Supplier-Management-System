"""Footy fixture alerts for the Smart Inventory app."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


ADELAIDE_TZ = ZoneInfo("Australia/Adelaide")
FEED_BASE = "https://fixturedownload.azurewebsites.net/feed/json"
AFL_TEAMS = {"adelaide crows", "port adelaide"}


def current_year():
    return datetime.now(ADELAIDE_TZ).year


def _value(row, *names):
    """Read a field even if the JSON feed changes spaces/capitalisation."""
    normalised = {
        str(key).lower().replace(" ", "").replace("_", ""): value
        for key, value in row.items()
    }
    for name in names:
        key = name.lower().replace(" ", "").replace("_", "")
        if key in normalised:
            return normalised[key]
    return None


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("matches", "fixtures", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _match_time(value):
    if not value:
        return None

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    # Fixture Download publishes its feed times in UTC unless a timezone is requested.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ADELAIDE_TZ)


def _fetch_fixture(session, competition, year):
    response = session.get(f"{FEED_BASE}/{competition.lower()}-{year}", timeout=12)
    response.raise_for_status()

    matches = []
    for row in _rows(response.json()):
        if not isinstance(row, dict):
            continue

        match_time = _match_time(_value(row, "Date", "date"))
        if not match_time or match_time.year != year:
            continue

        home = str(_value(row, "Home Team", "homeTeam", "home") or "TBC")
        away = str(_value(row, "Away Team", "awayTeam", "away") or "TBC")
        venue = str(_value(row, "Location", "Venue", "location", "venue") or "Venue TBC")
        round_name = str(_value(row, "Round Number", "Round", "roundNumber", "round") or "")

        if competition == "AFL":
            teams = {home.lower(), away.lower()}
            is_adelaide_team = bool(teams & AFL_TEAMS)
            is_final = "final" in round_name.lower() or "wildcard" in round_name.lower()
            if not (is_adelaide_team or is_final):
                continue

        matches.append(
            {
                "competition": competition,
                "round": round_name or "—",
                "datetime": match_time,
                "match": f"{home} vs {away}",
                "venue": venue,
            }
        )

    return matches


def current_date():
    return datetime.now(ADELAIDE_TZ).date()


def get_year_matches():
    """Return all published AFL/SANFL matches for the current year."""
    year = current_year()
    matches = []

    with requests.Session() as session:
        for competition in ("AFL", "SANFL"):
            try:
                matches.extend(_fetch_fixture(session, competition, year))
            except (requests.RequestException, ValueError):
                # One unavailable feed should not break the whole page.
                continue

    unique = {
        (m["competition"], m["datetime"], m["match"], m["venue"]): m
        for m in matches
    }
    return sorted(unique.values(), key=lambda match: match["datetime"])
