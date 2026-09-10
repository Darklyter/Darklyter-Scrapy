import sys
from datetime import date, timedelta, datetime
import re
import unidecode
from PIL import Image
import base64
from io import BytesIO
import html
import logging
import string
from abc import ABC
from urllib.parse import urlparse, unquote
from tpdb.helpers.dbhelper import db_conn

import dateparser
import scrapy
import tldextract

from furl import furl
from tpdb.helpers.http import Http
from scrapy.utils.project import get_project_settings


class BaseScraper(scrapy.Spider, ABC):
    limit_pages = 1
    force = False
    debug = False
    days = 9999
    max_pages = 100
    cookies = {}
    headers = {}
    page = 1

    custom_tpdb_settings = {}
    custom_scraper_settings = {}
    selector_map = {}
    regex = {}
    proxy_address = None

    title_trash = []
    description_trash = ['Description:']
    date_trash = ['Released:', 'Added:', 'Published:']

    # Set once the force_update/force_fields mismatch has been reported, so the
    # warning doesn't repeat for every image on every item.
    force_warned = False

    def __init__(self, *args, **kwargs):
        super(BaseScraper, self).__init__(*args, **kwargs)

        for name in self.get_selector_map():
            if (name == 'external_id' or name.startswith('re_')) and name in self.get_selector_map() and self.get_selector_map()[name]:
                regexp, group, mod = self.get_regex(self.get_selector_map(name))
                self.regex[name] = (re.compile(regexp, mod), group)

        self.days = int(self.days)
        if self.days < 9999:
            logging.info(f"Days to retrieve: {self.days}")
        self.force = bool(self.force)
        self.debug = bool(self.debug)
        self.page = int(self.page)

        if self.limit_pages is None:
            self.limit_pages = 1
        else:
            if self.limit_pages == 'all':
                self.limit_pages = sys.maxsize
            self.limit_pages = int(self.limit_pages)

    # ~ def update_settings(cls, settings):
        # ~ cls.custom_tpdb_settings.update(cls.custom_scraper_settings)
        # ~ settings.update(cls.custom_tpdb_settings)
        # ~ cls.headers['User-Agent'] = settings['USER_AGENT']
        # ~ if settings['DAYS']:
            # ~ cls.days = settings['DAYS']
        # ~ super(BaseScraper, cls).update_settings(settings)

    @classmethod
    def update_settings(cls, settings):
        super().update_settings(settings)

        merged = {}
        merged.update(getattr(cls, "custom_tpdb_settings", {}))
        merged.update(getattr(cls, "custom_scraper_settings", {}))

        for key, value in merged.items():
            settings.set(key, value, priority="spider")

        cls.headers["User-Agent"] = settings.get("USER_AGENT")

        if settings.get("DAYS"):
            cls.days = settings.get("DAYS")

    async def start(self):
        for r in self.start_requests():
            yield r

    def start_requests(self):
        settings = get_project_settings()

        if not hasattr(self, 'start_urls'):
            raise AttributeError('start_urls missing')

        if not self.start_urls:
            raise AttributeError('start_urls selector missing')

        # Informational only, so a slow or unreachable ipify must never hold up or
        # kill the crawl.  Http.get swallows transport errors and returns None.
        ip_response = Http.get('https://api.ipify.org', timeout=10)
        if ip_response is not None and ip_response.is_success:
            print('My public IP address is: {}'.format(ip_response.text.strip()))
        else:
            print('My public IP address is: unavailable (lookup failed)')

        meta = {}
        meta['page'] = self.page
        if 'USE_PROXY' in self.settings.attributes.keys():
            use_proxy = self.settings.get('USE_PROXY')
        elif 'USE_PROXY' in settings.attributes.keys():
            use_proxy = settings.get('USE_PROXY')
        else:
            use_proxy = None

        if use_proxy:
            print(f"Using Settings Defined Proxy: True ({settings.get('PROXY_ADDRESS')})")
        else:
            if self.proxy_address:
                meta['proxy'] = self.proxy_address
                print(f"Using Scraper Defined Proxy: True ({meta['proxy']})")
            else:
                print("Using Proxy: False")

        singleurl = self.settings.get('url')
        if singleurl:
            yield scrapy.Request(singleurl, callback=self.parse_scene, meta=meta, headers=self.headers, cookies=self.cookies)
        else:
            for link in self.start_urls:
                yield scrapy.Request(url=self.get_next_page_url(link, self.page), callback=self.parse, meta=meta, headers=self.headers, cookies=self.cookies)

    # Bookkeeping keys that Scrapy and its middlewares attach on the way back out.
    # They describe the response that already happened, so forwarding them into a
    # new request is meaningless at best and misleading to the middlewares that own
    # them at worst.  Keys that carry the spider's own intent (cookiejar, proxy,
    # dont_redirect, handle_httpstatus_list and friends) are deliberately kept.
    response_meta_keys = {
        '_auth_proxy',
        'depth',
        'download_latency',
        'download_slot',
        'is_start_request',
        'redirect_reasons',
        'redirect_times',
        'redirect_urls',
        'retry_times',
    }

    def copy_meta(self, response, **extra):
        """Return a forwardable copy of response.meta.

        Two things wrong with `meta = response.meta`: it hands Scrapy's own
        bookkeeping keys to the next request, which is what the MetaCopyDetection
        warning is about, and it aliases rather than copies, so `meta['page'] += 1`
        quietly edits the response's own meta.  This returns a fresh dict with the
        internal keys dropped.  Extra keyword arguments are merged in, so a
        pagination request reads `self.copy_meta(response, page=next_page)`.
        """
        meta = {key: value for key, value in response.meta.items()
                if key not in self.response_meta_keys}
        meta.update(extra)
        return meta

    def get_selector_map(self, attr=None):
        if hasattr(self, 'selector_map'):
            if attr is None:
                return self.selector_map
            if attr not in self.selector_map:
                raise AttributeError(f'{attr} missing from selector map')
            return self.selector_map[attr]
        raise NotImplementedError('selector map missing')

    def get_force_options(self):
        """Resolve the force_update / force_fields pair into (bool, list).

        force_update only means anything alongside force_fields, so a run that sets
        one without the other is reported once and then treated as not forcing.
        That keeps the mismatch out of the middle of a crawl, where it used to
        surface as `argument of type 'NoneType' is not iterable`.
        """
        force_update = bool(self.settings.get('force_update'))
        force_fields = self.settings.get('force_fields')

        if isinstance(force_fields, str):
            force_fields = [field.strip() for field in force_fields.split(",") if field.strip()]
        elif force_fields:
            force_fields = [str(field).strip() for field in force_fields if str(field).strip()]
        else:
            force_fields = []

        if force_update and not force_fields:
            if not self.force_warned:
                logging.error(
                    "force_update was set without force_fields, so nothing will be forced. "
                    "Pass -s force_fields=image,title,... alongside it.")
                self.force_warned = True
            force_update = False

        return force_update, force_fields

    def should_fetch(self, field):
        """True when `field` should be scraped for this run."""
        force_update, force_fields = self.get_force_options()
        return not force_update or field in force_fields

    def get_image(self, response, path=None):
        if self.should_fetch('image'):
            if 'image' in self.get_selector_map():
                image = self.get_element(response, 'image', 're_image')
                if isinstance(image, list):
                    image = image[0]
                image = image.strip()
                image = image.replace(" ", "%20")
                if path:
                    return self.format_url(path, image)
                else:
                    return self.format_link(response, image)
        return ''

    def get_back_image(self, response):
        if 'back' in self.get_selector_map():
            image = self.get_element(response, 'back', 're_back')
            if isinstance(image, list):
                image = image[0]
            return self.format_link(response, image)
        return ''

    def get_image_blob(self, response):
        if 'image_blob' not in self.get_selector_map():
            image = self.get_image(response)
            return self.get_image_blob_from_link(image)
        return None

    def get_image_back_blob(self, response):
        if 'image_blob' not in self.get_selector_map():
            image = self.get_back_image(response)
            return self.get_image_blob_from_link(image)
        return None

    def get_image_from_link(self, image):
        if image:
            req = Http.get(image, headers=self.headers, cookies=self.cookies)
            if req and req.is_success:
                return req.content
        return None

    def get_image_blob_from_link(self, image):
        if image and self.should_fetch('image'):
            data = self.get_image_from_link(image)
            if data:
                try:
                    img = BytesIO(data)
                    img = Image.open(img)
                    img = img.convert('RGB')
                    width, height = img.size
                    if height > 1080 or width > 1920:
                        img.thumbnail((1920, 1080))
                    buffer = BytesIO()
                    img.save(buffer, format="JPEG")
                    data = buffer.getvalue()
                except Exception as ex:
                    print(f"Could not decode image for evaluation: '{image}'.  Error: ", ex)
                return base64.b64encode(data).decode('utf-8')
        return None

    @staticmethod
    def duration_to_seconds(time_text):
        duration = ''
        if ":" in time_text:
            time_text = time_text.split(":")
            time_text = [i for i in time_text if i]
            if len(time_text) == 3 and int(time_text[0]) < 10:
                duration = str((int(time_text[0]) * 3600) + (int(time_text[1]) * 60) + int(time_text[2]))
            elif len(time_text) == 3 and int(time_text[0]) >= 10:
                duration = (int(time_text[1]) * 60) + int(time_text[2])
            elif len(time_text) == 2:
                duration = str(int(time_text[0]) * 60 + int(time_text[1]))
            elif len(time_text) == 1:
                duration = time_text[0]
        elif re.search(r'(\d{1,2})M(\d{1,2})S', time_text):
            if "H" in time_text:
                duration = re.search(r'(\d{1,2})H(\d{1,2})M(\d{1,2})S', time_text)
                hours = int(duration.group(1)) * 3600
                minutes = int(duration.group(2)) * 60
                seconds = int(duration.group(3))
                duration = str(hours + minutes + seconds)
            else:
                duration = re.search(r'(\d{1,2})M(\d{1,2})S', time_text)
                minutes = int(duration.group(1)) * 60
                seconds = int(duration.group(2))
                duration = str(minutes + seconds)
        return duration

    def get_url(self, response):
        return response.url

    def get_id(self, response):
        sceneid = self.get_from_regex(response.url, 'external_id')
        if sceneid and "?nats" in sceneid:
            sceneid = re.search(r'(.*)\?nats', sceneid).group(1)
        return sceneid

    def get_site(self, response):
        return tldextract.extract(response.url).domain

    def get_network(self, response):
        return tldextract.extract(response.url).domain

    def get_parent(self, response):
        return tldextract.extract(response.url).domain

    def get_studio(self, response):
        if 'studio' in self.get_selector_map():
            return string.capwords(self.cleanup_text(self.get_element(response, 'studio', 're_studio')))
        return ''

    @staticmethod
    def process_xpath(response, selector: str):
        if selector.startswith('//') or selector.startswith('./'):
            return response.xpath(selector)

        if selector.startswith('/'):
            return response.dpath(selector)

        return response.css(selector)

    def format_link(self, response, link):
        return self.format_url(response.url, link)

    @staticmethod
    def format_url(base, path):
        if path.startswith('http'):
            return path

        if path.startswith('//'):
            return 'https:' + path

        new_url = urlparse(path)
        url = urlparse(base)
        url = url._replace(path=new_url.path, query=new_url.query)

        return furl(unquote(url.geturl())).url

    def get_next_page_url(self, base, page):
        return self.format_url(base, self.get_selector_map('pagination') % page)

    def get_from_regex(self, text, re_name):
        if re_name in self.regex and self.regex[re_name]:
            regexp, group, mod = self.get_regex(self.regex[re_name])

            r = regexp.search(text)
            if r:
                return r.group(group)
            return None

        return text

    @staticmethod
    def get_regex(regexp, group=1, mod=re.IGNORECASE):
        if isinstance(regexp, tuple):
            mod = regexp[2] if len(regexp) > 2 else mod
            group = regexp[1] if len(regexp) > 1 else group
            regexp = regexp[0]

        return regexp, group, mod

    @staticmethod
    def cleanup_text(text, trash_words=None):
        if trash_words is None:
            trash_words = []
        if isinstance(text, list):
            text = "".join(text)

        text = unidecode.unidecode(html.unescape(text))
        for trash in trash_words:
            text = text.replace(trash, '')
        text = text.strip()
        return text

    def cleanup_title(self, title):
        return string.capwords(self.cleanup_text(title, self.title_trash))

    def cleanup_description(self, description):
        return self.cleanup_text(description, self.description_trash)

    def cleanup_date(self, item_date):
        return self.cleanup_text(item_date, self.date_trash)

    def parse_date(self, item_date, date_formats=None):
        item_date = self.cleanup_date(item_date)
        settings = {'TIMEZONE': 'UTC'}

        return dateparser.parse(item_date, date_formats=date_formats, settings=settings)

    def check_item(self, item, days=None):
        if 'date' not in item:
            return item

        today = datetime.today().strftime('%Y-%m-%d')
        if item['date']:
            item_date = re.search(r'(\d{4}-\d{2}-\d{2})', item['date'])
            if item_date:
                item['date'] = item_date.group(1)

            if item['date'] > today:
                return None

            if days:
                if days > 9000:
                    filter_date = '0000-00-00'
                else:
                    filter_date = date.today() - timedelta(days)
                    filter_date = filter_date.strftime('%Y-%m-%d')

                if self.debug:
                    if not item['date'] > filter_date:
                        item['filtered'] = 'Scene filtered due to date restraint'
                    print(item)
                else:
                    if filter_date:
                        if item['date'] > filter_date:
                            return item
                        return None
        return item

    def get_element(self, response, selector, regex=None):
        selector = self.get_selector_map(selector)
        if selector:
            element = self.process_xpath(response, selector)
            if element:
                if (len(element) > 1 or regex == "list") and "/script" not in selector:
                    element = list(map(lambda x: x.strip(), element.getall()))
                else:
                    if isinstance(element, list):
                        element = element.getall()
                        element = " ".join(element)
                    else:
                        element = element.get()
                    element = self.get_from_regex(element, regex)
                    if element:
                        element = element.strip()
                if element:
                    if isinstance(element, list):
                        element = [i for i in element if i]
                    return element
        return ''

    def check_movie_cache(self, itemid, itemsite, itemtitle=None, itemdate=None, itemurl=None, itemsite_alt=None):

        #######################################################
        # Check to see if the movie has already been submitted.  If so, no reason to pull the images or scenes
        conn, cursor = db_conn()
        submitmovie = True
        hide_cache = self.settings.get('hide_cache')

        # Real quick we'll make our comparison fields.
        shortsite = re.sub('[^0-9a-z]', '', itemsite.lower())
        shorttitle = re.sub('[^0-9a-z]', '', itemtitle.lower())

        if itemdate:
            itemyear = re.search(r'(\d{4})', itemdate).group(1)
            yearlow = str(int(itemyear) - 1)
            yearhigh = str(int(itemyear) + 1)
        else:
            itemyear = None

        # First we check a Site/ID Combo
        cursor.execute("SELECT id FROM movies WHERE external_id = %s AND shortsite = %s", (itemid, shortsite))
        if cursor.rowcount:
            movieid = cursor.fetchone()[0]
            submitmovie = False
            if not hide_cache:
                print(f"Not submitting due to Site/ID Combo in cache:  ID# {movieid}  \"{itemtitle}\" for \"{itemsite}\"")

        # Next we check a Site/URL Combo
        if submitmovie and itemurl:
            cursor.execute("SELECT id FROM movies WHERE url = %s AND shortsite = %s", (itemurl, shortsite))
            if cursor.rowcount:
                movieid = cursor.fetchone()[0]
                submitmovie = False
                if not hide_cache:
                    print(f"Not submitting due to Site/URL Combo in cache:  ID# {movieid} \"{itemtitle}\" for \"{itemsite}\"")

        # Next we check a Site/Title Combo
        if submitmovie and shorttitle and not itemyear:
            cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite = %s", (shorttitle, shortsite))
            if cursor.rowcount:
                movieid = cursor.fetchone()[0]
                submitmovie = False
                if not hide_cache:
                    print(f"Not submitting due to Site/Title Combo in cache:  ID# {movieid} \"{itemtitle}\" for \"{itemsite}\"")

        # Next we check a Site/Title Combo with Year
        if submitmovie and shorttitle and itemyear:
            cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite = %s and year BETWEEN %s AND %s", (shorttitle, shortsite, yearlow, yearhigh))
            if cursor.rowcount:
                movieid = cursor.fetchone()[0]
                submitmovie = False
                if not hide_cache:
                    print(f"Not submitting due to Site/Title/Year Combo in cache:  ID# {movieid} \"{itemtitle}\" for \"{itemsite}\"")

        if submitmovie and itemsite_alt:
            shortsite = re.sub('[^0-9a-z]', '', itemsite_alt.lower())
            # First we check a Site/ID Combo
            cursor.execute("SELECT id FROM movies WHERE external_id = %s AND shortsite = %s", (itemid, shortsite))
            if cursor.rowcount:
                movieid = cursor.fetchone()[0]
                submitmovie = False
                if not hide_cache:
                    print(f"Not submitting due to Alt-Site/ID Combo in cache:  ID# {movieid}  \"{itemtitle}\" for \"{itemsite}\"")

            # Next we check a Site/URL Combo
            if submitmovie and itemurl:
                cursor.execute("SELECT id FROM movies WHERE url = %s AND shortsite = %s", (itemurl, shortsite))
                if cursor.rowcount:
                    movieid = cursor.fetchone()[0]
                    submitmovie = False
                    if not hide_cache:
                        print(f"Not submitting due to Alt-Site/URL Combo in cache:  ID# {movieid} \"{itemtitle}\" for \"{itemsite}\"")

            # Next we check a Site/Title Combo
            if submitmovie and shorttitle:
                cursor.execute("SELECT id FROM movies WHERE shorttitle = %s AND shortsite = %s", (shorttitle, shortsite))
                if cursor.rowcount:
                    movieid = cursor.fetchone()[0]
                    submitmovie = False
                    if not hide_cache:
                        print(f"Not submitting due to Alt-Site/Title Combo in cache:  ID# {movieid} \"{itemtitle}\" for \"{itemsite}\"")

        cursor.close()
        conn.close()
        return submitmovie
