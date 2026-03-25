# Upper Valley Events Scraper

This script collects upcoming cultural events scheduled to take place within the Upper Valley
(VT/NH) into one convenient page, with add-to-Google-Calendar buttons.

Results hosted on https://bluquark.github.io/UpperValleyEventScraper/events.html.  Refreshes nightly.

![Screenshot](example_screenshot.png)

## Requirements

```
pip install requests beautifulsoup4
```

Python 3.9+ required.

## Usage

```
python scraper.py [--sources=SOURCES] [--days=N]
```

`--sources` accepts a comma-separated list of sources or groups:
- `all` — all sources
- `theater` — `northernstage`, `shakerbridgetheatre`
- `movies` — `nugget`, `lebanon6`
- individual: `dartmouth`, `nhhumanities`, `northernstage`, `shakerbridgetheatre`, `nugget`, `lebanon6`

### Examples

Regenerate HTML from cached data (no network calls):
```
python scraper.py
```

Scrape everything and regenerate:
```
python scraper.py --sources=all
```

Scrape only nhhumanities with a 60-day window, then regenerate all:
```
python scraper.py --sources=nhhumanities --days=60
```
