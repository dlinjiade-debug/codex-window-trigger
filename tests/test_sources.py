import unittest
from urllib.error import HTTPError
from urllib.request import Request

from codex_window_trigger.sources import SOURCE_URLS, _RejectRedirects, fetch_json, fetch_sources


class FakeHeaders:
    def __init__(self, content_type="application/json"):
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type


class FakeResponse:
    def __init__(
        self,
        body,
        *,
        url="https://codex-reset.com/api/forecast",
        content_type="application/json",
        status=200,
    ):
        self.body = body
        self.url = url
        self.headers = FakeHeaders(content_type)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body[:limit]


class FetchJsonTest(unittest.TestCase):
    def test_accepts_small_json_from_allowed_https_url(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{"ok": true}')
        self.assertEqual({"ok": True}, fetch_json(SOURCE_URLS["forecast"], opener=opener))

    def test_rejects_url_not_exactly_in_source_allowlist(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{}')
        for url in (
            "http://codex-reset.com/api/forecast",
            "https://codex-reset.com:443/api/forecast",
            "https://user@codex-reset.com/api/forecast",
            "https://codex-reset.com/api/forecast?view=full",
            "https://codex-reset.com/api/other",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "allowlist"):
                fetch_json(url, opener=opener)

    def test_rejects_non_2xx_status(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{}', status=404)
        with self.assertRaisesRegex(ValueError, "status"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_non_json_content_type(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{}', content_type="text/html")
        with self.assertRaisesRegex(ValueError, "not JSON"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_body_larger_than_one_megabyte(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'x' * 1_048_577)
        with self.assertRaisesRegex(ValueError, "size limit"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_redirect_to_another_host(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{}', url="https://example.com/payload")
        with self.assertRaisesRegex(ValueError, "redirect"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_invalid_utf8(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{"value": "\xff"}')
        with self.assertRaises(UnicodeDecodeError):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_non_finite_json_numbers(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'{"value": NaN}')
        with self.assertRaisesRegex(ValueError, "finite"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_rejects_non_object_json(self):
        opener = lambda *_args, **_kwargs: FakeResponse(b'[]')
        with self.assertRaisesRegex(ValueError, "object"):
            fetch_json(SOURCE_URLS["forecast"], opener=opener)

    def test_default_redirect_handler_refuses_before_following(self):
        handler = _RejectRedirects()
        with self.assertRaises(HTTPError) as caught:
            handler.http_error_302(
                request=Request(SOURCE_URLS["forecast"]),
                fp=None,
                code=302,
                msg="Found",
                headers=FakeHeaders(),
            )
        caught.exception.close()

    def test_fetch_sources_uses_exact_three_urls(self):
        called = []

        def fetcher(url):
            called.append(url)
            return {"url": url}

        result = fetch_sources(fetcher=fetcher)
        self.assertEqual(list(SOURCE_URLS.values()), called)
        self.assertEqual(3, len(result))
