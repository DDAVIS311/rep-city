"""Rio Cinema (Dalston, London) scraper.

Platform: Savoy Systems (the site is served by the `Rio.dll` engine at
riocinema.org.uk; booking/images come from savoysystems.co.uk). NOT Spektrix.

Savoy embeds the *entire* current programme as a single JSON blob in a
`<script>var Events = {...}</script>` tag on every page (Home and every
WhatsOn?f= page carry the identical feed). One HTTP GET gets everything:
title, synopsis, running time, director, year, country, per-performance
date/time and a per-showtime booking deep-link. Dates/times are already
Europe/London local wall-clock (Savoy stores local), so no UTC conversion
is needed. requests + stdlib only; no bs4/Playwright required.
"""
import re
import json
import html
import requests

BASE = "https://riocinema.org.uk"
PROGRAMME_URL = BASE + "/Rio.dll/Home"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

_FMT_PATTERNS = [
    (re.compile(r"\b70\s*mm\b", re.I), "70mm"),
    (re.compile(r"\b35\s*mm\b", re.I), "35mm"),
    (re.compile(r"\b16\s*mm\b", re.I), "16mm"),
    (re.compile(r"\b4k\s*dcp\b", re.I), "4K DCP"),
    (re.compile(r"\bdcp\b", re.I), "DCP"),
]


def _extract_events_json(html):
    """Pull the `var Events = {...}` object out of the page via balanced braces."""
    m = re.search(r'var\s+Events\s*=\s*(\{"Events")', html)
    if not m:
        return []
    start = m.start(1)
    depth = 0
    instr = False
    esc = False
    end = None
    for i in range(start, len(html)):
        c = html[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            instr = not instr
            continue
        if instr:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return []
    return json.loads(html[start:end]).get("Events", [])


def _fmt_time(hhmm):
    """'2045' or '1930' -> '8:45 PM' / '7:30 PM' (12-hour, no leading zero)."""
    hhmm = (hhmm or "").zfill(4)
    try:
        h, m = int(hhmm[:2]), int(hhmm[2:])
    except ValueError:
        return ""
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


def _norm_format(*texts):
    hay = " ".join(t for t in texts if t)
    for pat, label in _FMT_PATTERNS:
        if pat.search(hay):
            return label
    return ""


def scrape_rio():
    """Return list of dicts, one row per showtime, for Rio Cinema Dalston."""
    resp = requests.get(PROGRAMME_URL, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    events = _extract_events_json(resp.text)

    rows = []
    for ev in events:
        title = html.unescape((ev.get("Title") or "").strip())
        synopsis = html.unescape((ev.get("Synopsis") or "").strip())
        director = html.unescape((ev.get("Director") or "").strip())
        rt = ev.get("RunningTime")
        runtime = f"{rt} min" if rt else ""
        # Savoy Tags carry a Format field but it is empty in practice at Rio;
        # infer from title/synopsis (35mm/70mm/DCP screenings are named there).
        tag_fmt = ""
        for t in ev.get("Tags", []):
            if t.get("Format"):
                tag_fmt = t["Format"].strip()
                break
        fmt = tag_fmt or _norm_format(title, synopsis)
        event_url = ev.get("URL") or ""

        for p in ev.get("Performances", []):
            perf_url = p.get("URL") or ""
            if perf_url.startswith("Booking"):
                url = f"{BASE}/Rio.dll/{perf_url}"
            elif perf_url:
                url = perf_url
            else:
                url = event_url
            rows.append({
                "title": title,
                "date": p.get("StartDate", ""),        # already YYYY-MM-DD, Europe/London
                "time": _fmt_time(p.get("StartTime")),  # Europe/London local
                "format": fmt,
                "venue": "Rio Cinema",
                "venue_short": "Rio",
                "url": url,
                "city": "London",
                "description": synopsis,
                "runtime": runtime,
                "director": director,
            })
    return rows


if __name__ == "__main__":
    data = scrape_rio()
    dates = [r["date"] for r in data if r["date"]]
    print(f"TOTAL ROWS: {len(data)}")
    print(f"DATE RANGE: {min(dates)} -> {max(dates)}")
    print(f"KEYS: {sorted(data[0].keys())}")
    print("-" * 70)
    for r in data[:8]:
        print(f"{r['date']} {r['time']:>9} | {r['title'][:44]:44} | fmt={r['format'] or '-':7} | {r['runtime'] or '-'}")
        print(f"         dir={r['director'] or '-'} | {r['url'][:70]}")
