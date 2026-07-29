"""Scraper for The Nickel Cinema, Clerkenwell, London (thenickel.co.uk).

Data source: the homepage server-renders every upcoming screening as an
<a class="block" href="/screening/NNN"> card (no JSON/XHR feed, no
ScreeningEvent ld+json). One GET returns the full programme.
"""
import re
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://thenickel.co.uk"
TZ = ZoneInfo("Europe/London")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

_FMT_MAP = [
    (r"\b70\s*mm\b", "70mm"),
    (r"\b35\s*mm\b", "35mm"),
    (r"\b16\s*mm\b", "16mm"),
    (r"\b4k\s*dcp\b", "4K DCP"),
    (r"\bdcp\b", "DCP"),
    (r"\b4k\b", "4K DCP"),
    (r"\bdigital\b", "Digital"),
]


def _norm_format(text):
    t = text.lower()
    for pat, val in _FMT_MAP:
        if re.search(pat, t):
            return val
    return ""


def _norm_time(raw):
    """'8:20pm' / '6pm' / '8pm' -> '8:20 PM'."""
    m = re.match(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap]m)\s*$", raw, re.I)
    if not m:
        return ""
    hour = int(m.group(1))
    minute = m.group(2) or "00"
    ampm = m.group(3).upper()
    return f"{hour}:{minute} {ampm}"


def _resolve_date(day, month, today):
    """D/M with no year -> nearest sensible YYYY-MM-DD (programme is future-facing)."""
    for yr in (today.year, today.year + 1, today.year - 1):
        try:
            cand = dt.date(yr, month, day)
        except ValueError:
            continue
        if (cand - today).days >= -14:
            return cand.isoformat()
    return dt.date(today.year, month, day).isoformat()


def _parse_meta(italic_text):
    """'(1994, Japan, Banmei Takahashi)' -> (director, year_str)."""
    inner = italic_text.strip().strip("()").strip()
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    director = ""
    if parts:
        last = parts[-1]
        # director is the last part unless it's just a year/number
        if not re.fullmatch(r"\d{4}", last):
            director = last
    return director


def scrape_nickel():
    today = dt.datetime.now(TZ).date()
    resp = requests.get(BASE + "/", headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    anchors = [a for a in soup.find_all("a", class_="block")
               if a.get("href", "").startswith("/screening")]

    for a in anchors:
        href = a["href"]
        url = href if href.startswith("http") else BASE + href

        # Title
        title_el = a.find("p", class_="font-bold")
        title = title_el.get_text(" ", strip=True) if title_el else ""

        # (year, country, director)
        director = ""
        italic = a.find("p", class_="italic")
        if italic:
            director = _parse_meta(italic.get_text(" ", strip=True))

        # Description
        desc_el = a.find("div", class_="whitespace-pre-wrap")
        description = desc_el.get_text("\n", strip=True) if desc_el else ""

        # Runtime (Runtime: 115 mins)
        runtime = ""
        block_text = a.get_text("\n")
        rm = re.search(r"Runtime:\s*\n?\s*(\d+)\s*\n?\s*min", block_text, re.I)
        if rm:
            runtime = f"{rm.group(1)} min"

        # Schedule column (last md:col-span-2 div)
        sched_divs = [d for d in a.find_all("div")
                      if d.get("class") and "md:col-span-2" in d.get("class")]
        if not sched_divs:
            continue
        lines = [l.strip() for l in sched_divs[-1].get_text("\n").split("\n")
                 if l.strip()]

        # date D/M
        date = ""
        for l in lines:
            dm = re.fullmatch(r"(\d{1,2})/(\d{1,2})", l)
            if dm:
                date = _resolve_date(int(dm.group(1)), int(dm.group(2)), today)
                break

        # Film time (line following a 'Film' label)
        time = ""
        for i, l in enumerate(lines):
            if l.lower() == "film" and i + 1 < len(lines):
                time = _norm_time(lines[i + 1])
                break
        if not time:  # fallback: last time-looking token
            times = [_norm_time(l) for l in lines if re.match(r"\d{1,2}([:.]\d{2})?\s*[ap]m$", l, re.I)]
            times = [t for t in times if t]
            if times:
                time = times[-1]

        # Format
        fmt = ""
        for l in lines:
            f = _norm_format(l)
            if f:
                fmt = f
                break

        rows.append({
            "title": title,
            "date": date,
            "time": time,
            "format": fmt,
            "venue": "The Nickel Cinema",
            "venue_short": "Nickel",
            "url": url,
            "city": "London",
            "description": description,
            "runtime": runtime,
            "director": director,
        })

    return rows


if __name__ == "__main__":
    import json
    data = scrape_nickel()
    print(f"TOTAL SHOWTIMES: {len(data)}")
    dates = sorted(r["date"] for r in data if r["date"])
    if dates:
        print(f"DATE RANGE: {dates[0]} -> {dates[-1]}")
    print("\nFirst 6 rows:")
    for r in data[:6]:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    # coverage
    n = len(data) or 1
    for k in ("title", "date", "time", "format", "url", "description", "runtime", "director"):
        c = sum(1 for r in data if r[k])
        print(f"coverage {k:12}: {c}/{len(data)}")
    from collections import Counter
    print("formats:", Counter(r["format"] for r in data))
