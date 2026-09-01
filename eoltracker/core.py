"""Shared building blocks for the EOL tracker.

Stdlib-only primitives every parser leans on: the process logger, the
date-field parser, the uniform error-result shape, and the two reusable
HTML table parsers. Parsers import from here; this module imports nothing
from the rest of the package (keeps the import graph acyclic).
"""

import gzip
import html.parser
import io
import logging
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024


def _read_bounded(stream, max_bytes, label):
    """Read *stream* until EOF or *max_bytes* bytes, tolerating short reads.

    ``read(size)`` may legitimately return fewer bytes than requested before
    EOF (``http.client.HTTPResponse`` and boto3 ``StreamingBody`` are both
    allowed to), so reads loop until EOF. One extra byte beyond the limit is
    consumed to detect over-limit bodies, which raise instead of being
    returned silently truncated.
    """
    chunks = []
    total = 0
    while total <= max_bytes:
        chunk = stream.read(max_bytes + 1 - total)
        if chunk is None:
            raise ValueError(
                f"{label} stream read() returned None; "
                "non-compliant stream")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} byte limit")
    return b"".join(chunks)


def read_response_bytes(response, max_bytes=MAX_HTTP_BODY_BYTES):
    """Read a response body with a hard byte limit.

    ``urllib`` response bodies and boto3 ``StreamingBody`` objects both
    support ``read(size)``, which may short-read before EOF; the read loops
    until EOF or the limit so a compliant short-reading stream is never
    silently truncated. Reading one byte beyond the limit lets callers
    fail loudly instead of parsing a silently truncated document.
    """
    if max_bytes < 1:
        raise ValueError("response byte limit must be positive")
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            # A malformed or non-numeric length cannot be trusted; the bounded
            # read below remains authoritative.
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise ValueError(
                f"response exceeds {max_bytes} byte limit "
                f"(Content-Length: {content_length})")
    return _read_bounded(response, max_bytes, "response")


def decompress_gzip_bytes(raw, max_bytes=MAX_HTTP_BODY_BYTES):
    """Decompress one gzip body without allowing unbounded expansion.

    Decompression streams through ``GzipFile`` with the same hard cap as the
    byte-limited HTTP read, so a decompression bomb fails loudly instead of
    exhausting memory.
    """
    if max_bytes < 1:
        raise ValueError("decompressed byte limit must be positive")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        return _read_bounded(stream, max_bytes, "decompressed response")


def parse_date_field(value):
    """Parse an EOL/support field which can be a date string, bool, or None.

    Returns:
        date   — if a valid date string was provided
        True   — already EOL / support ended (no specific date)
        False  — no EOL planned / still supported
        None   — field missing or unparseable
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _error_result(entry, message):
    """Build an error-shaped result for a config entry.

    Tolerates non-dict entries (e.g. a JSON array element that is not an
    object) so one unusable product row can never break the normalization
    contract the formatters rely on.
    """
    if isinstance(entry, dict):
        label = entry.get(
            "label", f'{entry.get("product", "?")} {entry.get("version", "?")}')
        if not isinstance(label, str):
            label = str(label)
        source = entry.get("source", "endoflife_date")
        if not isinstance(source, str):
            source = "unknown"
        return {
            "label": label,
            "product": entry.get("product"),
            "version": entry.get("version"),
            "status": "error",
            "message": message,
            "eol_date": None,
            "days_remaining": None,
            "latest_patch": None,
            "source": source,
        }
    return {
        "label": f"<unusable product entry ({type(entry).__name__})>",
        "product": None,
        "version": None,
        "status": "error",
        "message": message,
        "eol_date": None,
        "days_remaining": None,
        "latest_patch": None,
        "source": "unknown",
    }


# ---------------------------------------------------------------------------
# Heading-anchored HTML table parser (used by the AWS RDS scraper, whose
# calendar page has multiple tables — the target table is located by its
# preceding <h2> heading text).
# ---------------------------------------------------------------------------

class _AWSCalendarParser(html.parser.HTMLParser):
    """Locates a section by H2 text and extracts its first <table>."""

    def __init__(self, target_heading):
        super().__init__()
        self.target_heading = target_heading
        self.section_found = False
        self.headers = []
        self.rows = []
        self._in_h2 = False
        self._h2_buf = []
        self._in_target = False
        self._in_table = False
        self._table_done = False
        self._row = None
        self._cell_kind = None
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if self._table_done:
            return
        if tag == "h2":
            self._in_h2 = True
            self._h2_buf = []
            return
        if not self._in_target:
            return
        if tag == "table" and not self._in_table:
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell_kind = tag
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            heading = " ".join("".join(self._h2_buf).split())
            self._in_h2 = False
            if self.target_heading in heading:
                self._in_target = True
                self.section_found = True
            elif self._in_target:
                if self._in_table:
                    self._table_done = True
                self._in_target = False
            return
        if not self._in_target:
            return
        if tag in ("th", "td") and self._cell_kind is not None:
            cell = " ".join("".join(self._cell_buf).split())
            self._row.append((self._cell_kind, cell))
            self._cell_kind = None
            self._cell_buf = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                if not self.headers and all(k == "th" for k, _ in self._row):
                    self.headers = [t for _, t in self._row]
                else:
                    self.rows.append([t for _, t in self._row])
            self._row = None
        elif tag == "table" and self._in_table:
            self._in_table = False
            self._table_done = True

    def handle_data(self, data):
        if self._in_h2:
            self._h2_buf.append(data)
        elif self._cell_kind is not None:
            self._cell_buf.append(data)


# ---------------------------------------------------------------------------
# Generic HTML table extractor
#
# Used by scrapers whose target page has only one table of interest. Finds
# the first <table> whose <th> row contains every entry in required_headers.
# (The _AWSCalendarParser above is heading-anchored and is used by the
# AWS RDS scraper, where the calendar page has multiple tables.)
# ---------------------------------------------------------------------------

class _HtmlTableExtractor(html.parser.HTMLParser):
    """Extract the first <table> whose <th> row contains all required_headers."""

    def __init__(self, required_headers):
        super().__init__()
        self._required = set(required_headers)
        self.headers = []
        self.rows = []
        self.found = False
        self._depth = 0
        self._cur_headers = []
        self._cur_rows = []
        self._row = None
        self._cell_kind = None
        self._cell_buf = []

    def handle_starttag(self, tag, attrs):
        if self.found:
            return
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._cur_headers = []
                self._cur_rows = []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell_kind = tag
            self._cell_buf = []

    def handle_endtag(self, tag):
        if self.found:
            return
        if tag == "table":
            if self._depth == 1:
                if self._cur_headers and self._required.issubset(set(self._cur_headers)):
                    self.headers = self._cur_headers
                    self.rows = self._cur_rows
                    self.found = True
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("th", "td") and self._cell_kind is not None:
            cell = " ".join("".join(self._cell_buf).split())
            self._row.append((self._cell_kind, cell))
            self._cell_kind = None
            self._cell_buf = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                if not self._cur_headers and all(k == "th" for k, _ in self._row):
                    self._cur_headers = [t for _, t in self._row]
                else:
                    self._cur_rows.append([t for _, t in self._row])
            self._row = None

    def handle_data(self, data):
        if self._cell_kind is not None:
            self._cell_buf.append(data)
