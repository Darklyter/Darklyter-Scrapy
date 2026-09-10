# Define here the models for your spider middleware
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/spider-middleware.html
import re

import scrapy

from pymongo import MongoClient
from scrapy import signals
from scrapy.exceptions import IgnoreRequest


class TpdbSceneDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()

        cls.crawler = crawler

        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            cls.db = db['scrapy']

        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request):
        spider = self.crawler.spider
        if re.search(spider.get_selector_map('external_id'), request.url) is None:
            return None

        if spider.force is True:
            return None

        # Used in production - we store the scene in MongoDB for caching reasons
        if self.crawler.settings['ENABLE_MONGODB']:
            result = self.db.scenes.find_one({'url': request.url})
            if result is not None and ('api_response' not in result or not result['api_response']):
                raise scrapy.exceptions.IgnoreRequest

        return None

    def process_response(self, request, response):
        return response

    def process_exception(self, request, exception):
        pass

    def spider_opened(self, spider):
        spider.logger.info('Spider opened: %s' % spider.name)

class TpdbMovieDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()

        cls.crawler = crawler

        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            cls.db = db['scrapy']

        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request):
        spider = self.crawler.spider

        if spider.force is True:
            return None

        # Used in production - we store the scene in MongoDB for caching reasons
        if self.crawler.settings['ENABLE_MONGODB']:
            result = self.db.scenes.find_one({'url': request.url})
            if result is not None and ('api_response' not in result or not result['api_response']):
                raise scrapy.exceptions.IgnoreRequest

        return None

    def process_response(self, request, response):
        return response

    def process_exception(self, request, exception):
        pass

    def spider_opened(self, spider):
        spider.logger.info('Spider opened: %s' % spider.name)


class TpdbPerformerDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.
    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()

        cls.crawler = crawler

        if crawler.settings['ENABLE_MONGODB']:
            db = MongoClient(crawler.settings['MONGODB_URL'])
            cls.db = db['scrapy']

        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request):
        spider = self.crawler.spider
        if re.search(spider.get_selector_map('external_id'), request.url) is None:
            return None

        if spider.force is True:
            return None

        # Used in production - we store the scene in MongoDB for caching reasons
        if self.crawler.settings['ENABLE_MONGODB']:
            result = self.db.performers.find_one({'url': request.url})
            if result is not None and ('api_response' not in result or not result['api_response']):
                raise scrapy.exceptions.IgnoreRequest

        return None

    def process_response(self, request, response):
        return response

    def process_exception(self, request, exception):
        pass

    def spider_opened(self, spider):
        spider.logger.info('Spider opened: %s' % spider.name)
