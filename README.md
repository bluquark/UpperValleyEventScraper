# Upper Valley Events Scraper

Scrapes the next 30 days of events from multiple Upper Valley sources and produces
a local self-contained HTML page: `output/events_YYYY-MM-DD_to_YYYY-MM-DD.html`.

**Sources:**
- [home.dartmouth.edu/events](https://home.dartmouth.edu/events)
- [nhhumanities.org/programs/upcoming](https://www.nhhumanities.org/programs/upcoming)

![Screenshot](example_screenshot.png)

## Features

- Events color-coded and filterable by source
- Dartmouth: academic/non-public events collapsed by default; recurring events merged
- Per-event "Add to Google Calendar" button(s)
- Dark/light theme toggle

## Requirements

```
pip install requests beautifulsoup4
```

Python 3.9+ required (uses `zoneinfo`).

## Usage

The scraper has two independent stages: **scrape** (network fetch → JSON) and **generate** (JSON → HTML).
At least one flag is required; omitting both prints help and exits.

```
python scraper.py --scrape=SOURCES --generate=SOURCES
```

`SOURCES` is a comma-separated list of source names, or `all`.
Valid source names: `dartmouth`, `nhhumanities`

### Examples

Run everything end-to-end:
```
python scraper.py --scrape=all --generate=all
```

Scrape only (saves to `output/scraped_<source>.json`):
```
python scraper.py --scrape=dartmouth,nhhumanities
```

Generate HTML from previously scraped data (no network calls):
```
python scraper.py --generate=all
```

Scrape and generate a single source:
```
python scraper.py --scrape=nhhumanities --generate=nhhumanities
```

Scrape both but only generate Dartmouth:
```
python scraper.py --scrape=all --generate=dartmouth
```

Open `output/events_*.html` in a browser.

## Intermediate files

Scraped data is stored as JSON in `output/` before HTML generation:

| Source | File |
|---|---|
| Dartmouth | `output/scraped_dartmouth.json` |
| NH Humanities | `output/scraped_nhhumanities.json` |

This lets you re-generate the HTML (e.g. to tweak styling) without re-fetching all event pages.

## Notes

- Dartmouth: scraped via internal AJAX endpoint (`/events/ajax/search`), ~15s with 8 parallel requests
- NH Humanities: all events are embedded in the listing page HTML; detail pages fetched in parallel
