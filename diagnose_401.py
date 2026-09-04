"""Read-only 401 diagnostic. Prints NO credential values — only lengths,
boolean hygiene flags, clock skew, and the X error body (which never
contains credentials). Makes only GET/HEAD requests; posts nothing."""
import datetime
import email.utils
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

# 1) clock skew vs X server Date header (unauthenticated HEAD, read-only)
r = httpx.head("https://api.x.com/2/users/me", timeout=10)
server = r.headers.get("date")
local = datetime.datetime.now(datetime.timezone.utc).strftime(
    "%a, %d %b %Y %H:%M:%S GMT")
print("server date:", server, "| local:", local)
if server:
    skew = datetime.datetime.now(datetime.timezone.utc) - \
        email.utils.parsedate_to_datetime(server)
    print("clock skew seconds:", round(skew.total_seconds(), 1))

# 2) env value hygiene (booleans/lengths only, never values)
for k in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
          "X_ACCESS_TOKEN_SECRET"]:
    v = os.environ.get(k, "")
    print(k, "len:", len(v), "has_quote:", '"' in v or "'" in v,
          "has_cr:", "\r" in v, "has_space:", " " in v,
          "has_placeholder:", "here" in v)

# 3) authenticated GET, show X's own error detail (body has no secrets)
from app.x.client import XClient  # noqa: E402

c = XClient(os.environ.get("X_API_KEY", ""),
            os.environ.get("X_API_SECRET", ""),
            os.environ.get("X_ACCESS_TOKEN", ""),
            os.environ.get("X_ACCESS_TOKEN_SECRET", ""))
resp = c._http.request(
    "GET", "https://api.x.com/2/users/me", timeout=20,
    headers={"Authorization": c._oauth_header(
        "GET", "https://api.x.com/2/users/me")})
print("status:", resp.status_code)
print("body:", resp.text[:400])
