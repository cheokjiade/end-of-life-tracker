"""Network-free safety regressions for shared provider dispatch and bodies."""

import gzip
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eoltracker.core import decompress_gzip_bytes, read_response_bytes
from eoltracker.parsers import PROVIDERS, _URL_FNS, check_product
from eoltracker.parsers import endoflife_date, npm_registry
from eoltracker.report import format_report_html

TODAY = date(2026, 8, 29)


class FakeResponse:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}
        self._offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self.body[self._offset:]
        else:
            chunk = self.body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class OneByteStream:
    """Worst-case compliant body: one byte per read until EOF."""

    def __init__(self, body):
        self._body = body
        self._offset = 0
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        chunk = self._body[self._offset:self._offset + 1]
        self._offset += len(chunk)
        return chunk


class TwoByteStream:
    """Compliant body that short-reads at most two bytes per read."""

    def __init__(self, body):
        self._body = body
        self._offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._body[self._offset:]
        else:
            chunk = self._body[self._offset:self._offset + min(size, 2)]
        self._offset += len(chunk)
        return chunk


def test_bounded_response_and_gzip_helpers():
    assert read_response_bytes(FakeResponse(b"1234"), max_bytes=4) == b"1234"
    for response in (
            FakeResponse(b"12345"),
            FakeResponse(b"x", {"Content-Length": "5"})):
        try:
            read_response_bytes(response, max_bytes=4)
        except ValueError as exc:
            assert "byte limit" in str(exc)
        else:
            raise AssertionError("oversize response was not rejected")

    payload = gzip.compress(b"a" * 100)
    assert decompress_gzip_bytes(payload, max_bytes=100) == b"a" * 100
    try:
        decompress_gzip_bytes(payload, max_bytes=99)
    except ValueError as exc:
        assert "decompressed response" in str(exc)
    else:
        raise AssertionError("gzip expansion was not bounded")

    double = gzip.compress(b"a" * 60) + gzip.compress(b"b" * 60)
    assert decompress_gzip_bytes(double, max_bytes=120) == b"a" * 60 + b"b" * 60
    try:
        decompress_gzip_bytes(double, max_bytes=90)
    except ValueError as exc:
        assert "decompressed response" in str(exc)
    else:
        raise AssertionError("multi-member gzip expansion was not bounded")


def test_short_read_streams_are_read_to_limit():
    # A stream returning one byte per read must be read to completion, not
    # truncated at the first short read.
    stream = OneByteStream(b"1234")
    assert read_response_bytes(stream, max_bytes=4) == b"1234"
    assert stream.reads == 5  # four data reads plus one EOF probe
    assert read_response_bytes(TwoByteStream(b"abcdefgh"), max_bytes=8) == \
        b"abcdefgh"

    # Over-limit bodies are still detected even when they short-read.
    for stream in (OneByteStream(b"123456"), TwoByteStream(b"123456")):
        try:
            read_response_bytes(stream, max_bytes=4)
        except ValueError as exc:
            assert "byte limit" in str(exc)
        else:
            raise AssertionError("oversize short-read response was not rejected")

    # The limit boundary stays exact: max accepted, max+1 rejected.
    assert read_response_bytes(OneByteStream(b"1234"), max_bytes=4) == b"1234"
    try:
        read_response_bytes(OneByteStream(b"12345"), max_bytes=4)
    except ValueError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("limit boundary was not exact")


def test_none_reading_stream_fails_loud():
    # A stream whose read() returns None is non-compliant; the loop must
    # fail loudly instead of treating None as EOF (which would silently
    # return a truncated or empty body).
    class NoneStream:
        def __init__(self):
            self.reads = 0

        def read(self, size=-1):
            self.reads += 1
            return None

    stream = NoneStream()
    try:
        read_response_bytes(stream, max_bytes=8)
    except ValueError as exc:
        assert "None" in str(exc)
        assert stream.reads == 1
    else:
        raise AssertionError("None-reading stream was not rejected")


def test_runtime_config_bounds():
    # Runtime and helper config loading share the same size and depth
    # bounds: generated configs at the scanner's advertised maximum are
    # accepted, and over-limit/over-depth input fails with ValueError
    # (never RecursionError).
    from eoltracker.core import (
        MAX_CONFIG_DEPTH,
        MAX_CONFIG_FILE_BYTES,
        _max_nesting_depth,
        validate_bounded_json,
    )
    import json as json_mod

    # Max-bound generated config accepted.
    big = json_mod.dumps({
        "products": [], "_inventory": {
            "manifests": [f"f{i}" for i in range(5000)],
            "summary": {"files": 5000},
        }})
    assert len(big.encode("utf-8")) <= MAX_CONFIG_FILE_BYTES
    result = validate_bounded_json(big.encode("utf-8"))
    assert result["_inventory"]["summary"]["files"] == 5000

    # Depth at the limit accepted, one level over rejected.
    at_limit = json_mod.dumps({"k": [{"k": 1}] * 1})
    deep_ok = "{\"k\":" * MAX_CONFIG_DEPTH + "1" + "}" * MAX_CONFIG_DEPTH
    deep_bad = "{\"k\":" * (MAX_CONFIG_DEPTH + 1) + "1" + "}" * (
        MAX_CONFIG_DEPTH + 1)
    assert _max_nesting_depth(deep_ok) <= MAX_CONFIG_DEPTH
    result = validate_bounded_json(deep_ok.encode("utf-8"))
    try:
        validate_bounded_json(deep_bad.encode("utf-8"))
    except ValueError as exc:
        assert "nesting depth" in str(exc)
    else:
        raise AssertionError("over-depth config was not rejected")

    # Over-size rejected.
    oversize = b'{"k":1}' + b' ' * (MAX_CONFIG_FILE_BYTES + 1)
    try:
        validate_bounded_json(oversize)
    except ValueError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("over-size config was not rejected")

    # Non-object top-level rejected.
    try:
        validate_bounded_json(b"[1,2]")
    except ValueError as exc:
        assert "not an object" in str(exc)
    else:
        raise AssertionError("non-object config was not rejected")

    # Invalid JSON rejected.
    try:
        validate_bounded_json(b"{invalid}")
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("invalid JSON was not rejected")

    # Helper/runtime parity: the constants must match.
    helper_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "helper_scripts")
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)
    from eol_inventory.config_io import (
        MAX_CONFIG_DEPTH as H_DEPTH,
        MAX_CONFIG_FILE_BYTES as H_BYTES,
    )
    assert H_DEPTH == MAX_CONFIG_DEPTH
    assert H_BYTES == MAX_CONFIG_FILE_BYTES


def test_dispatch_isolates_provider_failures():
    source = "test_exploding_provider"
    PROVIDERS[source] = lambda entry, today: 1 / 0
    try:
        result = check_product({
            "source": source, "label": "Broken", "policy_note": "Keep note",
        }, TODAY)
    finally:
        PROVIDERS.pop(source, None)
    assert result["status"] == "error"
    assert result["source"] == source
    assert "ZeroDivisionError" in result["message"]
    assert result["policy_note"] == "Keep note"

    malformed = [
        check_product(None, TODAY),
        check_product([], TODAY),
        check_product({"source": []}, TODAY),
    ]
    assert all(item["status"] == "error" for item in malformed)
    assert "expected object" in malformed[0]["message"]
    assert "non-empty string" in malformed[2]["message"]

    invalid_results = (
        ({}, "missing required keys"),
        ({
            "label": "Broken", "product": "x", "version": "1",
            "status": "invented", "message": "bad", "days_remaining": None,
            "eol_date": None, "latest_patch": None,
            "source": "test_invalid_result",
        }, "unsupported status"),
        ({
            "label": "Broken", "product": "x", "version": "1",
            "status": "ok", "message": "bad", "days_remaining": "soon",
            "eol_date": None, "latest_patch": None,
            "source": "test_invalid_result",
        }, "days_remaining"),
        ({
            "label": "Broken", "product": "x", "version": "1",
            "status": "ok", "message": "bad", "days_remaining": None,
            "eol_date": None, "latest_patch": None, "source": "wrong_source",
        }, "does not match dispatched source"),
    )
    source = "test_invalid_result"
    try:
        for provider_result, expected in invalid_results:
            PROVIDERS[source] = lambda entry, today, value=provider_result: value
            result = check_product({"source": source, "label": "Broken"}, TODAY)
            assert result["status"] == "error"
            assert result["source"] == source
            assert expected in result["message"]
    finally:
        PROVIDERS.pop(source, None)


def test_malformed_provider_documents_return_error_rows():
    real_cycles = endoflife_date.fetch_all_cycles
    real_npm = npm_registry._fetch_npm_doc
    try:
        endoflife_date.fetch_all_cycles = lambda product: {"message": "Not Found"}
        eol_result = endoflife_date._provider_endoflife_date(
            {"product": "python", "version": "3.12"}, TODAY)
        assert eol_result["status"] == "error"
        assert "malformed" in eol_result["message"]

        npm_registry._fetch_npm_doc = lambda package: ["not", "an", "object"]
        npm_result = npm_registry._provider_npm_registry(
            {"source": "npm_registry", "package": "x", "version": "1.0.0"},
            TODAY)
        assert npm_result["status"] == "error"
        assert "not a JSON object" in npm_result["message"]
    finally:
        endoflife_date.fetch_all_cycles = real_cycles
        npm_registry._fetch_npm_doc = real_npm


def test_endoflife_urls_escape_config_values():
    captured = {}

    class ContextResponse(FakeResponse):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    real_urlopen = endoflife_date.urllib.request.urlopen
    try:
        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return ContextResponse(b"[]")

        endoflife_date.urllib.request.urlopen = fake_urlopen
        assert endoflife_date.fetch_all_cycles("valid-slug") == []
        assert endoflife_date.fetch_all_cycles("../bad?x") is None
    finally:
        endoflife_date.urllib.request.urlopen = real_urlopen

    assert captured["url"].endswith("/valid-slug.json")
    assert endoflife_date.url_for({"product": "../bad?x"}) is None


def test_provider_url_failures_do_not_break_html_reports():
    manual = check_product({
        "source": "manual", "label": "Manual", "version": "1",
        "reference_url": [],
    }, TODAY)
    html, _ = format_report_html([manual], [30, 60, 90], TODAY)
    assert "Manual" in html

    source = "test_bad_url"
    _URL_FNS[source] = lambda _result: 1 / 0
    result = {
        "label": "Broken URL", "product": "x", "version": "1",
        "status": "ok", "message": "still reportable", "eol_date": None,
        "days_remaining": None, "latest_patch": None, "source": source,
    }
    try:
        html, _ = format_report_html([result], [30, 60, 90], TODAY)
    finally:
        _URL_FNS.pop(source, None)
    assert "Broken URL" in html and "still reportable" in html


TESTS = [
    test_bounded_response_and_gzip_helpers,
    test_short_read_streams_are_read_to_limit,
    test_none_reading_stream_fails_loud,
    test_runtime_config_bounds,
    test_dispatch_isolates_provider_failures,
    test_malformed_provider_documents_return_error_rows,
    test_endoflife_urls_escape_config_values,
    test_provider_url_failures_do_not_break_html_reports,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print("OK test_provider_safety")
