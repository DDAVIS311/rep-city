"""
NYC rep theatre scrapers.
Same output schema as scraper.py: {title, date, time, format, venue, venue_short, url, city}
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup, Comment
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _normalize_format(text):
    t = text.lower()
    if "70mm" in t: return "70mm"
    if "35mm" in t: return "35mm"
    if "16mm" in t: return "16mm"
    if "nitrate" in t: return "Nitrate"
    if "4k dcp" in t or "4k restoration" in t: return "4K DCP"
    if "2k dcp" in t: return "2K DCP"
    if "dcp" in t: return "DCP"
    if "digital" in t: return "Digital"
    if "vhs" in t: return "VHS"
    if "dvd" in t: return "DVD"
    return ""


def _make(title, date, time_str, fmt, venue, venue_short, url):
    return {
        "title": title, "date": date, "time": time_str, "format": fmt,
        "venue": venue, "venue_short": venue_short, "url": url, "city": "NYC",
    }


# ─────────────────────────────────────────────────────────────
# IFC Center
# ─────────────────────────────────────────────────────────────
def scrape_ifc():
    screenings = []
    try:
        r = requests.get("https://www.ifccenter.com/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

        for day_key in DAY_ORDER:
            day_div = soup.select_one(f"div.daily-schedule.{day_key}")
            if not day_div:
                continue

            # Header like "Mon Jun 29"
            header = day_div.select_one("h3")
            if not header:
                continue
            date_str = _parse_ifc_date(header.text.strip())
            if not date_str:
                continue

            for film_li in day_div.select("li"):
                title_el = film_li.select_one("div.details h3 a")
                if not title_el:
                    continue
                title = title_el.text.strip()
                film_url = title_el.get("href", "https://www.ifccenter.com/")

                for time_li in film_li.select("ul.times li a"):
                    time_str = time_li.text.strip()
                    ticket_url = time_li.get("href", film_url)
                    if title and time_str:
                        screenings.append(_make(title, date_str, time_str, "", "IFC Center", "IFC", ticket_url))

    except Exception as e:
        print(f"[IFC] Error: {e}")
    return screenings


def _parse_ifc_date(text):
    """Parse 'Mon Jun 29' or 'Fri Jul 4' into YYYY-MM-DD."""
    m = re.search(r"(\w+)\s+(\d{1,2})", text)
    if not m:
        return None
    month_str = m.group(1).lower()[:3]
    day = int(m.group(2))
    month = MONTH_MAP.get(month_str, 0)
    if not month:
        return None
    year = datetime.now().year
    # Handle year rollover
    try:
        dt = datetime(year, month, day)
        if dt < datetime.now() - timedelta(days=1):
            dt = datetime(year + 1, month, day)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
# Film Forum
# ─────────────────────────────────────────────────────────────
def scrape_film_forum():
    screenings = []
    try:
        r = requests.get("https://filmforum.org/films", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Build format lookup from image alt text on the page
        format_by_title = {}
        for img in soup.find_all("img", alt=True):
            alt = img["alt"]
            fmt = _normalize_format(alt)
            if fmt:
                # Try to find the nearest film title
                parent = img.parent
                for _ in range(5):
                    if not parent:
                        break
                    link = parent.find("a", href=re.compile(r"/film/"))
                    if link:
                        format_by_title[link.text.strip().upper()] = fmt
                        break
                    parent = parent.parent

        # Tab → day offset from Monday (tabs-0=Mon, tabs-1=Tue, ... tabs-6=Sun)
        TAB_DAY_OFFSET = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}  # Mon=0...Sun=6
        # Find Monday date from the week shown (look for comment <!-- N --> in tabs-0)
        monday_date = None
        tabs_container = soup.select_one(".showtimes-container")
        if tabs_container:
            tab0 = tabs_container.select_one("#tabs-0")
            if tab0:
                # HTML comment contains the day number
                for item in tab0.contents:
                    if isinstance(item, Comment):
                        try:
                            day_num = int(str(item).strip())
                            monday_date = _resolve_film_forum_date(soup, day_num, 0)
                            break
                        except ValueError:
                            pass

        if not monday_date:
            # Fallback: find next Monday from today
            today = datetime.now()
            days_until_monday = (7 - today.weekday()) % 7
            if days_until_monday == 0 and today.weekday() != 0:
                days_until_monday = 7
            monday_date = (today - timedelta(days=today.weekday())).date()

        if tabs_container:
            for tab_idx in range(7):
                tab = tabs_container.select_one(f"#tabs-{tab_idx}")
                if not tab:
                    continue

                # Calculate the actual date for this tab
                # tabs-0=Mon, offset 0; tabs-6=Sun, offset 6
                tab_date = datetime.combine(monday_date, datetime.min.time()) + timedelta(days=tab_idx)
                date_str = tab_date.strftime("%Y-%m-%d")

                # Each film is a <p> with <strong><a>Title</a></strong><br/><span>HH:MM</span>...
                for p in tab.select("p"):
                    title_el = p.select_one("strong a")
                    if not title_el:
                        continue
                    title = title_el.get_text(" ", strip=True)
                    film_url = title_el.get("href", "https://filmforum.org/films")
                    if not film_url.startswith("http"):
                        film_url = "https://filmforum.org" + film_url

                    fmt = format_by_title.get(title.upper(), _normalize_format(p.get_text()))

                    for span in p.select("span"):
                        raw_time = span.text.strip()  # "12:20" or "7:00"
                        time_str = _ff_time_to_ampm(raw_time)
                        if time_str:
                            screenings.append(_make(title, date_str, time_str, fmt, "Film Forum", "FilmForum", film_url))

    except Exception as e:
        print(f"[FilmForum] Error: {e}")
    return screenings


def _resolve_film_forum_date(soup, day_num, tab_idx):
    """Given the day number from a tab's HTML comment, find the full date."""
    # Look for week range like "Friday, June 26 – Thursday, July 2"
    text = soup.get_text(" ", strip=True)
    # Find something like "June 26" near this day_num
    year = datetime.now().year
    # Try to find the month by looking at surrounding context
    # tabs-0 = Mon, so find a Monday with day_num in the current or next week
    today = datetime.now()
    # Start from last Monday
    last_monday = today - timedelta(days=today.weekday())
    for week_offset in range(-1, 3):
        candidate = last_monday + timedelta(weeks=week_offset, days=tab_idx)
        if candidate.day == day_num:
            return candidate.date()
    return last_monday.date()


def _ff_time_to_ampm(raw):
    """Convert Film Forum time "12:20" or "7:00" to "12:20 PM" / "7:00 PM"."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if not m:
        return ""
    hour, minute = int(m.group(1)), m.group(2)
    # Film Forum convention: 10 and 11 = AM; everything else = PM
    if hour in (10, 11):
        ampm = "AM"
    else:
        ampm = "PM"
    return f"{hour}:{minute} {ampm}"


# ─────────────────────────────────────────────────────────────
# Anthology Film Archives
# ─────────────────────────────────────────────────────────────
def scrape_anthology():
    screenings = []
    try:
        today = datetime.now()
        months_to_scrape = [(today.year, today.month)]
        # Add next 2 months
        for i in range(1, 3):
            m = today.month + i
            y = today.year + (m - 1) // 12
            m = ((m - 1) % 12) + 1
            months_to_scrape.append((y, m))

        for year, month in months_to_scrape:
            url = f"https://www.anthologyfilmarchives.org/film_screenings/calendar?month={month:02d}&year={year}"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if not r.ok:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            for day_td in soup.select("td.calendar_day[name]"):
                day_num = int(day_td["name"])
                try:
                    date_obj = datetime(year, month, day_num)
                except ValueError:
                    continue
                if date_obj.date() < today.date():
                    continue
                date_str = date_obj.strftime("%Y-%m-%d")

                for ev in day_td.select("li.calendar_event"):
                    ev_text = ev.get_text(" ", strip=True)
                    # Format: "7:00 PM TITLE OF FILM"
                    time_match = re.match(r"(\d{1,2}:\d{2}\s*[APap][Mm])\s+(.+)", ev_text)
                    if not time_match:
                        continue
                    time_str = time_match.group(1).strip()
                    title = time_match.group(2).strip()
                    # Clean up title (remove trailing punctuation)
                    title = re.sub(r"\s+", " ", title).strip()

                    link_el = ev.select_one("a[href]")
                    ev_url = "https://www.anthologyfilmarchives.org" + link_el["href"] \
                        if link_el and link_el["href"].startswith("/") \
                        else "https://www.anthologyfilmarchives.org/film_screenings"

                    screenings.append(_make(title, date_str, time_str, "", "Anthology Film Archives", "Anthology", ev_url))

            time.sleep(0.5)

    except Exception as e:
        print(f"[Anthology] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Low Cinema
# ─────────────────────────────────────────────────────────────
def scrape_low_cinema():
    screenings = []
    try:
        r = requests.get("https://lowcinema.com/", headers=HEADERS, timeout=15)
        m = re.search(r"window\.showingsData\s*=\s*(\{.*?\});", r.text, re.DOTALL)
        if not m:
            return screenings

        data = json.loads(m.group(1))
        today_str = datetime.now().strftime("%Y-%m-%d")

        for date_str, shows in data.items():
            if date_str < today_str:
                continue
            for show in shows:
                title = show.get("movie", "").strip()
                time_str = show.get("time", "").strip()
                ticket_url = show.get("url", "")
                if ticket_url and not ticket_url.startswith("http"):
                    ticket_url = "https://lowcinema.com" + ticket_url
                movie_url = show.get("movie_url", "")
                if movie_url and not movie_url.startswith("http"):
                    movie_url = "https://lowcinema.com" + movie_url
                url = ticket_url or movie_url or "https://lowcinema.com/"

                if title:
                    screenings.append(_make(title, date_str, time_str, "", "Low Cinema", "LowCinema", url))

    except Exception as e:
        print(f"[LowCinema] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Metrograph — homepage calendar (all dates/times in static HTML)
# ─────────────────────────────────────────────────────────────
def scrape_metrograph():
    screenings = []
    try:
        r = requests.get("https://metrograph.com/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        today = datetime.now().date()

        for day_div in soup.select("div.calendar-list-day[id]"):
            day_id = day_div.get("id", "")
            # id="calendar-list-day-2026-07-01"
            date_m = re.search(r"(\d{4}-\d{2}-\d{2})$", day_id)
            if not date_m:
                continue
            date_str = date_m.group(1)
            if date_str < str(today):
                continue

            for item in day_div.select("div.item.film-thumbnail"):
                title_a = item.select_one("h4 a.title")
                if not title_a:
                    continue
                title = title_a.get_text(" ", strip=True)
                film_href = title_a.get("href", "")
                if film_href and not film_href.startswith("http"):
                    film_href = "https://metrograph.com" + film_href

                # format sometimes listed in a subtitle/h5 below the title
                fmt = ""
                for sub in item.select("h5, .subtitle, .format, .note"):
                    f = _normalize_format(sub.get_text())
                    if f:
                        fmt = f
                        break

                for time_a in item.select("div.showtimes a"):
                    raw_time = time_a.get_text(strip=True)  # e.g. "3:15pm"
                    if not raw_time:
                        continue
                    time_str = _normalize_12h(raw_time)
                    ticket_url = time_a.get("href") or film_href or "https://metrograph.com/"
                    if ticket_url.startswith("/"):
                        ticket_url = "https://metrograph.com" + ticket_url
                    screenings.append(_make(title, date_str, time_str, fmt, "Metrograph", "Metrograph", ticket_url))

    except Exception as e:
        print(f"[Metrograph] Error: {e}")
    return screenings


def _normalize_12h(raw):
    """Convert '3:15pm' / '10:00am' to '3:15 PM' / '10:00 AM'."""
    m = re.match(r"(\d{1,2}:\d{2})\s*([aApP][mM])", raw.strip())
    if m:
        return f"{m.group(1)} {m.group(2).upper()}"
    return raw.strip()


# ─────────────────────────────────────────────────────────────
# Film at Lincoln Center — Playwright + API interception
# ─────────────────────────────────────────────────────────────
def scrape_film_linc():
    screenings = []
    try:
        api_data = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()

            def handle_response(resp):
                if "filmlinc" in resp.url and "json" in resp.headers.get("content-type", ""):
                    try:
                        api_data.append({"url": resp.url, "body": resp.json()})
                    except Exception:
                        pass

            page.on("response", handle_response)
            page.goto("https://www.filmlinc.org/calendar/", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        # Try to extract from intercepted API calls first
        for call in api_data:
            extracted = _parse_filmlinc_api(call["body"])
            screenings.extend(extracted)

        if not screenings:
            # Fall back to parsing rendered HTML
            soup = BeautifulSoup(html, "html.parser")
            screenings = _parse_filmlinc_html(soup)

    except Exception as e:
        print(f"[FilmLinc] Error: {e}")
    return screenings


def _parse_filmlinc_api(data):
    screenings = []
    try:
        events = []
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            for k in ("events", "data", "results", "items"):
                if isinstance(data.get(k), list):
                    events = data[k]
                    break

        today = datetime.now().date()
        for ev in events:
            title = ev.get("title") or ev.get("name") or ""
            if not title:
                continue
            date_str = ""
            for key in ("start_date", "start", "date", "startDate"):
                if ev.get(key):
                    raw = str(ev[key])[:10]
                    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
                        date_str = raw
                        break
            if not date_str:
                continue
            if date_str < str(today):
                continue

            time_str = ev.get("start_time") or ev.get("time") or ""
            fmt = _normalize_format(str(ev))
            url = ev.get("url") or ev.get("link") or "https://www.filmlinc.org/calendar/"

            screenings.append(_make(title, date_str, time_str, fmt, "Film at Lincoln Center", "FilmLinc", url))
    except Exception:
        pass
    return screenings


def _parse_filmlinc_html(soup):
    screenings = []
    today = datetime.now().date()
    year = datetime.now().year

    # Try various event card selectors
    for sel in ["article", "[class*='event']", "[class*='Event']", ".program-item", "li.event"]:
        cards = soup.select(sel)
        if len(cards) > 3:
            for card in cards:
                try:
                    text = card.get_text(" ", strip=True)
                    if len(text) < 15:
                        continue
                    title_el = card.select_one("h1,h2,h3,h4,[class*='title'],[class*='Title']")
                    if not title_el:
                        continue
                    title = title_el.text.strip()
                    if len(title) < 3:
                        continue

                    date_m = re.search(
                        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?",
                        text, re.I
                    )
                    if not date_m:
                        continue
                    month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                    day = int(date_m.group(2))
                    yr = int(date_m.group(3)) if date_m.group(3) else year
                    dt = datetime(yr, month, day)
                    if dt.date() < today:
                        continue

                    time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                    time_str = time_m.group(0).strip() if time_m else ""
                    fmt = _normalize_format(text)
                    link = card.select_one("a[href]")
                    url = link["href"] if link else "https://www.filmlinc.org/calendar/"
                    if url.startswith("/"):
                        url = "https://www.filmlinc.org" + url

                    screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt,
                                            "Film at Lincoln Center", "FilmLinc", url))
                except Exception:
                    continue
            break

    return screenings


# ─────────────────────────────────────────────────────────────
# MoMA — try their API, fall back to Playwright
# ─────────────────────────────────────────────────────────────
def scrape_moma():
    screenings = []
    try:
        # MoMA has an events API
        today = datetime.now()
        from_str = today.strftime("%Y-%m-%d")
        to_str = (today + timedelta(days=90)).strftime("%Y-%m-%d")

        for api_url in [
            f"https://www.moma.org/api/calendar/events?type=film&from={from_str}&to={to_str}&per_page=200",
            f"https://www.moma.org/calendar/events.json?type=film&start_date={from_str}&end_date={to_str}",
            "https://www.moma.org/api/v1/events?category=film&limit=200",
        ]:
            r = requests.get(api_url, headers=HEADERS, timeout=12)
            if r.ok and "json" in r.headers.get("content-type", ""):
                data = r.json()
                if data:
                    events = data if isinstance(data, list) else data.get("events") or data.get("data") or []
                    screenings = _parse_moma_events(events)
                    if screenings:
                        break

        if not screenings:
            # Try Playwright
            screenings = _scrape_moma_playwright()

    except Exception as e:
        print(f"[MoMA] Error: {e}")
    return screenings


def _parse_moma_events(events):
    screenings = []
    today = datetime.now().date()
    for ev in events:
        try:
            title = ev.get("title") or ev.get("name") or ""
            if not title:
                continue
            date_str = ""
            for key in ("start_date", "date", "start", "startDate", "event_date"):
                v = ev.get(key, "")
                if v and re.match(r"\d{4}-\d{2}-\d{2}", str(v)[:10]):
                    date_str = str(v)[:10]
                    break
            if not date_str or date_str < str(today):
                continue
            time_str = ev.get("start_time") or ev.get("time") or ""
            fmt = _normalize_format(str(ev))
            url = ev.get("url") or ev.get("link") or "https://www.moma.org/calendar/?filters[0][value]=Film"
            screenings.append(_make(title, date_str, time_str, fmt, "MoMA", "MoMA", url))
        except Exception:
            continue
    return screenings


def _scrape_moma_playwright():
    screenings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(
                "https://www.moma.org/calendar/?filters[0][value]=Film",
                wait_until="networkidle", timeout=30000
            )
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        today = datetime.now().date()
        year = today.year

        for card in soup.select("article, [class*='event'], [class*='Event'], li.grid-item"):
            try:
                text = card.get_text(" ", strip=True)
                title_el = card.select_one("h2,h3,h4,[class*='title'],[class*='Title']")
                if not title_el:
                    continue
                title = title_el.text.strip()
                if len(title) < 3:
                    continue
                date_m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?", text, re.I)
                if not date_m:
                    continue
                month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                day = int(date_m.group(2))
                yr = int(date_m.group(3)) if date_m.group(3) else year
                dt = datetime(yr, month, day)
                if dt.date() < today:
                    continue
                time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                time_str = time_m.group(0).strip() if time_m else ""
                fmt = _normalize_format(text)
                link = card.select_one("a[href]")
                url = link["href"] if link else "https://www.moma.org/calendar/?filters[0][value]=Film"
                if url.startswith("/"):
                    url = "https://www.moma.org" + url
                screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt, "MoMA", "MoMA", url))
            except Exception:
                continue
    except Exception as e:
        print(f"[MoMA/Playwright] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Museum of the Moving Image — Playwright
# ─────────────────────────────────────────────────────────────
def scrape_momi():
    screenings = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto("https://movingimage.us/program/", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        today = datetime.now().date()
        year = today.year

        for card in soup.select("article, [class*='event'], [class*='program'], [class*='film']"):
            try:
                text = card.get_text(" ", strip=True)
                title_el = card.select_one("h1,h2,h3,h4,[class*='title']")
                if not title_el:
                    continue
                title = title_el.text.strip()
                if len(title) < 3:
                    continue
                date_m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?", text, re.I)
                if not date_m:
                    continue
                month = MONTH_MAP.get(date_m.group(1).lower()[:3], 0)
                day = int(date_m.group(2))
                yr = int(date_m.group(3)) if date_m.group(3) else year
                dt = datetime(yr, month, day)
                if dt.date() < today:
                    continue
                time_m = re.search(r"\d{1,2}:\d{2}\s*[APap][Mm]", text)
                time_str = time_m.group(0).strip() if time_m else ""
                fmt = _normalize_format(text)
                link = card.select_one("a[href]")
                url = link["href"] if link else "https://movingimage.us/program/"
                if url.startswith("/"):
                    url = "https://movingimage.us" + url
                screenings.append(_make(title, dt.strftime("%Y-%m-%d"), time_str, fmt,
                                        "Museum of Moving Image", "MoMI", url))
            except Exception:
                continue
    except Exception as e:
        print(f"[MoMI] Error: {e}")
    return screenings


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────
def scrape_all_nyc():
    results = {}

    print("  Scraping IFC Center...")
    results["IFC"] = scrape_ifc()
    print(f"    → {len(results['IFC'])} screenings")

    print("  Scraping Film Forum...")
    results["FilmForum"] = scrape_film_forum()
    print(f"    → {len(results['FilmForum'])} screenings")

    print("  Scraping Anthology Film Archives...")
    results["Anthology"] = scrape_anthology()
    print(f"    → {len(results['Anthology'])} screenings")

    print("  Scraping Low Cinema...")
    results["LowCinema"] = scrape_low_cinema()
    print(f"    → {len(results['LowCinema'])} screenings")

    print("  Scraping Metrograph (Playwright)...")
    results["Metrograph"] = scrape_metrograph()
    print(f"    → {len(results['Metrograph'])} screenings")

    print("  Scraping Film at Lincoln Center (Playwright)...")
    results["FilmLinc"] = scrape_film_linc()
    print(f"    → {len(results['FilmLinc'])} screenings")

    print("  Scraping MoMA...")
    results["MoMA"] = scrape_moma()
    print(f"    → {len(results['MoMA'])} screenings")

    print("  Scraping Museum of Moving Image (Playwright)...")
    results["MoMI"] = scrape_momi()
    print(f"    → {len(results['MoMI'])} screenings")

    all_screenings = []
    for v in results.values():
        all_screenings.extend(v)

    return all_screenings


if __name__ == "__main__":
    screenings = scrape_all_nyc()
    print(f"\nNYC total: {len(screenings)}")
    from collections import Counter
    vc = Counter(s["venue_short"] for s in screenings)
    for v, c in sorted(vc.items()):
        print(f"  {v}: {c}")
