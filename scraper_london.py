"""
London rep-cinema scrapers.
Same output schema as scraper.py / scraper_nyc.py:
    {title, date, time, format, venue, venue_short, url, city="London",
     description, runtime, director}

Launch venues use the Spektrix adapter (adapters.scrape_spektrix): ICA and the
Barbican. Both expose Spektrix's public JSON API — reliable showtimes, titles and
in-title format; director/synopsis are thin via the API (they live on the venue
websites) and runtime is client-dependent. Additional London venues (Savoy /
Admit One HTML platforms) can be added here later to reach fuller coverage.
"""

from adapters import scrape_spektrix

# (spektrix_client, venue name, venue_short)
SPEKTRIX_VENUES = [
    ("ica", "ICA", "ICA"),
    ("barbicancentre", "Barbican", "Barbican"),
]


def scrape_all_london():
    screenings = []
    for client, venue, short in SPEKTRIX_VENUES:
        try:
            rows = scrape_spektrix(client, venue, short)
            print(f"  {venue}: {len(rows)} screenings")
            screenings += rows
        except Exception as e:
            print(f"  {venue}: FAILED ({e})")
    return screenings


if __name__ == "__main__":
    rows = scrape_all_london()
    print(f"\nLondon total: {len(rows)}")
