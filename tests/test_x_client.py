"""X client integration tests — fully offline.

No test ever reaches the real X API: the HTTP transport is faked, and
dry-run / AUTO_POST guards are exercised with the real run_cycle code.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CONFIG
from app.database import Database
from app.main import run_cycle
from app.x.client import (ME_URL, POST_URL, XClient, _sign,
                          _signature_base, XResult, validate_payload)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeHTTP:
    """Records requests; never touches the network."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.calls = []
        self.error = error

    def request(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers})
        if self.error:
            raise self.error
        return self.responses.get(url, FakeResponse(200, {}))


def make_client(**kw):
    http = kw.pop("http", FakeHTTP())
    return XClient("k" * 25, "s" * 50, "t" * 50, "ts" * 45,
                   http_client=http), http


class TestConfiguration(unittest.TestCase):
    def test_missing_credentials(self):
        c = XClient("", "", "", "")
        self.assertFalse(c.configured)
        self.assertEqual(c.missing_fields(), [
            "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
            "X_ACCESS_TOKEN_SECRET"])

    def test_partial_credentials(self):
        c = XClient("key", "", "", "")
        self.assertFalse(c.configured)
        self.assertEqual(c.missing_fields(),
                         ["X_API_SECRET", "X_ACCESS_TOKEN",
                          "X_ACCESS_TOKEN_SECRET"])

    def test_whitespace_credentials_treated_as_missing(self):
        c = XClient("  ", "s", "t", "ts")
        self.assertFalse(c.configured)
        self.assertEqual(c.missing_fields(), ["X_API_KEY"])

    def test_unconfigured_client_never_calls_http(self):
        http = FakeHTTP()
        c = XClient("", "", "", "", http_client=http)
        r = c.post_tweet("some valid tweet text")
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith("not_configured"))
        r2 = c.verify_me()
        self.assertFalse(r2.ok)
        self.assertEqual(http.calls, [])  # zero network attempts

    def test_full_configuration(self):
        c, _ = make_client()
        self.assertTrue(c.configured)
        self.assertEqual(c.missing_fields(), [])

    def test_credential_status_booleans_only(self):
        c, _ = make_client()
        status = c.credential_status()
        self.assertEqual(status, {"X_API_KEY": True, "X_API_SECRET": True,
                                  "X_ACCESS_TOKEN": True,
                                  "X_ACCESS_TOKEN_SECRET": True})
        # partial
        c2 = XClient("k", "", "", "")
        self.assertEqual(c2.credential_status(), {
            "X_API_KEY": True, "X_API_SECRET": False,
            "X_ACCESS_TOKEN": False, "X_ACCESS_TOKEN_SECRET": False})
        # status values are strictly booleans — never credential content
        self.assertTrue(all(isinstance(v, bool) for v in status.values()))


class TestAuth(unittest.TestCase):
    def test_oauth_signature_is_base64_rfc5849(self):
        """Signature must be Base64 HMAC-SHA1 (OAuth 1.0a), not hex.

        Regression test: the client previously emitted hexdigest(), which
        X rejects with 401 auth_failed."""
        import base64
        import hashlib
        import hmac as hmac_mod
        import urllib.parse

        params = {
            "oauth_consumer_key": "consumer",
            "oauth_nonce": "nonce123",
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": "1700000000",
            "oauth_token": "token",
            "oauth_version": "1.0",
        }
        sig = _sign("GET", "https://api.x.com/2/users/me", params,
                    "csecret", "tsecret")
        # must be valid base64, decodable to a 20-byte SHA1 digest
        decoded = base64.b64decode(sig, validate=True)
        self.assertEqual(len(decoded), 20)
        self.assertNotIn("-", sig[1:-1])  # hex would use no +/= padding like this

        # independent reference implementation (RFC 5849 §3.4)
        enc = lambda s: urllib.parse.quote(str(s), safe="")
        param_str = "&".join(
            "%s=%s" % (enc(k), enc(v)) for k, v in sorted(params.items()))
        base_string = "&".join(["GET",
                                enc("https://api.x.com/2/users/me"),
                                enc(param_str)])
        key = (enc("csecret") + "&" + enc("tsecret")).encode()
        expected = base64.b64encode(
            hmac_mod.new(key, base_string.encode(), hashlib.sha1).digest()
        ).decode("ascii")
        self.assertEqual(sig, expected)

    def test_oauth_header_structure(self):
        c, _ = make_client()
        h = c._oauth_header("POST", POST_URL, {"text": "hello"})
        self.assertTrue(h.startswith("OAuth "))
        for part in ("oauth_consumer_key", "oauth_nonce",
                     "oauth_signature_method=\"HMAC-SHA1\"",
                     "oauth_timestamp", "oauth_token", "oauth_version=\"1.0\"",
                     "oauth_signature"):
            self.assertIn(part, h)

    def test_oauth_nonce_unique(self):
        c, _ = make_client()
        n1 = c._oauth_header("GET", ME_URL)
        n2 = c._oauth_header("GET", ME_URL)
        self.assertNotEqual(n1, n2)

    def test_verify_me_ok(self):
        http = FakeHTTP({ME_URL: FakeResponse(200, {
            "data": {"id": "1", "username": "testuser", "name": "Test"}})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.verify_me()
        self.assertTrue(r.ok)
        self.assertEqual(r.data["username"], "testuser")
        self.assertEqual(http.calls[0]["method"], "GET")
        self.assertEqual(http.calls[0]["url"], ME_URL)

    def test_verify_me_auth_failure(self):
        http = FakeHTTP({ME_URL: FakeResponse(401, {"errors": []})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.verify_me()
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "auth_failed")


class TestPayloadValidation(unittest.TestCase):
    def test_valid(self):
        self.assertIsNone(validate_payload("A perfectly good tweet text"))

    def test_empty_rejected(self):
        self.assertEqual(validate_payload(""), "empty_text")
        self.assertEqual(validate_payload("   "), "empty_text")

    def test_short_rejected(self):
        self.assertEqual(validate_payload("too short"), "text_too_short")

    def test_over_limit_rejected(self):
        self.assertEqual(validate_payload("x" * 300), "text_too_long:300")


class TestPostTweet(unittest.TestCase):
    def test_success(self):
        http = FakeHTTP({POST_URL: FakeResponse(
            201, {"data": {"id": "12345", "text": "hi"}})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["id"], "12345")
        call = http.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["json"], {"text": "A valid tweet text for testing"})
        self.assertIn("Authorization", call["headers"])
        self.assertTrue(call["headers"]["Authorization"].startswith("OAuth "))

    def test_invalid_payload_never_sends(self):
        http = FakeHTTP()
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("short")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "text_too_short")
        self.assertEqual(http.calls, [])

    def test_rate_limited(self):
        http = FakeHTTP({POST_URL: FakeResponse(
            429, {"errors": []}, headers={"retry-after": "900"})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "rate_limited")
        self.assertEqual(r.retry_after, 900)

    def test_forbidden(self):
        http = FakeHTTP({POST_URL: FakeResponse(403, {"errors": []})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "forbidden")

    def test_auth_failed(self):
        http = FakeHTTP({POST_URL: FakeResponse(401, {"errors": []})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertEqual(r.error, "auth_failed")

    def test_network_error(self):
        http = FakeHTTP(error=ConnectionError("boom"))
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "network_error")

    def test_result_repr_contains_no_secrets(self):
        http = FakeHTTP({POST_URL: FakeResponse(403, {"errors": []})})
        c = XClient("SECRETK", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertNotIn("SECRETK", repr(r))


class TestOAuthSignatureVectors(unittest.TestCase):
    """Exact known-vector tests for OAuth 1.0a signing.

    Vector: the documented Twitter/X "Creating a signature" example
    (POST statuses/update with a form-encoded body)."""

    VECTOR_URL = "https://api.twitter.com/1.1/statuses/update.json"
    VECTOR_PARAMS = {
        "include_entities": "true",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    }
    CONSUMER_SECRET = "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw"
    TOKEN_SECRET = "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE"

    EXPECTED_BASE = (
        "POST&https%3A%2F%2Fapi.twitter.com%2F1.1%2Fstatuses%2Fupdate.json"
        "&include_entities%3Dtrue"
        "%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog"
        "%26oauth_nonce%3DkYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg"
        "%26oauth_signature_method%3DHMAC-SHA1"
        "%26oauth_timestamp%3D1318622958"
        "%26oauth_token%3D370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb"
        "%26oauth_version%3D1.0"
        "%26status%3DHello%2520Ladies%2520%252B%2520Gentlemen%252C%2520a"
        "%2520signed%2520OAuth%2520request%2521")
    EXPECTED_SIGNATURE = "hCtSmYh+iHYCEqBWrE7C7hYmtUk="

    def test_signature_base_string_exact_vector(self):
        base = _signature_base("POST", self.VECTOR_URL, self.VECTOR_PARAMS)
        self.assertEqual(base, self.EXPECTED_BASE)

    def test_signature_exact_vector(self):
        sig = _sign("POST", self.VECTOR_URL, self.VECTOR_PARAMS,
                    self.CONSUMER_SECRET, self.TOKEN_SECRET)
        self.assertEqual(sig, self.EXPECTED_SIGNATURE)

    def test_json_body_excluded_from_signature(self):
        """X API v2 tweet posts send a JSON body. RFC 5849 §3.4.1.3
        includes only form-encoded body parameters in the base string —
        a JSON body MUST be excluded, or X rejects every post with 401.

        Regression test: the client previously signed the JSON body
        into the base string."""
        import urllib.parse
        c = XClient("k" * 25, "s" * 50, "t" * 50, "ts" * 45)
        nonce = mock.Mock()
        nonce.hex = "fixednonce123"
        with mock.patch("app.x.client.uuid.uuid4",
                        return_value=nonce), \
             mock.patch("app.x.client.time.time",
                        return_value=1318622958):
            header = c._oauth_header("POST", POST_URL,
                                     {"text": "A valid tweet body"})
        # the tweet text must not leak into the signed header at all
        self.assertNotIn("A valid tweet body", header)
        # the signature must equal one computed over oauth params ONLY
        oauth_only = {
            "oauth_consumer_key": "k" * 25,
            "oauth_nonce": "fixednonce123",
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": "1318622958",
            "oauth_token": "t" * 50,
            "oauth_version": "1.0",
        }
        expected_sig = _sign("POST", POST_URL, oauth_only,
                             "s" * 50, "ts" * 45)
        # RFC 5849 §3.5.1: header parameter values are percent-encoded —
        # the Base64 signature's '=' padding appears as %3D (and '+' as
        # %2B, '/' as %2F), so the raw base64 string is never in the
        # header as-is
        enc = lambda s: urllib.parse.quote(str(s), safe="")
        self.assertIn('oauth_signature="%s"' % enc(expected_sig), header)
        self.assertNotIn('oauth_signature="%s"' % expected_sig, header)
        # the complete header matches the RFC 5849 §3.5.1 construction:
        # comma-separated name="value" pairs, names and values
        # percent-encoded, sorted by name
        expected_header = "OAuth " + ", ".join(
            '%s="%s"' % (enc(k), enc(v))
            for k, v in sorted(dict(oauth_only,
                                    oauth_signature=expected_sig).items()))
        self.assertEqual(header, expected_header)


class TestLivePosting(unittest.TestCase):
    """run_cycle live-path behavior: success marks the story tweeted;
    transient failures retry with backoff and stay retryable; auth
    failures are never blindly retried. Fully offline (mocked client)."""

    def _db(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def _article_row(self, db):
        aid = db.insert_article({
            "url": "https://x.com/1", "normalized_url": "https://x.com/1",
            "url_hash": "h1", "title": "RBI keeps repo rate unchanged",
            "summary": "The MPC voted to hold.", "source": "The Hindu",
            "country": "IN", "reliability": 0.9})
        db.update_scores(aid, 0.8, 0.6, aid, "new")
        return db.query_one("SELECT * FROM articles WHERE id=?", (aid,))

    def _cycle(self, db, article, post_results):
        """Run one live cycle with a mocked XClient whose post_tweet
        returns the given results in order. Returns the mock."""
        ok_result = XResult(ok=True, data={"id": "tw1"}, status=201)
        results = list(post_results) + [ok_result]
        with mock.patch("app.main.collect_and_process",
                        return_value={}), \
             mock.patch("app.main.select_stories",
                        return_value=[dict(article)]), \
             mock.patch("app.main.check_rate_limits",
                        return_value=(True, "")), \
             mock.patch("app.main.time.sleep"), \
             mock.patch.object(CONFIG, "BOT_ENABLED", True), \
             mock.patch.object(CONFIG, "AUTO_POST", True), \
             mock.patch("app.main.XClient") as xc:
            xc.return_value.configured = True
            xc.return_value.post_tweet.side_effect = results
            run_results = run_cycle(db, dry_run=False, force=False)
        return xc, run_results

    def test_successful_post_marks_story_tweeted(self):
        db = self._db()
        article = self._article_row(db)
        xc, results = self._cycle(db, article, [])
        self.assertEqual(results[0][2], "posted")
        self.assertEqual(xc.return_value.post_tweet.call_count, 1)
        row = db.query_one("SELECT * FROM articles WHERE id=?",
                           (article["id"],))
        self.assertEqual(row["status"], "tweeted")
        tweet = db.query_one("SELECT * FROM tweets ORDER BY id DESC")
        self.assertEqual(tweet["status"], "posted")
        self.assertEqual(tweet["tweet_id"], "tw1")

    def test_transient_failure_retried_then_succeeds(self):
        db = self._db()
        article = self._article_row(db)
        transient = [XResult(error="network_error"),
                     XResult(error="network_error")]
        xc, results = self._cycle(db, article, transient)
        # two failures then success: exactly 3 attempts, no sleep on the
        # final attempt
        self.assertEqual(xc.return_value.post_tweet.call_count, 3)
        self.assertEqual(results[0][2], "posted")
        row = db.query_one("SELECT * FROM articles WHERE id=?",
                           (article["id"],))
        self.assertEqual(row["status"], "tweeted")

    def test_transient_failure_exhausted_story_stays_retryable(self):
        db = self._db()
        article = self._article_row(db)
        transient = [XResult(error="network_error")] * 3
        xc, results = self._cycle(db, article, transient)
        # bounded: never more than 3 attempts
        self.assertEqual(xc.return_value.post_tweet.call_count, 3)
        self.assertEqual(results, [])
        # the article was NOT marked tweeted and the story is still
        # selectable next cycle (failed tweet rows never block it)
        row = db.query_one("SELECT * FROM articles WHERE id=?",
                           (article["id"],))
        self.assertEqual(row["status"], "new")
        from app.news.collector import select_stories
        self.assertEqual(len(select_stories(db)), 1)

    def test_auth_failure_not_retried_and_stays_retryable(self):
        db = self._db()
        article = self._article_row(db)
        auth = [XResult(error="auth_failed", status=401)]
        xc, results = self._cycle(db, article, auth)
        # 401 is permanent for this cycle: exactly one attempt
        self.assertEqual(xc.return_value.post_tweet.call_count, 1)
        self.assertEqual(results, [])
        row = db.query_one("SELECT * FROM articles WHERE id=?",
                           (article["id"],))
        self.assertEqual(row["status"], "new")
        from app.news.collector import select_stories
        self.assertEqual(len(select_stories(db)), 1)

    def test_rate_limited_not_retried(self):
        db = self._db()
        article = self._article_row(db)
        rl = [XResult(error="rate_limited", status=429, retry_after=900)]
        xc, results = self._cycle(db, article, rl)
        self.assertEqual(xc.return_value.post_tweet.call_count, 1)
        self.assertEqual(results, [])


class TestPendingPosts(unittest.TestCase):
    """HTTP 402 (X API credits exhausted): the validated tweet is queued
    as a pending post, the story is never marked tweeted, and no
    duplicate pending post is created. Fully offline."""

    def _db(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def _article_row(self, db):
        aid = db.insert_article({
            "url": "https://x.com/1", "normalized_url": "https://x.com/1",
            "url_hash": "h1", "title": "RBI keeps repo rate unchanged",
            "summary": "The MPC voted to hold.", "source": "The Hindu",
            "country": "IN", "reliability": 0.9})
        db.update_scores(aid, 0.8, 0.6, aid, "new")
        return db.query_one("SELECT * FROM articles WHERE id=?", (aid,))

    def _cycle_402(self, db, article):
        result_402 = XResult(error="payment_required", status=402)
        with mock.patch("app.main.collect_and_process",
                        return_value={}), \
             mock.patch("app.main.select_stories",
                        return_value=[dict(article)]), \
             mock.patch("app.main.check_rate_limits",
                        return_value=(True, "")), \
             mock.patch("app.main.time.sleep"), \
             mock.patch.object(CONFIG, "BOT_ENABLED", True), \
             mock.patch.object(CONFIG, "AUTO_POST", True), \
             mock.patch("app.main.XClient") as xc:
            xc.return_value.configured = True
            xc.return_value.post_tweet.return_value = result_402
            run_results = run_cycle(db, dry_run=False, force=False)
        return xc, run_results

    def test_client_maps_402_to_payment_required(self):
        http = FakeHTTP({POST_URL: FakeResponse(402, {"errors": []})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.post_tweet("A valid tweet text for testing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "payment_required")
        self.assertEqual(r.status, 402)

    def test_402_queues_pending_post_and_never_marks_tweeted(self):
        db = self._db()
        article = self._article_row(db)
        xc, results = self._cycle_402(db, article)
        # one attempt only — 402 is never retried
        self.assertEqual(xc.return_value.post_tweet.call_count, 1)
        self.assertEqual(results, [])
        # the story was NOT marked tweeted
        row = db.query_one("SELECT * FROM articles WHERE id=?",
                           (article["id"],))
        self.assertNotEqual(row["status"], "tweeted")
        self.assertEqual(row["status"], "pending_post")
        # the validated tweet was saved with source, url, status=pending
        pending = db.pending_posts()
        self.assertEqual(len(pending), 1)
        p = pending[0]
        self.assertEqual(p["status"], "pending")
        self.assertEqual(p["source"], "The Hindu")
        self.assertEqual(p["article_url"], "https://x.com/1")
        self.assertIn("RBI keeps repo rate unchanged", p["tweet_text"])
        self.assertTrue(p["created_at"])
        # the X attempt is recorded as failed, not posted
        tweet = db.query_one("SELECT * FROM tweets ORDER BY id DESC")
        self.assertEqual(tweet["status"], "failed")
        self.assertEqual(tweet["error_message"], "payment_required")

    def test_402_no_duplicate_pending_post_for_same_story(self):
        db = self._db()
        article = self._article_row(db)
        # the same story hits 402 in two consecutive cycles
        self._cycle_402(db, article)
        self._cycle_402(db, article)
        self.assertEqual(len(db.pending_posts()), 1)
        # direct model-level dedupe as well
        self.assertIsNone(db.insert_pending_post(
            article["id"], "another text", "The Hindu", "https://x.com/1"))
        self.assertEqual(len(db.pending_posts()), 1)

    def test_pending_story_not_reselected_for_posting(self):
        db = self._db()
        article = self._article_row(db)
        self._cycle_402(db, article)
        from app.news.collector import select_stories
        # the queued story is not re-offered for posting next cycle
        self.assertEqual(len(select_stories(db)), 0)

    def test_pending_cli_print_format(self):
        import io
        db = self._db()
        db.insert_pending_post(1, "Tweet text line one.\n• bullet.",
                               "The Hindu", "https://x.com/1")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            from app.main import print_pending_posts
            print_pending_posts(db)
        out = buf.getvalue()
        self.assertIn("pending post #1", out)
        self.assertIn("The Hindu", out)
        self.assertIn("https://x.com/1", out)
        self.assertIn("Tweet text line one.\n• bullet.", out)
        self.assertIn("1 pending post(s)", out)
        # empty queue prints cleanly
        db2 = self._db()
        buf2 = io.StringIO()
        with mock.patch("sys.stdout", buf2):
            print_pending_posts(db2)
        self.assertIn("No pending posts.", buf2.getvalue())


class TestManualPublishWorkflow(unittest.TestCase):
    """--publish-next / --mark-posted: zero X API calls, clipboard +
    browser compose only, pending rows untouched until marked."""

    def _db(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def _queue(self, db, tweet="Breaking: test tweet text.\n• point one.",
               n=1):
        for i in range(n):
            db.insert_pending_post(i + 1, tweet, "The Hindu",
                                   "https://x.com/%d" % (i + 1))

    def test_publish_next_empty_queue(self):
        import io
        from app.main import publish_next
        db = self._db()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), \
             mock.patch("app.main.subprocess.run") as run:
            rc = publish_next(db)
        self.assertEqual(rc, 1)
        self.assertIn("No pending posts.", buf.getvalue())
        # no clipboard, no browser
        self.assertFalse(run.called)

    def test_publish_next_copies_and_opens_compose(self):
        import io
        from app.main import publish_next
        db = self._db()
        self._queue(db, "Cyclone Remal hits the coast.\n• Landfall at "
                        "110 kmph.\n\nSource: The Hindu")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), \
             mock.patch("app.main.subprocess.run") as run:
            rc = publish_next(db)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Cyclone Remal hits the coast.", out)
        self.assertIn("ready to paste", out)
        self.assertIn("--mark-posted", out)
        # pbcopy receives ONLY the tweet text (utf-8), and the compose
        # page is opened in the default browser
        pbcopy_call = run.call_args_list[0]
        self.assertEqual(pbcopy_call[0][0], ["pbcopy"])
        self.assertEqual(pbcopy_call[1]["input"],
                         b"Cyclone Remal hits the coast.\n"
                         b"\xe2\x80\xa2 Landfall at 110 kmph."
                         b"\n\nSource: The Hindu")
        open_call = run.call_args_list[1]
        self.assertEqual(open_call[0][0],
                         ["open", "https://x.com/compose/post"])
        # the pending post is NOT marked and NOT deleted
        self.assertEqual(len(db.pending_posts()), 1)
        self.assertEqual(db.pending_posts()[0]["status"], "pending")

    def test_publish_next_oldest_first(self):
        import io
        from app.main import publish_next
        db = self._db()
        db.insert_pending_post(1, "First queued tweet.", "s", "u1")
        db.insert_pending_post(2, "Second queued tweet.", "s", "u2")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), \
             mock.patch("app.main.subprocess.run"):
            publish_next(db)
        self.assertIn("First queued tweet.", buf.getvalue())
        self.assertNotIn("Second queued tweet.", buf.getvalue())

    def test_publish_next_survives_clipboard_failure(self):
        import io
        from app.main import publish_next
        db = self._db()
        self._queue(db, "Tweet text survives clipboard failure.")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), \
             mock.patch("app.main.subprocess.run",
                        side_effect=OSError("no pbcopy")):
            rc = publish_next(db)
        self.assertEqual(rc, 0)
        self.assertIn("Tweet text survives clipboard failure.", buf.getvalue())
        self.assertIn("copy the tweet text above", buf.getvalue())
        self.assertEqual(len(db.pending_posts()), 1)

    def test_mark_posted_removes_from_pending(self):
        import io
        from app.main import mark_posted
        db = self._db()
        self._queue(db, "Tweet to be marked.")
        pid = db.pending_posts()[0]["id"]
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = mark_posted(db, pid)
        self.assertEqual(rc, 0)
        self.assertIn("marked as posted", buf.getvalue())
        # gone from the pending queue, row kept with status posted
        self.assertEqual(len(db.pending_posts()), 0)
        row = db.query_one("SELECT status FROM pending_posts WHERE id=?",
                           (pid,))
        self.assertEqual(row["status"], "posted")

    def test_mark_posted_unknown_id(self):
        import io
        from app.main import mark_posted
        db = self._db()
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = mark_posted(db, 999)
        self.assertEqual(rc, 1)
        self.assertIn("not found", buf.getvalue())

    def test_mark_posted_twice_is_idempotent_message(self):
        import io
        from app.main import mark_posted
        db = self._db()
        self._queue(db, "Tweet marked twice.")
        pid = db.pending_posts()[0]["id"]
        with mock.patch("sys.stdout", io.StringIO()):
            mark_posted(db, pid)
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = mark_posted(db, pid)
        self.assertEqual(rc, 1)
        self.assertIn("already marked 'posted'", buf.getvalue())


class TestSafetyGuards(unittest.TestCase):
    """Dry-run and AUTO_POST guards — using the real run_cycle code."""

    def _db(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Database(os.path.join(d.name, "t.db"))

    def test_dry_run_never_calls_x_api(self):
        db = self._db()
        article = {"id": 1, "title": "RBI keeps repo rate unchanged",
                   "summary": "The MPC voted to hold.",
                   "source": "The Hindu", "normalized_url": "https://x.com/1",
                   "url": "https://x.com/1", "india_relevance_score": 0.8,
                   "importance_score": 0.6, "story_cluster_id": 1,
                   "status": "new"}
        with mock.patch("app.main.collect_and_process", return_value={}), \
             mock.patch("app.main.select_stories", return_value=[article]), \
             mock.patch("app.main.check_rate_limits",
                        return_value=(True, "")), \
             mock.patch("app.main.XClient") as xc:
            xc.return_value.configured = True
            results = run_cycle(db, dry_run=True)
        # the safety property: no post_tweet call ever happened in dry-run
        self.assertFalse(xc.return_value.post_tweet.called)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2], "dry_run")

    def test_autopost_false_blocks_live_posting(self):
        db = self._db()
        with mock.patch("app.main.collect_and_process", return_value={}), \
             mock.patch("app.main.select_stories", return_value=[]), \
             mock.patch.object(CONFIG, "BOT_ENABLED", True), \
             mock.patch.object(CONFIG, "AUTO_POST", False), \
             mock.patch("app.main.XClient") as xc:
            results = run_cycle(db, dry_run=False, force=False)
        self.assertEqual(results, [])
        self.assertFalse(xc.return_value.post_tweet.called)

    def test_verify_me_is_read_only(self):
        """verify_me must issue exactly one GET to /users/me and nothing else."""
        http = FakeHTTP({ME_URL: FakeResponse(200, {
            "data": {"id": "1", "username": "testuser", "name": "Test"}})})
        c = XClient("k", "s", "t", "ts", http_client=http)
        r = c.verify_me()
        self.assertTrue(r.ok)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["method"], "GET")
        self.assertEqual(http.calls[0]["url"], ME_URL)

    def test_kill_switch_blocks_live_posting(self):
        db = self._db()
        with mock.patch.object(CONFIG, "BOT_ENABLED", False), \
             mock.patch("app.main.XClient") as xc:
            results = run_cycle(db, dry_run=False)
        self.assertEqual(results, [])
        self.assertFalse(xc.return_value.post_tweet.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
