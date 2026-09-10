# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html

import asyncio
import hashlib
import re
import os.path
import string
import json
import logging
import time
import socket
import html
import unidecode
# ~ from http.client import responses

from pathlib import Path
from datetime import datetime
from tpdb.helpers.dbhelper import db_conn
from pymongo import MongoClient
from scrapy.exporters import JsonItemExporter, JsonLinesItemExporter

from tpdb.helpers.http import Http


class TpdbPipeline:
    def process_item(self, item):
        return item


class TpdbSubmitMixin:
    """Shared submission logic for the scene, movie and performer pipelines.

    Keeping this in one place is what stops the three copies drifting apart, which
    is how one of them ended up posting its last retry to the wrong endpoint.
    """

    # Worth another go: the request never landed, we were throttled, or the server
    # had a bad moment.  Any other 4xx is a rejection of this payload and will be
    # rejected again just as fast, so it isn't retried.
    RETRY_CODES = {429, 500, 502, 503, 504}

    _site_conversions = None

    @staticmethod
    def apply_defaults(item, defaults):
        """Give every field a value before anything reads it.

        Fields the item class doesn't declare are skipped: assigning one of those
        raises, and PerformerItemExport genuinely has fewer fields than PerformerItem.
        """
        for field, default in defaults:
            if field not in item.fields:
                continue
            if field not in item or item[field] is None:
                item[field] = default

    def get_site_conversions(self):
        """Site rename table from site_convert.json, read once per run.

        The handle is closed either way, and a missing or malformed file just means
        no renames rather than a failed crawl.
        """
        if self._site_conversions is None:
            conversions = []
            if os.path.isfile('site_convert.json'):
                try:
                    with open('site_convert.json', 'r', encoding='utf-8') as conv_file:
                        conversions = json.load(conv_file).get('sites', [])
                except (OSError, ValueError) as ex:
                    logging.error(f"Could not read site_convert.json, skipping site renames: {ex}")
            self._site_conversions = conversions
        return self._site_conversions

    @staticmethod
    def error_message(response):
        """Pull a human-readable error out of a response without trusting its shape."""
        try:
            body = response.json()
        except Exception:
            return response.text[:200]

        if isinstance(body, dict) and body.get('message'):
            return str(body['message'])
        return response.text[:200]

    async def submit(self, url, payload, headers, attempts=3, count_stats=True):
        """POST `payload` to `url`, returning (submitted, message).

        Exactly one submit_good or submit_bad is recorded per item however many
        attempts it takes, so the stats stay a count of items rather than tries.
        """
        detail = 'no response'

        for attempt in range(1, attempts + 1):
            response = await Http.post_async(url, json=payload, headers=headers, verify=False)

            if response is None:
                detail = 'no response'
            elif response.is_success:
                if count_stats:
                    self.crawler.stats.inc_value('submit_good')
                return True, 'Submitted OK' if attempt == 1 else f'Submitted OK on attempt {attempt}'
            elif response.status_code in self.RETRY_CODES:
                detail = f'code #{response.status_code}'
            else:
                if count_stats:
                    self.crawler.stats.inc_value('submit_bad')
                return False, f'Submission Error: code #{response.status_code} - {self.error_message(response)}'

            if attempt < attempts:
                # Awaited, not slept: the crawl keeps working through the backoff.
                await asyncio.sleep(2 ** attempt)

        if count_stats:
            self.crawler.stats.inc_value('submit_bad')
        return False, f'Submission Error after {attempts} attempts: {detail}'


class TpdbApiScenePipeline(TpdbSubmitMixin):
    def __init__(self, crawler):
        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            self.db = db['scrapy']

        self.crawler = crawler

        if crawler.settings.get('path'):
            path = crawler.settings.get('path')
        else:
            path = crawler.settings.get('DEFAULT_EXPORT_PATH')

        if crawler.settings.get('FILTER_TAGS'):
            logging.info(f"Loading Scene Tag Alias File: {crawler.settings.get('FILTER_TAG_FILENAME')}")
            with open(crawler.settings.get('FILTER_TAG_FILENAME'), encoding='utf-8') as f:
                self.tagaliases = json.load(f)

        if crawler.settings.get('file'):
            filename = crawler.settings.get('file')
            if '\\' not in filename and '/' not in filename:
                filename = Path(path, filename)
        else:
            filename = Path(path, f'{crawler.spidercls.name}_{time.strftime("%Y%m%d-%H%M")}.json')

        if crawler.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            print(f'*** Exporting to file: {filename}')
            self.fp = open(filename, 'wb')
            self.fp.write('{"scenes":['.encode())

            if crawler.settings.getbool('oneline'):
                self.exporter = JsonLinesItemExporter(self.fp, ensure_ascii=False, encoding='utf-8')
            else:
                self.exporter = JsonItemExporter(self.fp, ensure_ascii=False, encoding='utf-8', sort_keys=True, indent=2)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    async def process_item(self, item):
        spider = self.crawler.spider
        if spider.debug is True:
            return item

        # So we don't re-send scenes that have already been scraped
        if self.crawler.settings['ENABLE_MONGODB']:
            if spider.force is not True:
                result = self.db.scenes.find_one({'url': item['url']})
                if result is not None:
                    return

        missing_fields = []
        if 'title' not in item:
            missing_fields.append('title')

        if 'id' not in item:
            missing_fields.append('id')

        if 'date' not in item:
            missing_fields.append('date')

        if 'tags' not in item:
            missing_fields.append('tags')

        if 'performers' not in item:
            missing_fields.append('performers')

        if missing_fields:
            print(f"Aborting due to missing fields: {', '.join(missing_fields)} for URL: {item['url']}")
            itemprint = ", ".join(f"{key}{value}" for key, value in item.items())
            print(f"Item: {itemprint}")
            return item

        # Everything the payload reads gets a safe default before anything touches it.
        # A spider that leaves a field unset should produce an empty value, not take
        # the pipeline down with a KeyError halfway through a crawl.  'date' is absent
        # on purpose: None is a valid date and must stay distinguishable from ''.
        for field, default in (
            ('back', ''), ('back_blob', ''), ('description', ''), ('director', ''),
            ('directors', []), ('duration', ''), ('format', ''), ('image', ''),
            ('image_blob', ''), ('markers', []), ('merge_id', ''), ('movies', []),
            ('network', ''), ('performers', []), ('performers_data', []),
            ('poster', ''), ('poster_blob', ''), ('scenes', []), ('site', ''),
            ('sku', ''), ('store', ''), ('tags', []), ('title', ''), ('trailer', ''),
            ('type', 'Scene'), ('url', ''),
        ):
            if field not in item or item[field] is None:
                item[field] = default

        if spider.settings.get('force_date'):
            force_date = True
        else:
            force_date = False

        if spider.settings.get('force_fields'):
            force_fields = spider.settings.get('force_fields').split(",")
        elif "force_fields" in item:
            if type(item['force_fields']) is list:
                force_fields = item['force_fields']
            else:
                force_fields = item['force_fields'].split(",")
        else:
            force_fields = False

        if ((spider.settings.get('force_update') or ("force_update" in item)) and force_fields):
            force_update = True
        else:
            force_update = False

        if item['tags']:
            if "Jav" in item['tags']:
                jav = True
                item['tags'].append("Jav")
            else:
                jav = False
            if isinstance(item.get('tags'), list):
                item['tags'] = item['tags'] = list(map(lambda x: string.capwords(x.strip()), item['tags']))

        if force_update and force_fields and len(force_fields):
            if (not item['image'] or not item['image_blob']) and "image" in force_fields:
                force_fields.remove("image")

        if self.crawler.settings['FILTER_TAGS']:
            item['tags'] = self.clean_tags(item['tags'], self.tagaliases)

        if item['date']:
            item_date = re.search(r'(\d{4}-\d{2}-\d{2})', item['date'])
            if item_date:
                item['date'] = item_date.group(1)
            else:
                # Nothing that looks like a date in there.  No date is a valid
                # payload, so report it and send the scene without one.
                logging.warning(f"Ignoring unrecognised date '{item['date']}' for {item['url']}")
                item['date'] = None

        # Check to see if the returned site is in our "site_convert.json" cross-reference file
        testsite = re.sub(r'[^a-z0-9]', '', item['site'].lower())
        for site in self.get_site_conversions():
            if site.get('orig') == testsite:
                item['site'] = site.get('rename', item['site'])
                break

        # Either 'director' or 'directors' may be set, by either name, as a list or
        # as a string that may or may not contain commas.  Whichever is populated is
        # normalised to a list here.
        director_source = item['directors'] or item['director']
        if not director_source:
            item['directors'] = []
        elif isinstance(director_source, list):
            item['directors'] = [str(name).strip() for name in director_source if str(name).strip()]
        else:
            item['directors'] = [name.strip() for name in str(director_source).split(",") if name.strip()]

        if item['image']:
            item['image'] = item['image'].replace(" ", "%20")

        # ~ if re.search(r'(.*?\.\w{3,4})\?', item['image']):
            # ~ item['image'] = re.search(r'(.*?\.\w{3,4})\?', item['image']).group(1)

        # ~ if re.search(r'(.*?\.\w{3,4})\?', item['back']):
            # ~ item['back'] = re.search(r'(.*?\.\w{3,4})\?', item['back']).group(1)

        if item['trailer']:
            item['trailer'] = item['trailer'].replace(" ", "%20")

        item['title'] = re.sub('<[^<]+?>', '', item['title'])
        item['title'] = unidecode.unidecode(html.unescape(item['title']).strip()).strip()
        if len(item['title']) > 254:
            item['title'] = item['title'][:250] + "..."

        item['description'] = re.sub('<[^<]+?>', '', item['description'])
        item['description'] = unidecode.unidecode(html.unescape(item['description'])).strip()

        payload = {
            'back': item['back'],
            'back_blob': item['back_blob'],
            'date': item['date'],
            'description': item['description'],
            'directors': item['directors'],
            'duration': item['duration'],
            'external_id': str(item['id']),
            'merge_id': str(item['merge_id']),
            'force_update': self.crawler.settings.getbool('FORCE_UPDATE'),
            'format': item['format'],
            'image': item['image'],
            'image_blob': item['image_blob'],
            'markers': item['markers'],
            # ~ 'markers': [],
            'performers': item['performers'],
            'performers_data': item['performers_data'],
            'poster': item['poster'],
            'poster_blob': item['poster_blob'],
            'movies': item['movies'],
            'scenes': item['scenes'],
            'site': item['site'],
            'network': item['network'],
            'sku': item['sku'],
            'store': item['store'],
            'tags': item['tags'],
            'title': item['title'],
            'trailer': item['trailer'],
            'type': item['type'],
            'url': item['url'],
        }

        if "hashes" in item:
            payload['hashes_data'] = item['hashes']

        if "hashes_data" in item:
            payload['hashes_data'] = item['hashes_data']

        if "scene_id" in item:
            payload['scene_id'] = item['scene_id']

        if "parent" in item and item['parent'] != item['site']:
            payload['parent'] = item['parent']

        if force_date:
            payload['force_date'] = True

        if force_fields:
            payload['force_fields'] = force_fields

        if force_update:
            payload['force_update'] = True

        submitmovie = None
        disp_result = ""

        submitmovie = True
        if not spider.settings.get('localdump') and not spider.settings.get('stashdump') and not spider.settings.get('local') and item['type'].lower() == "movie":
            conn, cursor = db_conn()

            # Real quick we'll make our comparison fields.
            shorttitle = re.sub('[^0-9a-z]', '', item['title'].lower())
            shortsite = re.sub('[^0-9a-z]', '', item['site'].lower())
            if item['date']:
                movieyear = re.search(r'(\d{4})', item['date']).group(1)
            else:
                movieyear = "1971"

            ##############################################
            # Check for Movie existence in cache
            #

            if not force_update:
                # First we'll check the simple combo of 'shorttitle', 'shortsite' and year
                cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite = %s and year = %s", (shorttitle, shortsite, movieyear))
                if cursor.rowcount:
                    movieid = cursor.fetchone()[0]
                    submitmovie = False
                    disp_result = f"Not submitting due to exact duplicate in cache:  ID# {movieid}"

                if submitmovie:
                    cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite LIKE %s and year = %s", (shorttitle, shortsite, movieyear))
                    if cursor.rowcount:
                        movieid = cursor.fetchone()[0]
                        submitmovie = False
                        disp_result = f"Not submitting due to fuzzy duplicate in cache:  ID# {movieid}"

                if submitmovie:
                    test_title = item['title'].lower()
                    test_title = test_title.replace("vol.", "")
                    test_title = test_title.replace(" vol ", " ")
                    test_title = test_title.replace(" volume ", " ")
                    test_title = test_title.replace("pt.", "")
                    test_title = test_title.replace(" pt ", " ")
                    test_title = test_title.replace(" part ", " ")
                    test_number = re.search(r'#(\d+)', test_title)
                    if test_number:
                        test_number = test_number.group(1)
                        test_title = test_title.replace(f"#{test_number}", str(int(test_number)))
                    test_title = test_title.replace("  ", " ")

                    shorttitle = re.sub('[^0-9a-z]', '', test_title)
                    cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite LIKE %s and year = %s", (shorttitle, shortsite, movieyear))
                    if cursor.rowcount:
                        movieid = cursor.fetchone()[0]
                        submitmovie = False
                        disp_result = f"Not submitting due to match without Volume/Part in cache:  ID# {movieid}"

            cursor.close()
            conn.close()

        tempsku = ''
        if spider.settings.get('localdump') or spider.settings.get('stashdump'):
            if "phashes" in item:
                payload['phashes'] = item['phashes']
            else:
                payload['phashes'] = []
            if "oshashes" in item:
                payload['oshashes'] = item['oshashes']
            else:
                payload['oshashes'] = []

            if "uuid" in item:
                payload['uuid'] = item['uuid']

            if "tpdbimage" in item:
                payload['tpdbimage'] = item['tpdbimage']

            if not payload['store']:
                payload['store'] = []

        if not item['type'].lower() == "movie" or (item['type'].lower() == "movie" and submitmovie):
            if payload['sku']:
                tempsku = payload['sku']
                payload['sku'] = ""
            else:
                tempsku = ''

            # payload['merge_id'] = ""
            # Post the scene to the API - requires auth with permissions
            if self.crawler.settings['TPDB_API_KEY'] and not spider.settings.get('local'):
                headers = {
                    'Authorization': f'Bearer {self.crawler.settings["TPDB_API_KEY"]}',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'tpdb-scraper/1.0.0'
                }
                submitted, result = await self.submit('https://api.theporndb.net/scenes', payload, headers)
                disp_result = f'{disp_result} {result}'
                if not submitted:
                    logging.info(f"Submission failed for {item['url']}: {result}")

                if submitted and item['type'].lower() == "movie":
                    conn, cursor = db_conn()

                    # Real quick we'll make our comparison fields.  Keeping them in item just to easily keep track of them
                    shorttitle = re.sub('[^0-9a-z]', '', item['title'].lower())
                    shortsite = re.sub('[^0-9a-z]', '', item['site'].lower())
                    if item['date']:
                        movieyear = re.search(r'(\d{4})', item['date']).group(1)
                    else:
                        movieyear = "1971"

                    ##############################################
                    # Insert new Movie into TPDB cache
                    #

                    # Let's get the ip address of the local movie, for tracking down who is sending information
                    hostname = socket.gethostname()

                    submitmovie = True
                    # First we'll check the simple combo of 'shorttitle', 'shortsite' and year
                    cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite = %s and year = %s", (shorttitle, shortsite, movieyear))
                    if cursor.rowcount:
                        movieid = cursor.fetchone()[0]
                        submitmovie = False
                        disp_result = f"Not caching due to existing exact duplicate in database:  ID# {movieid}"

                    if submitmovie:
                        test_title = item['title'].lower()
                        test_title = test_title.replace("vol.", "")
                        test_title = test_title.replace(" vol ", " ")
                        test_title = test_title.replace(" volume ", " ")
                        test_title = test_title.replace("pt.", "")
                        test_title = test_title.replace(" pt ", " ")
                        test_title = test_title.replace(" part ", " ")
                        test_number = re.search(r'#(\d+)', test_title)
                        if test_number:
                            test_number = test_number.group(1)
                            test_title = test_title.replace(f"#{test_number}", str(int(test_number)))
                        test_title = test_title.replace("  ", " ")

                        shorttitle = re.sub('[^0-9a-z]', '', test_title)
                        cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite LIKE %s and year = %s", (shorttitle, shortsite, movieyear))
                        if cursor.rowcount:
                            movieid = cursor.fetchone()[0]
                            submitmovie = False
                            disp_result = f"Not caching due to match without Volume/Part in cache:  ID# {movieid}"

                    if submitmovie:
                        # First we'll check the simple combo of 'shorttitle', 'shortsite' and year
                        cursor.execute("INSERT INTO movies (title, site, date, url, external_id, shorttitle, shortsite, year, ipaddress) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", (item['title'], item['site'], item['date'], item['url'], item['id'], shorttitle, shortsite, movieyear, hostname))
                        if cursor.rowcount:
                            conn.commit()
                            movieid = cursor.fetchone()[0]
                            disp_result = disp_result + f" (Movie cached:  ID# {movieid})"
                        else:
                            disp_result = disp_result + " (Movie Caching Error!)"

                    cursor.close()
                    conn.close()

            else:
                disp_result = 'Local Run, Not Submitted'
                self.crawler.stats.inc_value('local_run')
        else:
            if not disp_result:
                disp_result = 'Local Movie Run, Not Submitted'
            self.crawler.stats.inc_value('local_run')

        payload['sku'] = tempsku
        # ~ print(payload)
        if (spider.settings.get('localdump') and not payload['format'] == "StashDB") or payload['format'] == "TPDB":
            # Toss to local TPDB Instance
            headers = {
                'Content-Type': 'application/json'
            }
            local_url = self.crawler.settings.get('LOCAL_TPDB_URL', 'http://127.0.0.1:8000/scenes')
            _, result = await self.submit(local_url, payload, headers, count_stats=False)
            disp_result = f'{disp_result}\tLocal TPDB: {result}'
            # #############################

        # ~ print(payload)
        if (spider.settings.get('stashdump') and not payload['format'] == "TPDB") or payload['format'] == "StashDB":
            # Toss to local StashDB Instance
            headers = {
                'Content-Type': 'application/json'
            }
            local_url = self.crawler.settings.get('LOCAL_STASHDB_URL', 'http://127.0.0.1:8000/stashscenes')
            _, result = await self.submit(local_url, payload, headers, count_stats=False)
            disp_result = f'{disp_result}\tLocal StashDB: {result}'
            # #############################

        if (spider.settings.getbool('display') or self.crawler.settings['DISPLAY_ITEMS']) and spider.settings.get('LOG_LEVEL') == 'INFO':
            if len(item['title']) >= 50:
                title_length = 5
            else:
                title_length = 55 - len(item['title'])

            if len(item['site']) >= 15:
                site_length = 5
            else:
                site_length = 20 - len(item['site'])

            if item['date']:
                if "T" in item['date']:
                    disp_date = re.search(r'(.*)T\d', item['date']).group(1)
                else:
                    disp_date = item['date']
            else:
                disp_date = "Calculated"

            logging.info(f"Item: {item['title'][0:50]}" + " " * title_length + f"{item['site'][0:15]}" + " " * site_length + f"\t{str(item['id'])[0:15]}\t{disp_date}\t{item['url']}\t{disp_result}")

        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            item2 = payload.copy()
            if not spider.settings.get('showblob'):
                if 'image_blob' in item2:
                    item2.pop('image_blob', None)
                if 'back_blob' in item2:
                    item2.pop('back_blob', None)
                if 'poster_blob' in item2:
                    item2.pop('poster_blob', None)
                if item2['performers']:
                    for performer in item2['performers']:
                        if 'image_blob' in performer:
                            performer['image_blob'] = ""
                if item2['performers_data']:
                    for performer in item2['performers_data']:
                        if 'image_blob' in performer:
                            performer['image_blob'] = ""
            self.exporter.export_item(item2)

        return item

    def clean_tags(self, tags, aliaslist):
        tags2 = []
        if tags:
            for tag in tags:
                pointer = 0
                for alias in aliaslist:
                    if not pointer and tag.lower().strip() == alias['alias'].lower().strip():
                        tags2.append(alias['tag'])
                        pointer = 1
                        break
                if not pointer:
                    tags2.append(tag.rstrip(".").rstrip(",").strip())

        tags2 = [i for n, i in enumerate(tags2) if i not in tags2[:n]]
        if "Assorted Additional Tags" in tags2:
            tags2.remove("Assorted Additional Tags")
        return tags2

    def close_spider(self):
        spider = self.crawler.spider
        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            self.fp.write(']}'.encode())
            self.fp.close()


class TpdbApiMoviePipeline(TpdbSubmitMixin):
    def __init__(self, crawler):
        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            self.db = db['scrapy']

        self.crawler = crawler

        if crawler.settings.get('path'):
            path = crawler.settings.get('path')
        else:
            path = crawler.settings.get('DEFAULT_EXPORT_PATH')

        if crawler.settings.get('FILTER_TAGS'):
            logging.info(f"Loading Movie Tag Alias File: {crawler.settings.get('FILTER_TAG_FILENAME')}")
            with open(crawler.settings.get('FILTER_TAG_FILENAME'), encoding='utf-8') as f:
                self.tagaliases = json.load(f)

        if crawler.settings.get('file'):
            filename = crawler.settings.get('file')
            if '\\' not in filename and '/' not in filename:
                filename = Path(path, filename)
        else:
            filename = Path(path, f'{crawler.spidercls.name}_{time.strftime("%Y%m%d-%H%M")}.json')

        if crawler.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            print(f'*** Exporting to file: {filename}')
            self.fp = open(filename, 'wb')
            self.fp.write('{"movies":['.encode())

            if crawler.settings.getbool('oneline'):
                self.exporter = JsonLinesItemExporter(self.fp, ensure_ascii=False, encoding='utf-8')
            else:
                self.exporter = JsonItemExporter(self.fp, ensure_ascii=False, encoding='utf-8', sort_keys=True, indent=2)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    async def process_item(self, item):
        spider = self.crawler.spider
        if spider.debug is True:
            return item

        # So we don't re-send scenes that have already been scraped
        if self.crawler.settings['ENABLE_MONGODB']:
            if spider.force is not True:
                result = self.db.scenes.find_one({'url': item['url']})
                if result is not None:
                    return

        # Same contract as the scene pipeline: every field the payload reads has a
        # value before anything touches it.  'date' stays out, None is valid there.
        for field, default in (
            ('back', ''), ('back_blob', ''), ('description', ''), ('director', ''),
            ('directors', []), ('duration', ''), ('format', ''), ('front', ''),
            ('front_blob', ''), ('length', ''), ('markers', []), ('performers', []),
            ('rating', ''), ('site', ''), ('sku', ''), ('store', ''), ('studio', ''),
            ('tags', []), ('title', ''), ('trailer', ''), ('upc', ''), ('url', ''),
            ('year', ''),
        ):
            if field not in item or item[field] is None:
                item[field] = default

        if self.crawler.settings['FILTER_TAGS']:
            item['tags'] = self.clean_tags(item['tags'], self.tagaliases)

        # Either name, list or comma string, same as the scene pipeline.
        director_source = item['directors'] or item['director']
        if not director_source:
            item['directors'] = []
        elif isinstance(director_source, list):
            item['directors'] = [str(name).strip() for name in director_source if str(name).strip()]
        else:
            item['directors'] = [name.strip() for name in str(director_source).split(",") if name.strip()]

        if item['length'] and not item['duration']:
            # Lengths arrive as bare minutes or as free text like "92 min", so pull
            # the number out rather than trusting int() with the whole string.
            length_match = re.search(r'(\d+)', str(item['length']))
            if length_match:
                length = int(length_match.group(1))
                if length < 450:
                    length = length * 60
                item['length'] = str(length)
                item['duration'] = str(length)
            else:
                logging.warning(f"Ignoring unrecognised length '{item['length']}' for {item['url']}")

        payload = {
            'title': item['title'],
            'description': item['description'],
            'site': item['site'],
            'date': item['date'],
            'front': item['front'],
            'front_blob': item['front_blob'],
            'back': item['back'],
            'back_blob': item['back_blob'],
            'performers': item['performers'],
            'tags': item['tags'],
            'url': item['url'],
            'external_id': str(item['id']),
            'trailer': item['trailer'],
            'markers': item['markers'],
            'studio': item['studio'],
            'directors': item['directors'],
            'format': item['format'],
            'length': item['length'],
            'duration': item['duration'],
            'year': item['year'],
            'rating': item['rating'],
            'sku': item['sku'],
            'upc': item['upc'],
            'store': item['store'],
            'force_update': self.crawler.settings.getbool('FORCE_UPDATE'),
        }

        # Post the scene to the API - requires auth with permissions
        disp_result = ""
        if self.crawler.settings['TPDB_API_KEY'] and not spider.settings.get('local'):
            headers = {
                'Authorization': f'Bearer {self.crawler.settings["TPDB_API_KEY"]}',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': 'tpdb-scraper/1.0.0'
            }

            submitted, result = await self.submit('https://api.theporndb.net/movies', payload, headers)
            disp_result = f'{disp_result} {result}'

            if self.crawler.settings['ENABLE_MONGODB']:
                url_hash = hashlib.sha1(str(item['url']).encode('utf-8')).hexdigest()
                if submitted:
                    self.db.scenes.replace_one({'_id': url_hash}, dict(item), upsert=True)
                else:
                    self.db.errors.replace_one({'_id': url_hash}, {
                        'url': item['url'],
                        'error': 1,
                        'when': datetime.now().isoformat(),
                        'response': result,
                    }, upsert=True)
        else:
            disp_result = 'Local Run, Not Submitted'

        if spider.settings.get('localdump'):
            # Toss to local TPDB Instance
            headers = {
                'Authorization': f'Bearer {self.crawler.settings["TPDB_TEST_API_KEY"]}',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': 'tpdb-scraper/1.0.0'
            }
            _, result = await self.submit(self.crawler.settings.get('LOCAL_TPDB_MOVIE_URL', 'http://api.tpdb.test/movies'), payload, headers, count_stats=False)
            disp_result = f'{disp_result}\tLocal: {result}'
            # #############################

        if (spider.settings.getbool('display') or self.crawler.settings['DISPLAY_ITEMS']) and spider.settings.get('LOG_LEVEL') == 'INFO':
            if len(item['title']) >= 50:
                title_length = 5
            else:
                title_length = 55 - len(item['title'])

            if len(item['site']) >= 15:
                site_length = 5
            else:
                site_length = 20 - len(item['site'])

            if not item.get('date'):
                disp_date = "Calculated"
            elif "T" in item['date']:
                disp_date = re.search(r'(.*)T\d', item['date']).group(1)
            else:
                disp_date = item['date']

            logging.info(f"Item: {item['title'][0:50]}" + " " * title_length + f"{item['site'][0:15]}" + " " * site_length + f"\t{str(item['id'])[0:15]}\t{disp_date}\t{item['url']}\t{disp_result}")

        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            item2 = item.copy()
            if not spider.settings.get('showblob'):
                if 'front_blob' in item2:
                    item2.pop('front_blob', None)
                if 'back_blob' in item2:
                    item2.pop('back_blob', None)
            self.exporter.export_item(item2)

        return item

    def clean_tags(self, tags, aliaslist):
        tags2 = []
        if tags:
            for tag in tags:
                pointer = 0
                for alias in aliaslist:
                    if not pointer and tag.lower().strip() == alias['alias'].lower().strip():
                        tags2.append(alias['tag'])
                        pointer = 1
                        break
                if not pointer:
                    tags2.append(tag.rstrip(".").rstrip(",").strip())

        tags2 = [i for n, i in enumerate(tags2) if i not in tags2[:n]]
        return tags2

    def close_spider(self):
        spider = self.crawler.spider
        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            self.fp.write(']}'.encode())
            self.fp.close()


class TpdbApiPerformerPipeline(TpdbSubmitMixin):
    def __init__(self, crawler):
        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            self.db = db['scrapy']

        self.crawler = crawler

        if crawler.settings.get('path'):
            path = crawler.settings.get('path')
        else:
            path = crawler.settings.get('DEFAULT_EXPORT_PATH')

        if crawler.settings.get('file'):
            filename = crawler.settings.get('file')
            if '\\' not in filename and '/' not in filename:
                filename = Path(path, filename)
        else:
            filename = Path(path, f'{crawler.spidercls.name}_{time.strftime("%Y%m%d-%H%M")}-performers.json')

        if crawler.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            print(f"*** Exporting to file: {filename}")
            self.fp = open(filename, 'wb')
            self.fp.write('{"scenes":['.encode())

            if crawler.settings.getbool('oneline'):
                self.exporter = JsonLinesItemExporter(self.fp, ensure_ascii=False, encoding='utf-8')
            else:
                self.exporter = JsonItemExporter(self.fp, ensure_ascii=False, encoding='utf-8', sort_keys=True, indent=2)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    async def process_item(self, item):
        spider = self.crawler.spider
        if self.crawler.settings['ENABLE_MONGODB']:
            if spider.force is not True:
                result = self.db.performers.find_one({'url': item['url']})
                if result is not None:
                    return

        self.apply_defaults(item, (
            ('astrology', ''), ('bio', ''), ('birthday', ''), ('birthplace', ''),
            ('birthplace_code', ''), ('cupsize', ''), ('ethnicity', ''),
            ('eyecolor', ''), ('gender', ''), ('haircolor', ''), ('height', ''),
            ('image', None), ('image_blob', None), ('measurements', ''), ('name', ''),
            ('nationality', ''), ('network', ''), ('piercings', ''), ('site', ''),
            ('tattoos', ''), ('url', ''), ('weight', ''),
        ))

        if item.get('fakeboobs') and isinstance(item['fakeboobs'], str):
            if item['fakeboobs'].lower() == 'yes':
                item['fakeboobs'] = True
            elif item['fakeboobs'].lower() == 'no':
                item['fakeboobs'] = False
            else:
                item['fakeboobs'] = None

        if item['gender'] == "Male":
            item['fakeboobs'] = False

        gender = item['gender'].lower()
        if "male" not in gender and "female" not in gender and "trans" not in gender and "binary" not in gender:
            item['gender'] = None

        # Whichever of site/network the spider supplied fills the other, matching
        # what the base performer scraper now does.
        if 'site' in item.fields:
            if not item['site'] and item['network']:
                item['site'] = item['network']
            elif not item['network'] and item['site']:
                item['network'] = item['site']

        if 'imageFilename' not in item:
            payload = {
                'name': item['name'],
                'site': item['site'],
                'network': item['network'],
                'url': item['url'],
                'bio': item['bio'],
                'image': item['image'],
                'image_blob': item['image_blob'],
                'extra': {
                    'gender': item['gender'],
                    'birthday': item['birthday'],
                    'astrology': item['astrology'],
                    'birthplace': item['birthplace'],
                    'birthplace_code': item['birthplace_code'],
                    'ethnicity': item['ethnicity'],
                    'nationality': item['nationality'],
                    'haircolor': item['haircolor'],
                    'eyecolor': item['eyecolor'],
                    'weight': item['weight'],
                    'height': item['height'],
                    'measurements': item['measurements'],
                    'tattoos': item['tattoos'],
                    'piercings': item['piercings'],
                    'cupsize': item['cupsize'],
                    'fakeboobs': item['fakeboobs']
                }
            }

            # Post the scene to the API - requires auth with permissions
            disp_result = ""
            if self.crawler.settings['TPDB_API_KEY'] and not spider.settings.get('local'):
                headers = {
                    'Authorization': f'Bearer {self.crawler.settings["TPDB_API_KEY"]}',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'tpdb-scraper/1.0.0'
                }

                submitted, result = await self.submit('https://api.theporndb.net/performer-sites', payload, headers)
                disp_result = f'{disp_result} {result}'

                if self.crawler.settings['ENABLE_MONGODB']:
                    url_hash = hashlib.sha1(str(item['url']).encode('utf-8')).hexdigest()
                    if submitted:
                        self.db.performers.replace_one({'_id': url_hash}, dict(item), upsert=True)
                    else:
                        self.db.errors.replace_one({'_id': url_hash}, {
                            'url': item['url'],
                            'error': 1,
                            'when': datetime.now().isoformat(),
                            'response': result,
                        }, upsert=True)
            else:
                disp_result = 'Local Run, Not Submitted'

            if spider.settings.get('localdump'):
                # Toss to local TPDB Instance
                headers = {
                    'Authorization': f'Bearer {self.crawler.settings["TPDB_TEST_API_KEY"]}',
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'tpdb-scraper/1.0.0'
                }

                # Note: the local test instance is addressed with an underscore here,
                # unlike the production '/performer-sites'.  Left as it was found.
                _, result = await self.submit(self.crawler.settings.get('LOCAL_TPDB_PERFORMER_URL', 'http://api.tpdb.test/performer_sites'), payload, headers, count_stats=False)
                disp_result = f'{disp_result}\tLocal: {result}'
                # ##############################

            if (spider.settings.getbool('display') or self.crawler.settings['DISPLAY_ITEMS']) and spider.settings.get('LOG_LEVEL') == 'INFO':
                name_length = 50 - len(payload['name'])
                if name_length < 1:
                    name_length = 1

                logging.info(f"Performer: {payload['name']}" + " " * name_length + f"{payload['site']}\t{payload['url']}\t{disp_result}")

            if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
                item2 = payload.copy()
                if not spider.settings.get('showblob'):
                    if "image_blob" in item2:
                        item2.pop('image_blob', None)
                self.exporter.export_item(item2)

            return item

        else:
            payload = {
                'name': item['name'],
                'site': item['network'],
                'network': item['network'],
                'url': item['url'],
                'bio': item['bio'],
                'image': item['image'],
                'imageFilename': item['imageFilename'],
                'image_blob': item['image_blob'],
                'extra': {
                    'gender': item['gender'],
                    'birthday': item['birthday'],
                    'astrology': item['astrology'],
                    'birthplace': item['birthplace'],
                    'ethnicity': item['ethnicity'],
                    'nationality': item['nationality'],
                    'haircolor': item['haircolor'],
                    'eyecolor': item['eyecolor'],
                    'weight': item['weight'],
                    'height': item['height'],
                    'measurements': item['measurements'],
                    'tattoos': item['tattoos'],
                    'piercings': item['piercings'],
                    'cupsize': item['cupsize'],
                    'fakeboobs': item['fakeboobs']
                }
            }

            if (spider.settings.getbool('display') or self.crawler.settings['DISPLAY_ITEMS']) and spider.settings.get('LOG_LEVEL') == 'INFO':
                name_length = 50 - len(payload['name'])
                if name_length < 1:
                    name_length = 1

                logging.info(f"Performer: {payload['name']}" + " " * name_length + f"{payload['site']}\t{payload['url']}\tExport Filename: {payload['imageFilename']}")

            if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
                item2 = payload.copy()
                if not spider.settings.get('showblob'):
                    if "image_blob" in item2:
                        item2.pop('image_blob', None)
                self.exporter.export_item(item2)

            return item

    def close_spider(self):
        spider = self.crawler.spider
        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            self.fp.write(']}'.encode())
            self.fp.close()
