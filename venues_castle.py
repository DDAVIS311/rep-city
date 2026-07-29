"""Scraper for The Castle Cinema, London (Hackney) — https://thecastlecinema.com

Data source:
  * Showtimes: schema.org ScreeningEvent JSON-LD embedded server-side in the
    homepage HTML (the "what's on" week). Each block gives title, startDate
    (Europe/London wall time), duration, a per-performance booking URL
    (/bookings/{perfCode}/ -> redirects to the Admit One ticket page), and the
    film's programme-page URL.
  * Per-film metadata (synopsis / director / runtime / format): the programme
    detail page HTML, parsed once per distinct film via stable CSS classes
    (.film-director, .film-synopsis, .film-duration) plus format-token sniffing.

Stack: requests + BeautifulSoup + stdlib zoneinfo. No JS/Playwright needed —
the homepage is prerendered on Netlify and the JSON-LD is in the raw HTML.
"""

import re
import json
import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from datetime import datetime

BASE = "https://thecastlecinema.com"
TZ = ZoneInfo("Europe/London")
VENUE = "The Castle Cinema"
VENUE_SHORT = "Castle"
CITY = "London"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120 Safari/537.36"}

FORMAT_PATTERNS = [
    (re.compile(r'\b70\s?mm\b', re.I), "70mm"),
    (re.compile(r'\b35\s?mm\b', re.I), "35mm"),
    (re.compile(r'\b16\s?mm\b', re.I), "16mm"),
    (re.compile(r'\b4K\s+DCP\b', re.I), "4K DCP"),
    (re.compile(r'\b4K\b', re.I), "4K"),
    (re.compile(r'\bDCP\b'), "DCP"),
]


def _iso_dur_to_min(dur):
    """'PT172M' / 'PT2H5M' -> minutes int, or ''."""
    if not dur:
        return ""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?', dur)
    if not m:
        return ""
    h = int(m.group(1) or 0)
    mm = int(m.group(2) or 0)
    total = h * 60 + mm
    return str(total) if total else ""


def _fmt_time(dt):
    """datetime -> '7:00 PM' (no leading zero, cross-platform)."""
    h = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h}:{dt.minute:02d} {ampm}"


def _detect_format(*texts):
    blob = " ".join(t for t in texts if t)
    for pat, label in FORMAT_PATTERNS:
        if pat.search(blob):
            return label
    return ""


def _prog_id(url):
    m = re.search(r'/programme/(\d+)', url or "")
    return m.group(1) if m else None


def _fetch(url, session):
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def _parse_screening_events(html):
    events = []
    for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("@type") == "ScreeningEvent":
            events.append(d)
    return events


def _programme_meta(prog_url, session, cache):
    """Return dict(description, director, runtime, format) for a film, cached."""
    pid = _prog_id(prog_url)
    key = pid or prog_url
    if key in cache:
        return cache[key]
    meta = {"description": "", "director": "", "runtime": "", "format": ""}
    try:
        # normalise to absolute
        url = prog_url if prog_url.startswith("http") else BASE + prog_url
        soup = BeautifulSoup(_fetch(url, session), "html.parser")
        syn = soup.find(class_="film-synopsis") or soup.find(class_="film-description")
        if syn:
            meta["description"] = syn.get_text(" ", strip=True)
        d = soup.find(class_="film-director")
        if d:
            meta["director"] = d.get_text(" ", strip=True)
        dur = soup.find(class_="film-duration")
        if dur:
            m = re.search(r'(\d+)', dur.get_text())
            if m:
                meta["runtime"] = m.group(1)
        meta["format"] = _detect_format(meta["description"], soup.title.get_text() if soup.title else "")
    except requests.RequestException:
        pass
    cache[key] = meta
    return meta


def scrape_castle():
    """Scrape The Castle Cinema. Returns list of dicts, one per showtime."""
    session = requests.Session()
    home = _fetch(BASE + "/", session)
    events = _parse_screening_events(home)

    meta_cache = {}
    rows = []
    for e in events:
        start = e.get("startDate")
        if not start:
            continue
        try:
            dt = datetime.fromisoformat(start).replace(tzinfo=TZ)
        except ValueError:
            continue

        title = (e.get("name") or "").strip()
        booking_url = e.get("url") or e.get("@id") or ""
        prog_url = (e.get("workPresented") or {}).get("url", "")

        meta = _programme_meta(prog_url, session, meta_cache) if prog_url else \
            {"description": "", "director": "", "runtime": "", "format": ""}

        runtime = _iso_dur_to_min(e.get("duration")) or meta["runtime"]
        fmt = _detect_format(title) or meta["format"]

        rows.append({
            "title": title,
            "date": dt.strftime("%Y-%m-%d"),
            "time": _fmt_time(dt),
            "format": fmt,
            "venue": VENUE,
            "venue_short": VENUE_SHORT,
            "url": booking_url,
            "city": CITY,
            "description": meta["description"],
            "runtime": runtime,
            "director": meta["director"],
        })

    rows.sort(key=lambda r: (r["date"], r["time"]))
    return rows


if __name__ == "__main__":
    data = scrape_castle()
    print(f"TOTAL SHOWTIMES: {len(data)}")
    if data:
        days = sorted(set(r["date"] for r in data))
        print(f"DATE RANGE: {days[0]} .. {days[-1]}  ({len(days)} days)")
        filled = lambda k: sum(1 for r in data if r[k])
        for k in ("description", "director", "runtime", "format", "url"):
            print(f"  {k:12s}: {filled(k)}/{len(data)} filled")
    print("\n=== SAMPLE ROWS ===")
    for r in data[:8]:
        print(json.dumps(r, ensure_ascii=False))
