# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class SceneItem(scrapy.Item):
    title = scrapy.Field()
    description = scrapy.Field()
    site = scrapy.Field()
    date = scrapy.Field()
    image = scrapy.Field()
    image_blob = scrapy.Field()
    performers = scrapy.Field()
    tags = scrapy.Field()
    url = scrapy.Field()
    id = scrapy.Field()
    trailer = scrapy.Field()
    parent = scrapy.Field()
    network = scrapy.Field()
    file_urls = scrapy.Field()
    gallery_urls = scrapy.Field()
    file_folder = scrapy.Field()
    file_filename = scrapy.Field()
    gallery_filename = scrapy.Field()
    full_path = scrapy.Field()
    shortdate = scrapy.Field()
    cookies = scrapy.Field()
    headers = scrapy.Field()
    displayString = scrapy.Field()
    payload = scrapy.Field()
    body = scrapy.Field()
    resolution = scrapy.Field()
