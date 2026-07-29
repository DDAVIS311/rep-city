"""Scraper for The Garden Cinema, London (Covent Garden).

Data source: the site homepage https://www.thegardencinema.co.uk/ which server-renders
the full "by date" what's-on schedule as static HTML (no JS / no Jacro plugin).
One request returns every published showtime.
"""

import re
import datetime as dt

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:  # pragma: no cover
    LONDON = None

BASE = "https://www.thegardencinema.co.uk/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Known film-format tokens, normalized to the target vocabulary.
_FORMAT_PATTERNS = [
    (re.compile(r"\b70\s*mm\b", re.I), "70mm"),
    (re.compile(r"\b35\s*mm\b", re.I), "35mm"),
    (re.compile(r"\b16\s*mm\b", re.I), "16mm"),
    (re.compile(r"\b4k\s*dcp\b", re.I), "4K DCP"),
    (re.compile(r"\b4k\b", re.I), "4K"),
    (re.compile(r"\bdcp\b", re.I), "DCP"),
    (re.compile(r"\bdigital\b", re.I), "Digital"),
]


def _today_london():
    if LONDON:
        return dt.datetime.now(LONDON).date()
    return dt.date.today()


def _resolve_date(day, mon, today):
    """Resolve a (day, month) with no year to the next occurrence >= today (London)."""
    for year in (today.year, today.year + 1, today.year + 2):
        try:
            cand = dt.date(year, mon, day)
        except ValueError:
            continue
        if cand >= today:
            return cand
    return dt.date(today.year, mon, day)


def _parse_date_title(text, today):
    """'Wed 29 Jul' -> date. Returns None if unparseable."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})", text)
    if not m:
        return None
    day = int(m.group(1))
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    return _resolve_date(day, mon, today)


def _to_12h(hhmm):
    """'18:00' -> '6:00 PM'."""
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mi:02d} {ap}"


def _detect_format(*texts):
    blob = " ".join(t for t in texts if t)
    for pat, norm in _FORMAT_PATTERNS:
        if pat.search(blob):
            return norm
    return ""


def _parse_stats(stats):
    """'Sophy Romvari, Canada, Hungary, 2025, 90m.' -> (director, runtime)."""
    stats = re.sub(r"\s+", " ", stats or "").strip().rstrip(".")
    director, runtime = "", ""
    if stats:
        parts = [p.strip() for p in stats.split(",") if p.strip()]
        # Director = first segment, unless it is a bare year/runtime.
        if parts and not re.fullmatch(r"\d{4}", parts[0]) and not re.fullmatch(r"\d+m", parts[0]):
            director = parts[0]
    rt = re.search(r"(\d+)\s*m\b", stats)
    if rt:
        runtime = f"{rt.group(1)} min"
    return director, runtime


def scrape_garden():
    """Return one dict per showtime for The Garden Cinema."""
    resp = requests.get(BASE, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    today = _today_london()

    rows = []
    by_date = soup.select_one(".films-list__by-date")
    if not by_date:
        return rows

    for film in by_date.select(".films-list__by-date__film"):
        title_el = film.select_one(".films-list__by-date__film__title")
        if not title_el:
            continue
        # Title without the trailing rating badge.
        rating_el = title_el.select_one(".films-list__by-date__film__rating")
        if rating_el:
            rating_el.extract()
        title = title_el.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()

        link_el = title_el.select_one("a[href]")
        detail_url = link_el["href"].strip() if link_el else ""

        syn_el = film.select_one(".films-list__by-date__film__synopsis")
        description = re.sub(r"\s+", " ", syn_el.get_text(" ", strip=True)).strip() if syn_el else ""

        stats_el = film.select_one(".films-list__by-date__film__stats")
        director, runtime = _parse_stats(stats_el.get_text(" ", strip=True) if stats_el else "")

        for panel in film.select(".screening-panel"):
            dt_el = panel.select_one(".screening-panel__date-title")
            date = _parse_date_title(dt_el.get_text(" ", strip=True), today) if dt_el else None
            if not date:
                continue

            # Event/format tags on this panel (class-based, e.g. ext-intro).
            tag_txt = " ".join(
                " ".join(t.get("class") or []) for t in panel.select(".screening-tag")
            )
            fmt = _detect_format(title, tag_txt, " ".join(panel.get("class") or []))

            for st in panel.select(".screening-time"):
                a = st.select_one("a[href]")
                tnode = a.get_text(" ", strip=True) if a else st.get_text(" ", strip=True)
                tm = re.search(r"\d{1,2}:\d{2}", tnode)
                if not tm:
                    continue
                time12 = _to_12h(tm.group(0))
                url = a["href"].strip() if a and a.has_attr("href") else detail_url

                rows.append({
                    "title": title,
                    "date": date.isoformat(),
                    "time": time12,
                    "format": fmt,
                    "venue": "The Garden Cinema",
                    "venue_short": "Garden",
                    "url": url,
                    "city": "London",
                    "description": description,
                    "runtime": runtime,
                    "director": director,
                })

    return rows


if __name__ == "__main__":
    import json
    data = scrape_garden()
    print(f"TOTAL SHOWTIMES: {len(data)}")
    if data:
        ds = sorted(r["date"] for r in data)
        print(f"DATE RANGE: {ds[0]} .. {ds[-1]}")
    print(json.dumps(data[:8], indent=2, ensure_ascii=False))
