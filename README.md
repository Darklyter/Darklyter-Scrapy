# tpdb scraper framework

> Since Darklyter is a pain in Dirtyracer's ass, this repo is a copy of what he is
> currently running as his Scrapy deployment. He's old school and doesn't go in for
> any of that Poetry nonsense. The actual "Official" TPDB Scrapy repo is located at
> https://github.com/ThePornDatabase/scrapy

The base classes, pipelines and helpers for building Scrapy spiders that submit
scenes, movies and performers to a ThePornDB-style API. This repository holds the
framework only. Spiders live in `tpdb/spiders/`, which ships empty.

## Layout

```
scrapy.cfg
requirements.txt
tpdb/
  BaseScraper.py            shared plumbing: selector map, regex, images, dates
  BaseSceneScraper.py       scene spiders subclass this
  BaseMovieScraper.py       movie spiders subclass this
  BasePerformerScraper.py   performer spiders subclass this
  BaseOCR.py                tesseract wrapper for image-only metadata
  items.py                  SceneItem, MovieItem, PerformerItem and friends
  pipelines.py              API submission, retries, export, tag filtering
  middlewares.py            per-type downloader middlewares
  custommiddlewares.py      proxy middleware
  monitors.py               spidermon close monitors
  validators.py             spidermon item validation models
  helpers/
    http.py                 httpx wrapper, sync and async
    dbhelper.py             postgres connection for the movie cache
    credentials.py          member-site logins read from credentials.ini
    flare_solverr.py        FlareSolverr client
    mediacheck.py           media file inspection
    scenefileindex.py       local scene file index
    scrapy_dpath/           JSON responses addressable with dpath selectors
    scrapy_flare/           FlareSolverr downloader middleware
  spiders/                  your spiders go here
```

## Setup

```bash
pip install -r requirements.txt
cp tpdb/settings.py.example tpdb/settings.py
```

Then edit `tpdb/settings.py` and fill in every line marked `CHANGE ME`. At minimum
that is `TPDB_API_KEY`. Without a key the pipelines scrape and display but never
submit, which is the same as passing `-s local=true`.

Two optional config files, both gitignored, both with an `.example` alongside:

- `tpdb/credentials.ini` for member-site logins, read by `helpers/credentials.py`.
- `tpdb/database_tpdb.ini` for the Postgres movie cache. Only the movie path uses
  it, so scene and performer scrapes work without it.

`tpdb/settings.py` is gitignored. Keep it that way: it holds your API keys.

## Writing a spider

Subclass the base for the type you are scraping and fill in `selector_map`. The
base class handles pagination, date filtering, image fetching and submission.

```python
from tpdb.BaseSceneScraper import BaseSceneScraper


class ExampleSpider(BaseSceneScraper):
    name = 'Example'
    network = 'Example Network'

    start_urls = ['https://example.com']

    selector_map = {
        'title': '//h1/text()',
        'description': '//div[@class="description"]/text()',
        'date': '//span[@class="date"]/text()',
        'image': '//video/@poster',
        'performers': '//a[contains(@href, "/model/")]/text()',
        'tags': '//a[contains(@href, "/tag/")]/text()',
        'external_id': r'/video/(\d+)',
        'pagination': '/videos/page/%s',
    }

    def get_scenes(self, response):
        meta = self.copy_meta(response)
        for scene in response.xpath('//a[@class="scene-link"]/@href').getall():
            yield scrapy.Request(url=self.format_link(response, scene),
                                 callback=self.parse_scene, meta=meta)
```

Selectors starting with `//` or `./` are XPath, a single `/` is a dpath expression
against the JSON body, and anything else is treated as CSS. A `re_<field>` entry
applies a regex to whatever the matching selector returned.

Use `self.copy_meta(response)` rather than `meta = response.meta`. The latter
forwards Scrapy's own bookkeeping keys into the new request, which trips the
MetaCopyDetection warning, and it aliases rather than copies.

## Running

```bash
scrapy crawl Example -s local=true -s display=true
```

Always test with `-s local=true`. Without it, a successful crawl posts straight to
the live API.

Useful flags:

| Flag | Effect |
|---|---|
| `-s local=true` | scrape and display, never submit |
| `-s display=true` | one line per item |
| `-s export=true` | write items to JSON under `DEFAULT_EXPORT_PATH` |
| `-s file=name.json` | export filename |
| `-a limit_pages=all` | do not stop after the first page |
| `-a days=30` | only items from the last 30 days |
| `-s force_update=true -s force_fields=image` | re-fetch named fields; both flags are required together |

## Notes

- The pipelines are async and await their API calls, so the crawl keeps running
  during submission and retries. Do not switch `TWISTED_REACTOR` away from asyncio.
- Submissions retry on connection failure and on 429 and 5xx, with backoff. Other
  4xx responses are rejections of the payload and are not retried.
- `FILTER_TAG_FILENAME` is resolved relative to the directory you run `scrapy`
  from, not to this package.
