from scrapy.http import TextResponse

from tpdb.helpers.scrapy_dpath import DPathResponse


class DPathMiddleware(object):
    def process_response(self, request, response):
        # Anything that isn't text (images, PDFs, octet-stream, or a response that
        # arrived with no Content-Type at all) gets the base Response class from
        # Scrapy and has nothing for dpath to walk.  It still has to be handed back
        # untouched: returning None here fails the request with _InvalidOutput.
        if not isinstance(response, TextResponse):
            return response

        return DPathResponse(request, response)
