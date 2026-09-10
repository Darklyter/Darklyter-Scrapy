class CustomProxyMiddleware(object):
    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        instance.crawler = crawler
        instance.proxy_address = crawler.settings.get('PROXY_ADDRESS')
        instance.use_proxy = crawler.settings.get('USE_PROXY')
        return instance

    def process_request(self, request):
        if 'proxy' not in request.meta:
            if self.use_proxy:
                request.meta['proxy'] = self.proxy_address
            else:
                request.meta['proxy'] = ''
