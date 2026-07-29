"""
Electric Cinema Portobello (London) scraper.

Output schema (one dict per showtime):
    {title, date, time, format, venue, venue_short, url, city,
     description, runtime, director}

Data source
-----------
electriccinema.co.uk is a WordPress site whose listings are client-rendered by a
custom <ec-programme> web component (Superwire ticketing platform, Vista box
office backend). The component pulls a single structured JSON feed:

    https://www.electriccinema.co.uk/data/data.json

which contains every cinema, film and screening for both Electric locations.
Portobello is cinema id 603 (White City is 602); we keep only 603. Each
screening carries local date `d` (YYYY-MM-DD) and time `t` (HH:MM, Europe/London),
a film id, a `bookable` flag and a booking `link` (/tickets/{id}).

Per-film runtime is not in the feed, so we enrich it once per distinct film from
the site's server-side render endpoint:

    /wp-json/superwire/v1/path/film/{slug}/   -> post.content.duration (minutes)

which also yields the fullest synopsis + director. No JS/Playwright needed.
"""

import re
import json
from datetime import date
from zoneinfo import ZoneInfo

import requests

LONDON = ZoneInfo("Europe/London")
BASE = "https://www.electriccinema.co.uk"
DATA_URL = BASE + "/data/data.json"
DETAIL_URL = BASE + "/wp-json/superwire/v1/path{path}"  # path e.g. /film/the-odyssey/
PORTOBELLO_ID = "603"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

_FMT_RE = re.compile(r"\b(70mm|35mm|16mm|4k dcp|2k dcp|dcp|4k|digital)\b", re.I)


def _norm_fmt(text):
    t = (text or "").lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "4k dcp" in t: return "4K DCP"
    if "2k dcp" in t: return "2K DCP"
    if "dcp" in t: return "DCP"
    if re.search(r"\b4k\b", t): return "4K"
    if "digital" in t: return "Digital"
    return ""


def _fmt_time(hhmm):
    """'16:00' (Europe/London local) -> '4:00 PM'."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except Exception:
        return hhmm
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


def scrape_electric(session=None, today=None):
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", UA)
    today = today or date.today()

    data = s.get(DATA_URL, timeout=40).json()
    films = data["films"]
    screenings = data["screenings"]

    # Only future/current dates for Portobello.
    port = [sc for sc in screenings.values()
            if str(sc.get("cinema")) == PORTOBELLO_ID
            and sc.get("d") and sc["d"] >= today.isoformat()]

    # Enrich runtime/synopsis/director once per distinct film.
    detail = {}
    for fid in sorted({sc["film"] for sc in port}):
        link = (films.get(fid) or {}).get("link")
        if not link:
            continue
        try:
            d = s.get(DETAIL_URL.format(path=link), timeout=30).json()
            detail[fid] = (d.get("post") or {}).get("content") or {}
        except Exception:
            detail[fid] = {}

    rows = []
    for sc in port:
        fid = sc["film"]
        film = films.get(fid, {})
        det = detail.get(fid, {})

        title = det.get("title") or film.get("title") or ""
        director = det.get("director") or film.get("director") or ""
        description = (det.get("short_synopsis") or det.get("synopsis")
                       or film.get("short_synopsis") or "")

        dur = det.get("duration")
        runtime = f"{dur} min" if dur and str(dur).strip() not in ("", "0") else ""

        # No explicit format in the feed; derive from any title hint (usually "").
        fmt = _norm_fmt(title)

        # Per-event URL: live booking link when open, else the film detail page.
        if sc.get("bookable") and sc.get("link"):
            url = BASE + sc["link"]
        elif film.get("link"):
            url = BASE + film["link"]
        else:
            url = BASE + "/programme/list/portobello/"

        rows.append({
            "title": title,
            "date": sc["d"],
            "time": _fmt_time(sc["t"]),
            "format": fmt,
            "venue": "Electric Cinema Portobello",
            "venue_short": "Electric",
            "url": url,
            "city": "London",
            "description": description,
            "runtime": runtime,
            "director": director,
        })

    rows.sort(key=lambda r: (r["date"], r["time"], r["title"]))
    return rows


if __name__ == "__main__":
    out = scrape_electric()
    print(f"TOTAL ROWS: {len(out)}")
    if out:
        print("DATE RANGE:", out[0]["date"], "->", out[-1]["date"])
    print("\nSAMPLE (first 8):")
    for r in out[:8]:
        print(json.dumps(r, ensure_ascii=False))
    # coverage
    n = len(out) or 1
    for k in ("runtime", "director", "description", "format"):
        c = sum(1 for r in out if r[k])
        print(f"coverage {k}: {c}/{len(out)} = {100*c//n}%")
