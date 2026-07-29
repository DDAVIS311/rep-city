"""
Peckhamplex (London / Peckham) scraper.

Output schema (one row PER SHOWTIME), matching scraper_london.py:
    {title, date "YYYY-MM-DD", time "7:00 PM", format, venue "Peckhamplex",
     venue_short "Peckhamplex", url, city "London", description, runtime, director}

Data source
-----------
Peckhamplex runs on the Veezi ticketing platform. Its own front-end powers the
"Choose Film by Date / by Title" widget with a public JSON API discovered in
/js/s/search.min.js:
    GET https://www.peckhamplex.london/api/v1/film/by/dates   (primary)
    GET https://www.peckhamplex.london/api/v1/film/by/title
Plain GET, no auth/token/headers required. `by/dates` returns:
    { "<human date>": { "<film_id>": [ {time, title, url, autism, hoh, wwb}, ... ] } }
where `url` is the working per-showtime Veezi booking link and `time` is the
Europe/London wall-clock time. `by/title` carries the same showtimes plus an ISO
UTC `date` field, used only to cross-check the timezone.

Metadata (director / synopsis / runtime) is NOT in the feed. It is enriched from
the venue's own /film/<slug> detail pages, mapped from the /films/out-now and
/films/coming-soon listing pages (img alt == exact API title). Detail pages are
cached by URL so daily runs only fetch films they haven't seen.
"""

import os
import re
import json
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

LONDON = ZoneInfo("Europe/London")
BASE = "https://www.peckhamplex.london"
DATES_API = BASE + "/api/v1/film/by/dates"
LISTING_URLS = [BASE + "/films/out-now", BASE + "/films/coming-soon"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html"}
_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_HERE, "peckhamplex_meta_cache.json")

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _parse_date(human):
    """'Wednesday 29th July 2026' -> '2026-07-29' (Europe/London local date)."""
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", human or "")
    if not m:
        return None
    day, month, year = int(m.group(1)), _MONTHS.get(m.group(2).lower()), int(m.group(3))
    if not month:
        return None
    try:
        return datetime(year, month, day, tzinfo=LONDON).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _fmt_time(hhmm):
    """'20:45' -> '8:45 PM' (already Europe/London wall-clock)."""
    m = re.match(r"(\d{1,2}):(\d{2})", (hhmm or "").strip())
    if not m:
        return (hhmm or "").strip()
    h, mn = int(m.group(1)), int(m.group(2))
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mn:02d} {ampm}"


def _norm_fmt(text):
    t = (text or "").lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "4k dcp" in t: return "4K DCP"
    if "dcp" in t:    return "DCP"
    if re.search(r"\b4k\b", t): return "4K"
    return ""  # Peckhamplex is digital new releases -> normally ""


def _runtime(text):
    if not text:
        return ""
    total = 0
    h = re.search(r"(\d+)\s*hour", text, re.I)
    mn = re.search(r"(\d+)\s*min", text, re.I)
    if h:
        total += int(h.group(1)) * 60
    if mn:
        total += int(mn.group(1))
    return f"{total} min" if total else ""


# ── front-end enrichment ─────────────────────────────────────────────────────
def _norm_title(t):
    t = re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()
    return re.sub(r"\s+", " ", t)


def _listing_map(session):
    """Map normalized film title -> detail URL from the listing pages."""
    out = {}
    for url in LISTING_URLS:
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select('a[href*="/film/"]'):
            href = a.get("href", "")
            slug = href.rstrip("/").split("/film/")[-1]
            if not slug or "/" in slug:
                continue
            img = a.find("img")
            title = img.get("alt", "").strip() if img else ""
            if not title:
                continue
            detail = href if href.startswith("http") else BASE + "/film/" + slug
            out.setdefault(_norm_title(title), detail)
    return out


def _detail(session, url):
    meta = {"director": "", "description": "", "runtime": "", "format": ""}
    try:
        r = session.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except Exception:
        return meta
    soup = BeautifulSoup(r.text, "html.parser")
    dirs = [s.get_text(" ", strip=True)
            for s in soup.select('[itemprop=director] [itemprop=name]')]
    meta["director"] = ", ".join(d for d in dirs if d)
    p = soup.select_one("p[itemprop=description]")
    if p:
        meta["description"] = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
    body = soup.get_text(" ", strip=True)
    rt = re.search(r"Running Time:\s*([0-9]+\s*hours?(?:\s*[0-9]+\s*minutes?)?|"
                   r"[0-9]+\s*minutes?)", body, re.I)
    meta["runtime"] = _runtime(rt.group(1)) if rt else ""
    title_txt = soup.title.get_text() if soup.title else ""
    meta["format"] = _norm_fmt(title_txt + " " + body[:400])
    return meta


# ── main ─────────────────────────────────────────────────────────────────────
def scrape_peckhamplex():
    session = requests.Session()
    r = session.get(DATES_API, headers=HEADERS, timeout=45)
    r.raise_for_status()
    feed = r.json()

    # title -> detail URL, plus per-URL metadata cache
    try:
        title_map = _listing_map(session)
    except Exception:
        title_map = {}
    cache = _load_json(CACHE_FILE)
    changed = False

    rows = []
    for human_date, films in (feed or {}).items():
        iso = _parse_date(human_date)
        if not iso or not isinstance(films, dict):
            continue
        for _film_id, showings in films.items():
            for s in showings or []:
                title = (s.get("title") or "").strip()
                if not title or not s.get("time"):
                    continue
                detail_url = title_map.get(_norm_title(title))
                meta = {}
                if detail_url:
                    if detail_url not in cache:
                        cache[detail_url] = _detail(session, detail_url)
                        changed = True
                        _time.sleep(0.25)
                    meta = cache.get(detail_url) or {}
                rows.append({
                    "title": title,
                    "date": iso,
                    "time": _fmt_time(s.get("time")),
                    "format": meta.get("format", ""),
                    "venue": "Peckhamplex",
                    "venue_short": "Peckhamplex",
                    # working per-showtime Veezi booking URL from the feed;
                    # fall back to the venue detail page if ever absent
                    "url": (s.get("url") or detail_url or BASE + "/films/out-now"),
                    "city": "London",
                    "description": meta.get("description", ""),
                    "runtime": meta.get("runtime", ""),
                    "director": meta.get("director", ""),
                })

    if changed:
        _save_json(CACHE_FILE, cache)
    rows.sort(key=lambda r: (r["date"], r["time"], r["title"]))
    return rows


if __name__ == "__main__":
    data = scrape_peckhamplex()
    print(f"total showtimes: {len(data)}")
    if data:
        print(f"date range: {data[0]['date']} .. {data[-1]['date']}")
    for row in data[:8]:
        print(json.dumps(row, ensure_ascii=False))
