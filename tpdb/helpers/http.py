import logging
import warnings
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import httpx
from httpx import Cookies, Response
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)


CookieDict = Dict[str, str]
CookiePairs = Iterable[Tuple[str, str]]
CookieExportList = List[Dict[str, Any]]
CookiesInput = Union[None, Cookies, CookieDict, CookiePairs, CookieExportList]


class Http:
    """
    Small httpx wrapper with:
      - sane logging defaults
      - verify defaults to False unless explicitly passed
      - cookie normalization to support:
          * dict: {"name": "value"}
          * list of pairs: [("name", "value")]
          * httpx.Cookies
          * "exported" list of dict cookies: [{"name": "...", "value": "...", ...}, ...]
    """

    @staticmethod
    def _quiet_httpx_logging() -> None:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    @staticmethod
    def _normalize_cookies(cookies: CookiesInput) -> Optional[Union[CookieDict, CookiePairs, Cookies]]:
        """
        Normalize cookies into a format httpx accepts.

        Accepts:
          - None
          - httpx.Cookies
          - dict of name->value
          - iterable of (name, value)
          - list of dict exports: [{"name": "...", "value": "...", ...}, ...]
        """
        if cookies is None:
            return None

        # Already an httpx Cookies instance
        if isinstance(cookies, Cookies):
            return cookies

        # Plain dict: {"cookie_name": "cookie_value"}
        if isinstance(cookies, dict):
            # Ensure values are str (httpx is tolerant, but keep it clean)
            return {str(k): str(v) for k, v in cookies.items()}

        # Exported cookies: [{"name": "...", "value": "...", ...}]
        if isinstance(cookies, list) and (not cookies or isinstance(cookies[0], dict)):
            out: Dict[str, str] = {}
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                value = c.get("value")
                if name is None or value is None:
                    continue
                out[str(name)] = str(value)
            return out

        # Otherwise assume it's already an iterable of (name, value) pairs.
        # If it's not, httpx will raise, which we'll log/catch upstream.
        return cookies

    @staticmethod
    def request(method: str, url: str, **kwargs) -> Optional[Response]:
        Http._quiet_httpx_logging()

        # Use verify from kwargs if passed, else default to False
        verify = kwargs.pop("verify", False)

        # NEW: bump default timeout from httpx's 5s to something more forgiving
        # (callers can still override by passing timeout=...)
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=15.0))

        # Normalize cookies if provided
        if "cookies" in kwargs:
            kwargs["cookies"] = Http._normalize_cookies(kwargs.get("cookies"))

        try:
            req = httpx.request(method, url, verify=verify, **kwargs)
            return req
        except Exception as e:
            logging.error(e)
            return None

    @staticmethod
    def get(url: str, **kwargs) -> Optional[Response]:
        Http._quiet_httpx_logging()
        return Http.request("GET", url, **kwargs)

    @staticmethod
    def post(url: str, **kwargs) -> Optional[Response]:
        Http._quiet_httpx_logging()
        return Http.request("POST", url, **kwargs)

    @staticmethod
    def head(url: str, **kwargs) -> Optional[Response]:
        Http._quiet_httpx_logging()
        return Http.request("HEAD", url, **kwargs)

    @staticmethod
    async def request_async(method: str, url: str, **kwargs) -> Optional[Response]:
        """Async twin of :meth:`request`.

        Item pipelines run inside Scrapy's asyncio reactor, so a blocking POST there
        stalls every download for the length of the timeout.  Awaiting this instead
        lets the crawl keep moving while the API call is in flight.
        """
        Http._quiet_httpx_logging()

        verify = kwargs.pop("verify", False)
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=15.0))

        if "cookies" in kwargs:
            kwargs["cookies"] = Http._normalize_cookies(kwargs.get("cookies"))

        try:
            async with httpx.AsyncClient(verify=verify) as client:
                return await client.request(method, url, **kwargs)
        except Exception as e:
            logging.error(e)
            return None

    @staticmethod
    async def get_async(url: str, **kwargs) -> Optional[Response]:
        return await Http.request_async("GET", url, **kwargs)

    @staticmethod
    async def post_async(url: str, **kwargs) -> Optional[Response]:
        return await Http.request_async("POST", url, **kwargs)

    @staticmethod
    def fake_response(
        url: str,
        status_code: int,
        content: Union[str, bytes],
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Union[Dict[str, str], CookieExportList]] = None,
    ) -> Response:
        """
        Create a Response-like object for testing/caching layers.

        ``Response.url`` and ``Response.cookies`` are read-only properties in httpx,
        so everything has to go in through the constructor.  ``url`` comes back out
        of the request we build here, and the cookie jar is filled through the public
        ``Cookies.set`` API rather than being replaced wholesale.
        """
        content_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
        headers = {} if headers is None else headers

        # Normalize cookies into a name->value dict, then load into httpx.Cookies
        normalized = Http._normalize_cookies(cookies)
        cookie_dict: Dict[str, str] = {}
        if isinstance(normalized, dict):
            cookie_dict = normalized
        elif normalized is None:
            cookie_dict = {}
        else:
            # If someone passes pairs here, convert to dict
            try:
                cookie_dict = {str(k): str(v) for (k, v) in normalized}  # type: ignore[misc]
            except Exception:
                cookie_dict = {}

        # response.url is derived from the request, so building the request with the
        # right url is what makes response.url correct.
        response = Response(
            status_code=status_code,
            content=content_bytes,
            headers=headers,
            request=httpx.Request("GET", str(url)),
        )

        if cookie_dict:
            jar = response.cookies
            for name, value in cookie_dict.items():
                jar.set(name, value)

        return response
