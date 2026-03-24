# Dartmouth Event Scraper

Scrapes the next 30 days of events from
[home.dartmouth.edu/events](https://home.dartmouth.edu/events) and produces a
local self-contained HTML page: `output/events_YYYY-MM-DD_to_YYYY-MM-DD.html`.
Save time finding interesting events for members of the public without access
to Dartmouth's intranet.

![Screenshot](example_screenshot.png)

## Features

- Events not open to the public are collapsed by default
- Recurring events are merged into one entry with a list of dates
- Per-event "Add to Google Calendar" button(s)

## Requirements

```
pip install requests beautifulsoup4
```

## Usage

```
python scraper.py
```

Open `output/events_*.html` in a browser.

## Notes

- Scraped via Dartmouth's internal AJAX endpoint (`/events/ajax/search`)
- Takes about 15 seconds to generate.  (Rate-limited to 8 parallel requests with a small delay.)
