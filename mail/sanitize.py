"""HTML mail bodies made safe to render.

Stored bodies are cleaned once, at sync (:func:`clean`): no scripts, styles, event handlers,
forms, frames or ``javascript:`` URLs; links get ``rel="noopener noreferrer"`` and open in a new
tab. Remote images stay in the stored body and are stripped on read (:func:`block_remote`) unless
a client asks for them, because loading one tells the sender when and where the mail was read.

Inline images keep their ``cid:`` references; a client resolves them through the message's
``attachments { contentId }``.
"""

import re
from html import unescape
from html.parser import HTMLParser

import nh3

ALLOWED_TAGS = {
    "a", "abbr", "address", "b", "bdi", "bdo", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup",
    "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "font", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike", "strong",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr",
}  # fmt: skip

_COMMON_ATTRIBUTES = {"style", "title", "dir", "lang", "align", "valign", "width", "height", "bgcolor", "color", "border"}
ALLOWED_ATTRIBUTES = {
    "*": _COMMON_ATTRIBUTES,
    "a": {"href", "name", "target"},
    "img": {"src", "alt", "hspace", "vspace"},
    "font": {"face", "size"},
    "table": {"cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "nowrap"},
    "th": {"colspan", "rowspan", "nowrap", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "type"},
    "li": {"value"},
    "time": {"datetime"},
    "q": {"cite"},
    "blockquote": {"cite"},
}
URL_SCHEMES = {"http", "https", "mailto", "tel", "cid", "data"}

# Anything that can load a resource from CSS -- and any escape (``\75 rl(``), which could spell one.
_CSS_URL = re.compile(r"url\s*\(|image-set|image\s*\(|src\s*\(|expression\s*\(|@import|behavior\s*:|-moz-binding|\\", re.IGNORECASE)
_REMOTE = re.compile(r"^\s*(https?:)?//", re.IGNORECASE)
_DATA_IMAGE = re.compile(r"^\s*data:image/(png|gif|jpe?g|webp|bmp);base64,", re.IGNORECASE)


def _filter(block_remote: bool):  # noqa: ANN202
    def attribute_filter(element: str, attribute: str, value: str) -> str | None:
        if attribute == "style":
            # CSS can load remote resources (background: url(…)) and has no business doing so here.
            return None if _CSS_URL.search(value) else value
        if element == "img" and attribute == "src":
            if value.lower().startswith("cid:") or _DATA_IMAGE.match(value):
                return value
            if _REMOTE.match(value):
                return None if block_remote else value
            return None
        if element == "a" and attribute == "href" and value.strip().lower().startswith(("data:", "cid:")):
            return None
        if element == "a" and attribute == "target":
            return "_blank"
        return value

    return attribute_filter


def clean(html: str) -> str:
    """The stored form of an HTML body: safe, with remote images kept."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags={"script", "style", "title", "head", "noscript", "template", "iframe", "object", "embed", "svg", "math"},
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter(block_remote=False),
        url_schemes=URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


def block_remote(html: str) -> str:
    """``html`` (already :func:`clean`) without remote images."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter(block_remote=True),
        url_schemes=URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


_IMG_REMOTE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']?\s*(https?:)?//", re.IGNORECASE)


def has_remote_images(html: str) -> bool:
    """Whether a (cleaned) body loads images from the internet."""
    return bool(_IMG_REMOTE.search(html or ""))


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "hr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ANN001
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable plain text of an HTML body (for bodies without a text part, snippets and search)."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return unescape(re.sub(r"<[^>]+>", " ", html or ""))
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
