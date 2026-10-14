"""
calendar_context.py

Calendar context for the Smart Inventory & Supplier Management system.
Current prototype: a pizza shop in South Australia.

Provides:
- weekend detection
- Australian public-holiday detection
- South Australian school-holiday detection

Public holidays come from the `holidays` Python package.

South Australian school term dates are fetched automatically from the
official SA Department for Education term-date page. School-holiday ranges
are derived from the gaps between terms, so this file does not need a manual
2027/2028/2029 date update. When the Department publishes new years on the
same page, the parser picks them up automatically.

If the Department page cannot be reached or a requested year is not yet
published, school-holiday data is marked unavailable and the forecast treats
that factor as neutral rather than guessing or crashing.
"""

from datetime import date, timedelta
from functools import lru_cache
import re

import holidays
import requests
from bs4 import BeautifulSoup


# Preset locations: (latitude, longitude, Australian state code)
# Adelaide is the current prototype location.
LOCATION_PRESETS = {
    "Adelaide, SA": (-34.93, 138.60, "SA"),
    "Sunshine Coast, QLD": (-26.65, 153.07, "QLD"),
    "Brisbane, QLD": (-27.47, 153.03, "QLD"),
    "Sydney, NSW": (-33.87, 151.21, "NSW"),
    "Melbourne, VIC": (-37.81, 144.96, "VIC"),
    "Perth, WA": (-31.95, 115.86, "WA"),
}
DEFAULT_LOCATION = "Adelaide, SA"

SA_TERM_DATES_URL = (
    "https://www.education.sa.gov.au/students/"
    "term-dates-south-australian-state-schools"
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

DATE_TOKEN_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)",
    re.IGNORECASE,
)


def _parse_term_range(text: str, year: int):
    """Parse strings such as '27 January to 10 April' into two dates."""
    matches = DATE_TOKEN_RE.findall(text.replace("\xa0", " "))
    if len(matches) < 2:
        return None
    (start_day, start_month), (end_day, end_month) = matches[:2]
    return (
        date(year, MONTHS[start_month.lower()], int(start_day)),
        date(year, MONTHS[end_month.lower()], int(end_day)),
    )


@lru_cache(maxsize=1)
def _get_sa_term_ranges_by_year():
    """
    Fetch official South Australian state-school term dates.

    Returns:
        {year: [(term1_start, term1_end), ... (term4_start, term4_end)]}

    The result is cached for the life of the Python process, so Streamlit does
    not repeatedly download the government page during every forecast loop.
    """
    try:
        response = requests.get(
            SA_TERM_DATES_URL,
            timeout=3,
            headers={"User-Agent": "SmartInventoryPrototype/1.0"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        term_map = {}

        # Current-year terms are presented as a heading followed by bullet items.
        current_heading = soup.find(
            lambda tag: tag.name in {"h2", "h3"}
            and re.search(r"Term dates for\s+20\d{2}", tag.get_text(" ", strip=True), re.I)
        )
        if current_heading:
            year_match = re.search(r"(20\d{2})", current_heading.get_text(" ", strip=True))
            if year_match:
                current_year = int(year_match.group(1))
                terms = [None, None, None, None]
                # Look through list items on the page; only Term 1..4 entries match.
                for li in soup.find_all("li"):
                    text = li.get_text(" ", strip=True)
                    m = re.match(r"Term\s*([1-4])\s*[–—-]\s*(.+)", text, re.I)
                    if not m:
                        continue
                    parsed = _parse_term_range(m.group(2), current_year)
                    if parsed:
                        terms[int(m.group(1)) - 1] = parsed
                if all(terms):
                    term_map[current_year] = terms

        # Future/previous years are presented in tables with a year plus 4 term cells.
        for row in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
            if len(cells) < 5 or not re.fullmatch(r"20\d{2}", cells[0].strip()):
                continue
            year = int(cells[0].strip())
            terms = []
            for cell in cells[1:5]:
                parsed = _parse_term_range(cell, year)
                if parsed:
                    terms.append(parsed)
            if len(terms) == 4:
                term_map[year] = terms

        return term_map
    except Exception as exc:
        print(f"SA school-term calendar unavailable: {exc}")
        return {}


def _sa_school_holiday_ranges(year: int):
    """Derive school-holiday ranges from official SA term boundaries."""
    terms = _get_sa_term_ranges_by_year().get(year)
    if not terms or len(terms) != 4:
        return []

    (t1_start, t1_end), (t2_start, t2_end), (t3_start, t3_end), (t4_start, t4_end) = terms

    ranges = []
    # Summer break before Term 1.
    if t1_start > date(year, 1, 1):
        ranges.append((date(year, 1, 1), t1_start - timedelta(days=1)))
    # Autumn, winter and spring breaks.
    ranges.extend(
        [
            (t1_end + timedelta(days=1), t2_start - timedelta(days=1)),
            (t2_end + timedelta(days=1), t3_start - timedelta(days=1)),
            (t3_end + timedelta(days=1), t4_start - timedelta(days=1)),
        ]
    )
    # Summer break after Term 4, within this calendar year.
    if t4_end < date(year, 12, 31):
        ranges.append((t4_end + timedelta(days=1), date(year, 12, 31)))

    return [(start, end) for start, end in ranges if start <= end]


def has_school_holiday_data(d: date, state: str = "SA") -> bool:
    """Whether an official school-term calendar is available for this date/year."""
    if state != "SA":
        return False
    terms = _get_sa_term_ranges_by_year().get(d.year)
    return bool(terms and len(terms) == 4)


def is_school_holiday(d: date, state: str = "SA") -> bool:
    """Return True when the date falls outside official SA school terms."""
    if not has_school_holiday_data(d, state):
        return False
    return any(start <= d <= end for start, end in _sa_school_holiday_ranges(d.year))



@lru_cache(maxsize=32)
def _public_holidays_for_year(state: str, year: int):
    """Cache public-holiday objects instead of rebuilding them for every product/day."""
    return holidays.Australia(state=state, years=year)

def day_context(d: date, state: str = "SA") -> dict:
    """Return demand-relevant calendar flags for one date."""
    au_holidays = _public_holidays_for_year(state, d.year)
    holiday_name = au_holidays.get(d)
    school_data_available = has_school_holiday_data(d, state)

    return {
        "date": d.isoformat(),
        "is_weekend": d.weekday() >= 5,
        "is_public_holiday": holiday_name is not None,
        "public_holiday_name": holiday_name,
        "is_school_holiday": (
            is_school_holiday(d, state) if school_data_available else False
        ),
        "school_holiday_data_available": school_data_available,
    }
