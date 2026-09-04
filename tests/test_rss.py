"""RSS fetcher tests — fully offline (httpx is mocked)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.news.rss import fetch_feed  # noqa: E402

RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>RBI cuts repo rate to 6.25 percent</title>
<link>https://example.com/a1</link>
<description>The Monetary Policy Committee voted to cut.</description>
</item></channel></rss>"""


class FakeResponse:
    def __init__(self, status_code=200, content=RSS_XML):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _source():
    return {"url": "https://example.com/feed", "name": "Test",
            "country": "IN", "category": "national",
            "reliability_score": 0.8}


class TestFetchFeed(unittest.TestCase):
    def test_success(self):
        with mock.patch("app.news.rss.httpx.get",
                        return_value=FakeResponse()) as g:
            rows = fetch_feed(_source())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "RBI cuts repo rate to 6.25 percent")
        self.assertEqual(rows[0]["url"], "https://example.com/a1")
        self.assertEqual(rows[0]["source"], "Test")
        g.assert_called_once()

    def test_transient_connect_error_retried(self):
        """One Errno-8-style blip must not kill the feed: retry succeeds."""
        good = FakeResponse()
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError(
                    "[Errno 8] nodename nor servname provided")
            return good

        with mock.patch("app.news.rss.httpx.get", side_effect=flaky), \
             mock.patch("app.news.rss.time.sleep"):
            rows = fetch_feed(_source())
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(rows), 1)

    def test_persistent_connect_error_falls_back_to_no_proxy(self):
        """If direct attempts keep failing, the fetcher retries with
        trust_env=False (proxy env bypass) instead of giving up."""
        calls = {"get": 0, "client": 0}

        def always_fail(*a, **kw):
            calls["get"] += 1
            raise httpx.ConnectError(
                "[Errno 8] nodename nor servname provided")

        fake_client = mock.MagicMock()
        fake_client.__enter__ = lambda s: fake_client
        fake_client.__exit__ = mock.Mock(return_value=False)
        fake_client.get.return_value = FakeResponse()

        with mock.patch("app.news.rss.httpx.get", side_effect=always_fail), \
             mock.patch("app.news.rss.httpx.Client", return_value=fake_client), \
             mock.patch("app.news.rss.time.sleep"):
            rows = fetch_feed(_source())
        # two direct attempts (original + retry), then proxy-bypass client
        self.assertEqual(calls["get"], 2)
        self.assertEqual(len(rows), 1)

    def test_403_retries_with_browser_ua(self):
        responses = [FakeResponse(status_code=403), FakeResponse()]

        def sequence(*a, **kw):
            return responses.pop(0)

        with mock.patch("app.news.rss.httpx.get", side_effect=sequence):
            rows = fetch_feed(_source())
        self.assertEqual(len(rows), 1)

    def test_persistent_failure_returns_empty(self):
        def fail(*a, **kw):
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided")

        fake_client = mock.MagicMock()
        fake_client.__enter__ = lambda s: fake_client
        fake_client.__exit__ = mock.Mock(return_value=False)
        fake_client.get.side_effect = fail

        with mock.patch("app.news.rss.httpx.get", side_effect=fail), \
             mock.patch("app.news.rss.httpx.Client", return_value=fake_client), \
             mock.patch("app.news.rss.time.sleep"):
            rows = fetch_feed(_source())
        self.assertEqual(rows, [])

    def test_http_error_returns_empty(self):
        with mock.patch("app.news.rss.httpx.get",
                        return_value=FakeResponse(status_code=404)):
            rows = fetch_feed(_source())
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
