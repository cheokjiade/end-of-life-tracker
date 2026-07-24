"""Jackson lifecycle scraper.

FasterXML's Jackson Releases wiki page lists branches as either "open"
(currently maintained) or "closed" (no further patches). No specific EOL
dates are published — this provider returns ok/eol only.
"""

import html.parser
import re
import urllib.request

from ..core import _error_result, logger

_JACKSON_WIKI_URL = "https://github.com/FasterXML/jackson/wiki/Jackson-Releases"
# Canary: 2.18 is an LTS that should consistently appear; if it's absent
# from both buckets, the wiki has been restructured.
_JACKSON_CANARY = "2.18"
_JACKSON_CACHE = None


class _JacksonWikiParser(html.parser.HTMLParser):
    """Extract Jackson branch statuses from the FasterXML Releases wiki page.

    The page uses <h3> headings like "Open branches" / "Closed branches" /
    "Legacy" within an <h2>"Public releases" section. Each list item under
    a section starts with a link tag <a href="Jackson-Release-X.Y">X.Y</a>
    whose text content is the branch number — that's what we collect.

    Sections handled:
      "Open branches" / "Currently Maintained" -> open
      "Closed branches" / "Recently Closed"    -> closed
      "Legacy"                                 -> closed (old majors)
      anything else                            -> ignored
    """

    def __init__(self):
        super().__init__()
        self._section = None       # "open" | "closed" | None
        self._heading_tag = None
        self._heading_buf = []
        self._in_anchor = False
        self._anchor_buf = []
        self.open_branches = set()
        self.closed_branches = set()

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading_tag = tag
            self._heading_buf = []
        elif tag == "a" and self._section is not None:
            self._in_anchor = True
            self._anchor_buf = []

    def handle_endtag(self, tag):
        if self._heading_tag is not None and tag == self._heading_tag:
            heading = " ".join("".join(self._heading_buf).split()).lower()
            self._heading_tag = None
            if "open" in heading or "maintain" in heading:
                self._section = "open"
            elif "closed" in heading or "legacy" in heading:
                self._section = "closed"
            elif tag in ("h1", "h2"):
                self._section = None
            # h3/h4 with non-matching name leaves the section unchanged
        elif tag == "a" and self._in_anchor:
            self._in_anchor = False
            text = "".join(self._anchor_buf).strip()
            m = re.fullmatch(r"(\d+\.\d+)", text)
            if m and self._section:
                if self._section == "open":
                    self.open_branches.add(m.group(1))
                elif self._section == "closed":
                    self.closed_branches.add(m.group(1))

    def handle_data(self, data):
        if self._heading_tag is not None:
            self._heading_buf.append(data)
        elif self._in_anchor:
            self._anchor_buf.append(data)


def _scrape_jackson_lifecycle():
    """Fetch + parse the Jackson Releases wiki page."""
    global _JACKSON_CACHE
    if _JACKSON_CACHE is not None:
        return _JACKSON_CACHE

    req = urllib.request.Request(_JACKSON_WIKI_URL, headers={
        "Accept": "text/html",
        "User-Agent": "EOL-Tracker/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        html_text = resp.read().decode("utf-8", errors="replace")

    parser = _JacksonWikiParser()
    parser.feed(html_text)

    if not parser.open_branches:
        raise ValueError(
            f"No 'open' Jackson branches detected. closed={parser.closed_branches}. "
            f"Wiki structure may have changed."
        )
    if (_JACKSON_CANARY not in parser.open_branches
            and _JACKSON_CANARY not in parser.closed_branches):
        raise ValueError(
            f"Jackson canary failed: branch {_JACKSON_CANARY} not found. "
            f"open={parser.open_branches}, closed={parser.closed_branches}"
        )

    logger.info(
        "Jackson lifecycle parsed: open=%s closed=%s",
        sorted(parser.open_branches), sorted(parser.closed_branches)
    )

    _JACKSON_CACHE = {"open": parser.open_branches, "closed": parser.closed_branches}
    return _JACKSON_CACHE


def _provider_jackson_lifecycle(entry, today):
    """Look up Jackson branch status from the FasterXML wiki."""
    version = str(entry.get("version", ""))
    label = entry.get("label", f"Jackson {version}.x")

    try:
        data = _scrape_jackson_lifecycle()
    except Exception as exc:
        logger.error("Jackson scraper failed: %s", exc)
        result = _error_result(entry, f"Jackson scraper failed: {exc}")
        result["source"] = "jackson_lifecycle"
        return result

    open_branches = data["open"]
    closed_branches = data["closed"]

    if version in open_branches:
        status = "ok"
        message = f"Branch {version} is currently maintained per FasterXML wiki"
    elif version in closed_branches:
        status = "eol"
        message = (
            f"Branch {version} has been closed by FasterXML "
            f"(no further patches; no specific EOL date published)"
        )
    else:
        result = _error_result(
            entry,
            f"Branch '{version}' not in Jackson wiki. "
            f"Open: {sorted(open_branches, reverse=True)}; "
            f"closed: {sorted(closed_branches, reverse=True)}"
        )
        result["source"] = "jackson_lifecycle"
        return result

    def _vkey(v):
        try:
            return tuple(int(p) for p in v.split("."))
        except (ValueError, AttributeError):
            return (-1,)
    latest_open = max(open_branches, key=_vkey) if open_branches else None
    on_latest = (version == latest_open) if latest_open else False

    return {
        "label": label,
        "product": "jackson",
        "version": version,
        "lts": False,
        "status": status,
        "message": message,
        "latest_patch": None,
        "latest_patch_date": None,
        "latest_cycle": latest_open,
        "latest_cycle_version": latest_open,
        "latest_cycle_release_date": None,
        "on_latest_cycle": on_latest,
        "eol_date": None,
        "support_date": None,
        "days_remaining": None,
        "support_days_remaining": None,
        "source": "jackson_lifecycle",
    }


SOURCE = "jackson_lifecycle"
LABEL = "FasterXML wiki"
provider = _provider_jackson_lifecycle


def url_for(r):
    return _JACKSON_WIKI_URL
