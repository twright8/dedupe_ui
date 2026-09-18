# backend/app/base_path.py
"""Base path support — the app is served under a prefix such as /donations.

Caddy routes /donations/* and /psc/* to two instances of this app. Whether the
proxy strips the prefix before forwarding is a proxy setting we do not control,
so the app accepts both forms: the middleware strips BASE_PATH from the front of
the request path when it is there, and leaves the path alone when it is not.

Session cookies keep path "/" on purpose — one login is shared across every tool
on the host (design decision D19).
"""

import html
import os
import re

_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)


def base_path() -> str:
    """BASE_PATH normalised: a leading slash, no trailing slash, '' when unset.

    Read from the environment at call time so tests can change it per case.
    """
    raw = (os.environ.get("BASE_PATH") or "").strip()
    trimmed = raw.strip("/")
    return f"/{trimmed}" if trimmed else ""


class BasePathMiddleware:
    """Strip BASE_PATH from the start of the request path when present.

    Pure ASGI rather than BaseHTTPMiddleware so it can run outside the session
    check: /donations/api/health must look like /api/health by the time auth
    decides whether the path is public.

    ``root_path`` is deliberately left alone. Starlette subtracts root_path from
    the path before matching routes, so setting it here would strip the prefix
    twice.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            prefix = base_path()
            path = scope.get("path", "")
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                scope = dict(scope)
                scope["path"] = path[len(prefix):] or "/"
                raw_path = scope.get("raw_path")
                if raw_path:
                    scope["raw_path"] = raw_path[len(prefix.encode()):] or b"/"
        await self.app(scope, receive, send)


def render_index(html_text: str, title: str) -> str:
    """Inject the base path and the profile title into the SPA's index.html.

    The frontend builds with relative asset URLs, so <base> is what makes
    ./assets/... resolve under the prefix, and window.__BASE__ is what the app
    prepends to its API calls. Done by string replacement at serve time, never
    cached, so one build works under any prefix.
    """
    prefix = base_path()
    injection = (
        f'<base href="{prefix}/">'
        f'<script>window.__BASE__="{prefix}";</script>'
    )

    if "<head>" in html_text:
        html_text = html_text.replace("<head>", f"<head>{injection}", 1)
    else:
        html_text = injection + html_text

    safe_title = html.escape(title or "")
    if _TITLE_RE.search(html_text):
        html_text = _TITLE_RE.sub(f"<title>{safe_title}</title>", html_text, count=1)
    else:
        html_text = html_text.replace(
            injection, f"{injection}<title>{safe_title}</title>", 1
        )
    return html_text
