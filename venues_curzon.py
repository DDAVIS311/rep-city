"""Curzon Soho scraper (Vista OCAPI).

Browser-bypass policy: HEADLESS Playwright Chromium ONLY. No curl_cffi / TLS
impersonation (the daily scrape runs headless on GitHub Actions ubuntu-latest).

Two-stage:
  1. Headless Playwright loads https://www.curzon.com/venues/soho/ (behind
     Cloudflare) and reads window.initialData.api = {authToken, apiUrl}. The SPA
     inlines a fresh short-lived bearer token into the HTML on every load.
  2. Plain `requests` (Bearer token) hits the Vista OCAPI data host
     (https://digital-api.curzon.com/ocapi/v1), which returns 200 with a token
     and 401 without one (no Cloudflare challenge on the API host).

Output schema (one row per Soho showtime), matching scraper_london.py:
    {title, date, time, format, venue, venue_short, url, city, description,
     runtime, director}
"""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

LONDON = ZoneInfo("Europe/London")
VENUE_URL = "https://www.curzon.com/venues/soho/"
SITE_ID = "SOH1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _norm_fmt(text):
    """Normalize a format string to one of 35mm/70mm/16mm/4K DCP/DCP/Digital/''."""
    t = (text or "").lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "4k dcp" in t or "4k restoration" in t: return "4K DCP"
    if "dcp" in t: return "DCP"
    if re.search(r"\b4k\b", t): return "4K DCP"
    if "digital" in t: return "Digital"
    return ""


def _get_api_credentials(timeout_s=45):
    """Use headless Playwright to read window.initialData.api from the venue page.

    Returns {"authToken": ..., "apiUrl": ...}. Raises RuntimeError if the token
    never appears (e.g. Cloudflare challenge that does not clear headless)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 800},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = ctx.new_page()
            page.goto(VENUE_URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                api = page.evaluate(
                    "() => (window.initialData && window.initialData.api) "
                    "? window.initialData.api : null")
                if api and api.get("authToken") and api.get("apiUrl"):
                    return {"authToken": api["authToken"], "apiUrl": api["apiUrl"]}
                time.sleep(1.0)
            raise RuntimeError(
                "Curzon: window.initialData.api.authToken not present after "
                f"{timeout_s}s (Cloudflare challenge may not have cleared headless).")
        finally:
            browser.close()


def scrape_curzon_soho():
    """Scrape all upcoming Curzon Soho showtimes. Returns list of row dicts."""
    creds = _get_api_credentials()
    base = creds["apiUrl"].rstrip("/") + "/ocapi/v1"
    sess = requests.Session()
    sess.headers.update({
        "Authorization": "Bearer " + creds["authToken"],
        "User-Agent": UA,
        "Accept": "application/json",
    })

    # Which business dates have Soho screenings?
    r = sess.get(base + "/film-screening-dates",
                 params={"siteIds": SITE_ID}, timeout=30)
    r.raise_for_status()
    dates = [d["businessDate"]
             for d in r.json().get("filmScreeningDates", [])
             if d.get("businessDate")]

    rows = []
    for bdate in dates:
        r = sess.get(base + f"/showtimes/by-business-date/{bdate}",
                     params={"siteIds": SITE_ID}, timeout=30)
        if r.status_code != 200:
            continue
        payload = r.json()
        rd = payload.get("relatedData", {}) or {}

        films = {f["id"]: f for f in rd.get("films", [])}
        attrs = {a["id"]: a for a in rd.get("attributes", [])}
        people = {c["id"]: c for c in rd.get("castAndCrew", [])}

        for st in payload.get("showtimes", []):
            if st.get("siteId") != SITE_ID:      # strict Soho-only filter
                continue
            film = films.get(st.get("filmId"), {})

            title = (film.get("title") or {}).get("text", "").strip()
            if not title:
                continue

            # local date/time from ISO startsAt (has +01:00/+00:00 offset)
            starts = (st.get("schedule") or {}).get("startsAt")
            try:
                dt = datetime.fromisoformat(starts).astimezone(LONDON)
            except Exception:
                continue
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%I:%M %p").lstrip("0")

            # format: from showtime attributes (e.g. "35mm presentations")
            fmt = ""
            for aid in st.get("attributeIds", []) or []:
                a = attrs.get(aid)
                if not a:
                    continue
                cand = _norm_fmt((a.get("shortName") or {}).get("text") or "") \
                    or _norm_fmt((a.get("name") or {}).get("text") or "")
                if cand:
                    fmt = cand
                    break
            if not fmt:      # then title tokens (e.g. "(4K Restoration)", "[35mm]")
                fmt = _norm_fmt(title)
            if not fmt:      # last, any format hint in the synopsis
                fmt = _norm_fmt((film.get("synopsis") or {}).get("text") or "")

            # description
            desc = (film.get("synopsis") or {}).get("text", "") \
                or (film.get("shortSynopsis") or {}).get("text", "")
            desc = (desc or "").strip()

            # runtime
            rt = film.get("runtimeInMinutes")
            runtime = f"{rt} min" if rt else ""

            # director: film.castAndCrew role "Director" -> people lookup
            director = ""
            for m in film.get("castAndCrew", []):
                if "Director" in (m.get("roles") or []):
                    pid = m.get("castAndCrewMemberId")
                    nm = (people.get(pid) or {}).get("name") or {}
                    full = " ".join(x for x in [nm.get("givenName"),
                                                nm.get("middleName"),
                                                nm.get("familyName")] if x).strip()
                    if full:
                        director = full
                        break

            rows.append({
                "title": title,
                "date": date_str,
                "time": time_str,
                "format": fmt,
                "venue": "Curzon Soho",
                "venue_short": "CurzonSoho",
                "url": f"https://www.curzon.com/ticketing/seats/{st.get('id')}/",
                "city": "London",
                "description": desc,
                "runtime": runtime,
                "director": director,
            })

    return rows


if __name__ == "__main__":
    out = scrape_curzon_soho()
    print("total rows:", len(out))
    if out:
        ds = sorted(r["date"] for r in out)
        print("date range:", ds[0], "->", ds[-1])
        import json
        print(json.dumps(out[:8], indent=1, ensure_ascii=False))
        cov = lambda k: sum(1 for r in out if r[k])
        n = len(out)
        for k in ("format", "description", "runtime", "director"):
            print(f"{k}: {cov(k)}/{n}")
