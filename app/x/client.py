"""Official X API v2 client — OAuth 1.0a user-context, stdlib signing.

Only the official X API is used. No browser automation, no scraping,
no unofficial APIs. Credentials come exclusively from environment
variables and are never logged.

The HTTP transport is injectable so tests run fully offline.
"""
import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid

import httpx

from app.logger import get_logger

log = get_logger("x.client")

POST_URL = "https://api.x.com/2/tweets"
ME_URL = "https://api.x.com/2/users/me"

CHAR_LIMIT = 280


def _percent_encode(s):
    return urllib.parse.quote(str(s), safe="")


def _signature_base(method, url, params):
    """Normalized signature base string per RFC 5849 §3.4.1:
    METHOD & percent-encoded URL & percent-encoded sorted k=v pairs."""
    enc = _percent_encode
    sorted_params = "&".join(
        "%s=%s" % (enc(k), enc(v)) for k, v in sorted(params.items()))
    return "&".join([method.upper(), enc(url), enc(sorted_params)])


def _sign(method, url, params, consumer_secret, token_secret):
    """HMAC-SHA1 signature, Base64-encoded per OAuth 1.0a (RFC 5849 §3.4).

    IMPORTANT: `params` must contain only the request parameters that
    participate in the signature per RFC 5849 §3.4.1.3 — the oauth_*
    parameters plus, when the body is application/x-www-form-urlencoded,
    the body parameters. X API v2 tweet posts send a JSON body
    (Content-Type: application/json), and a JSON body is NOT
    form-encoded, so it MUST be excluded from the base string. Signing
    the JSON body makes X's server-side signature computation mismatch
    and every post fails with 401 auth_failed."""
    key = (_percent_encode(consumer_secret) + "&" +
           _percent_encode(token_secret)).encode()
    digest = hmac.new(key, _signature_base(method, url, params).encode(),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class XResult:
    """Uniform result: ok flag, data, machine-readable error, no secrets."""

    def __init__(self, ok=False, data=None, error=None, status=None,
                 retry_after=None):
        self.ok = ok
        self.data = data or {}
        self.error = error
        self.status = status
        self.retry_after = retry_after

    def __repr__(self):
        # never include headers/body that might echo credentials
        return "XResult(ok=%s, error=%s, status=%s)" % (
            self.ok, self.error, self.status)


def validate_payload(text):
    """Local checks before any HTTP call. Returns error string or None."""
    if not text or not text.strip():
        return "empty_text"
    if len(text.strip()) < 20:
        return "text_too_short"
    if len(text) > CHAR_LIMIT:
        return "text_too_long:%d" % len(text)
    return None


class XClient:
    def __init__(self, api_key, api_secret, access_token,
                 access_token_secret, http_client=None):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.access_token = (access_token or "").strip()
        self.access_token_secret = (access_token_secret or "").strip()
        self._http = http_client or httpx

    @property
    def configured(self):
        return all([self.api_key, self.api_secret,
                    self.access_token, self.access_token_secret])

    def credential_status(self):
        """Which env vars are set — booleans only, never values."""
        return {
            "X_API_KEY": bool(self.api_key),
            "X_API_SECRET": bool(self.api_secret),
            "X_ACCESS_TOKEN": bool(self.access_token),
            "X_ACCESS_TOKEN_SECRET": bool(self.access_token_secret),
        }

    def missing_fields(self):
        """Names of missing credential env vars (for clear setup errors)."""
        fields = [("X_API_KEY", self.api_key),
                  ("X_API_SECRET", self.api_secret),
                  ("X_ACCESS_TOKEN", self.access_token),
                  ("X_ACCESS_TOKEN_SECRET", self.access_token_secret)]
        return [name for name, val in fields if not val]

    # --- auth ---

    def _oauth_header(self, method, url, body=None):
        oauth_params = {
            "oauth_consumer_key": self.api_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token,
            "oauth_version": "1.0",
        }
        # The body is deliberately NOT merged into the signature
        # parameters: POST /2/tweets sends JSON (not form-encoded), and
        # per RFC 5849 §3.4.1.3 / X's signature documentation only
        # form-encoded body parameters belong in the base string.
        # Including them would produce a 401 on every post.
        oauth_params["oauth_signature"] = _sign(
            method, url, oauth_params, self.api_secret,
            self.access_token_secret)
        header = "OAuth " + ", ".join(
            '%s="%s"' % (_percent_encode(k), _percent_encode(v))
            for k, v in sorted(oauth_params.items()))
        return header

    # --- error mapping ---

    @staticmethod
    def _map_error(status, resp_headers=None):
        headers = resp_headers or {}
        if status == 429:
            retry = headers.get("retry-after") or headers.get("Retry-After")
            return "rate_limited", (int(retry) if retry and str(retry).isdigit()
                                    else None)
        if status == 401:
            return "auth_failed", None
        if status == 402:
            # X API payment required — developer account has no credits;
            # posting is unavailable but the account/credentials are fine
            return "payment_required", None
        if status == 403:
            return "forbidden", None
        if status == 404:
            return "not_found", None
        return "http_%d" % status, None

    def _request(self, method, url, body=None):
        if not self.configured:
            missing = ", ".join(self.missing_fields())
            return XResult(error="not_configured:%s" % missing)
        headers = {"Authorization": self._oauth_header(method, url, body)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._http.request(
                method, url, json=body, timeout=20, headers=headers)
        except Exception as e:
            log.error("X API request failed: %s", type(e).__name__)
            return XResult(error="network_error")
        if resp.status_code in (200, 201):
            try:
                return XResult(ok=True, data=resp.json(),
                               status=resp.status_code)
            except ValueError:
                return XResult(error="bad_response", status=resp.status_code)
        error, retry_after = self._map_error(resp.status_code,
                                             getattr(resp, "headers", None))
        log.error("X API error %d (%s)", resp.status_code, error)
        return XResult(error=error, status=resp.status_code,
                       retry_after=retry_after)

    # --- API operations ---

    def verify_me(self):
        """GET /2/users/me — checks authentication and the account in use."""
        result = self._request("GET", ME_URL)
        if result.ok:
            result.data = result.data.get("data", {})
        return result

    def post_tweet(self, text):
        """POST /2/tweets — creates a post. Never called in dry-run mode."""
        payload_error = validate_payload(text)
        if payload_error:
            return XResult(error=payload_error)
        result = self._request("POST", POST_URL, body={"text": text})
        if result.ok:
            result.data = result.data.get("data", {})
        return result
