# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html

import hashlib
import re
import json
import logging
import time

from pathlib import Path
from datetime import datetime

from pymongo import MongoClient
from scrapy.exporters import JsonItemExporter, JsonLinesItemExporter

from tpdb.helpers.http import Http

class StashScenePipeline:
    def __init__(self, crawler):
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

    async def process_item(self, item, spider):
        if spider.debug is True:
            return item

        # ~ if self.crawler.settings['FILTER_TAGS']:
            # ~ item['tags'] = self.clean_tags(item['tags'], self.tagaliases)

        if item['date']:
            item['date'] = re.search(r'(\d{4}-\d{2}-\d{2})', item['date']).group(1)

        if "duration" not in item:
            item['duration'] = ''
        if "type" not in item:
            item['type'] = 'Scene'

        item['title'] = item['title'].replace("&amp;", "&")
        # ~ item['description'] = item['description'].replace("&amp;", "&")

        payload = {
            'id': item['id'],
            'date': item['date'],
            'title': item['title'],
            # ~ 'description': item['description'],
            'duration': item['duration'],
            'code': item['code'],
            'url': item['url'],
            'external_id': item['code'],
            'site': item['site'],
            # ~ 'parent': item['parent'],
            # ~ 'performers': item['performers'],
            'phashes': item['phashes'],
            'oshashes': item['oshashes'],
            # ~ 'tags': item['tags'],
            # ~ 'type': item['type'],
        }

        # Post the scene to the API - requires auth with permissions
        disp_result = ""

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

            logging.info(f"Item: {item['title'][0:50]}" + " " * title_length + f"{item['site'][0:15]}" + " " * site_length + f"\t{str(item['id'])[0:10]}\t{disp_date}\t{item['url'][0:30]}\t{disp_result}")

        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            item2 = item.copy()
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

    def close_spider(self, spider):
        if spider.settings.getbool('export') or self.crawler.settings['EXPORT_ITEMS']:
            self.fp.write(']}'.encode())
            self.fp.close()

