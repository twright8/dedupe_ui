# backend/app/services/html_sanitizer.py
"""Conservative allow-list HTML sanitizer.

The methodology-notes panel stores rich HTML authored in the browser and shows
it to every user, so a saved note is a stored-XSS surface. Rather than trust the
client, we rebuild the HTML server-side keeping only a small set of formatting
tags and attributes; anything else (scripts, event handlers, inline styles,
javascript: URLs, unknown tags) is dropped. Disallowed tags lose the tag but
keep their text; <script>/<style> lose their contents entirely.

This is deliberately simple (no external dependency) and errs on the side of
stripping. It is not a general-purpose sanitizer — it only needs to cover the
output of our own WYSIWYG editor plus whatever a user might paste in.
"""

from html import escape
from html.parser import HTMLParser

_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4",
    "strong", "b", "em", "i", "u", "s", "sub", "sup",
    "ul", "ol", "li", "blockquote", "code", "pre", "a", "span", "div",
    "table", "thead", "tbody", "tr", "th", "td",
}
_VOID_TAGS = {"br", "hr"}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
_DROP_WITH_CONTENT = {"script", "style"}


def _safe_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return (
        v.startswith("http://")
        or v.startswith("https://")
        or v.startswith("mailto:")
        or v.startswith("/")
        or v.startswith("#")
    )


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0  # inside a <script>/<style> we drop everything

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_WITH_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, set())
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in allowed or value is None:
                continue
            if name == "href" and not _safe_url(value):
                continue
            kept.append(f' {name}="{escape(value, quote=True)}"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if not self._skip_depth and tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_WITH_CONTENT:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS or tag in _VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.out.append(escape(data, quote=False))


def sanitize_html(html: str) -> str:
    """Return *html* with only allow-listed tags/attributes retained."""
    if not html:
        return ""
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)
