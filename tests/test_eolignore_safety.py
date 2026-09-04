"""Tests for .eolignore containment and bounds (load_ignore_patterns).

Covers symlink/junction/reparse rejection, acceptance of non-link
reparse tags (cloud placeholders) via the realpath fallback,
escape-outside-root containment, the MAX_FILE_BYTES read bound,
unreadable-file handling, and that normal multi-pattern parsing is
unchanged. One bad .eolignore must warn, never abort a scan.
Standalone assertion script: no pytest, no network, no subprocesses.

Run from the repository root:  python tests/test_eolignore_safety.py
"""
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parents[1]
_HELPER_DIR = ROOT / "helper_scripts"
sys.path.insert(0, str(_HELPER_DIR))

import eol_inventory as gc
import eol_inventory.models as models_module
from eol_inventory.models import (
    MAX_FILE_BYTES,
    _is_link_or_reparse,
    _resolves_away_from_parent,
    load_ignore_patterns,
)


def _with_category(warnings, category):
    return [w for w in warnings if w["category"] == category]


def _link_warnings(warnings):
    return _with_category(warnings, "escaped_symlink")


def _make_junction(target, link):
    """Create a Windows junction; returns False when unavailable."""
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError, NotImplementedError):
        return False
    return link.exists()


def _make_symlink(target, link):
    """Create a symlink; returns False when unavailable."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    return link.exists()


# IO_REPARSE_TAG_MOUNT_POINT (junctions); stat exposes it only on Windows.
_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
# Simulated cloud-placeholder tag (OneDrive Files-On-Demand range).
_PLACEHOLDER_TAG = 0x9000001A
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _ReparseStat:
    """lstat result stand-in with overridden reparse tag/attributes."""

    def __init__(self, base, reparse_tag, file_attributes):
        self.st_mode = base.st_mode
        self.st_reparse_tag = reparse_tag
        self.st_file_attributes = file_attributes


def _patch_path_lstat(entry, reparse_tag, file_attributes):
    """While active, pathlib.Path.lstat reports fake reparse fields on
    entry only; other paths and st_mode keep their real values, so
    realpath containment checks keep working. Returns the restore
    callable.
    """
    real_lstat = Path.lstat
    entry_key = os.path.normcase(os.path.abspath(str(entry)))

    def fake_lstat(self, *args, **kwargs):
        st = real_lstat(self, *args, **kwargs)
        if os.path.normcase(os.path.abspath(str(self))) == entry_key:
            return _ReparseStat(st, reparse_tag, file_attributes)
        return st

    Path.lstat = fake_lstat

    def restore():
        Path.lstat = real_lstat

    return restore


def _reparse_attributes(entry):
    """Real lstat attributes for entry, plus the reparse-point bit."""
    return getattr(entry.lstat(), "st_file_attributes", 0) | _REPARSE_ATTRIBUTE


def test_load_ignore_patterns_normal_and_multiple():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".eolignore").write_text(
            "# generated output\n\nlegacy/\ndist\n*.log\nbuild/\n",
            encoding="utf-8")
        warnings = []
        patterns = load_ignore_patterns(root, warnings)
        assert patterns == ["legacy", "dist", "*.log", "build"]
        assert warnings == []
        assert load_ignore_patterns(root, []) == patterns


def test_load_ignore_patterns_absent_is_silent():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        warnings = []
        assert load_ignore_patterns(root, warnings) == []
        assert warnings == []


def test_load_ignore_patterns_rejects_symlink():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        real = outside / "real_ignore.txt"
        real.write_text("pom.xml\n", encoding="utf-8")
        if not _make_symlink(real, root / ".eolignore"):
            print("skip: symlink creation unavailable")
            return
        warnings = []
        assert load_ignore_patterns(root, warnings) == []
        link = _link_warnings(warnings)
        assert len(link) == 1
        assert link[0]["path"] == ".eolignore"
        assert "symlink" in link[0]["message"]
        assert _is_link_or_reparse(root / ".eolignore")


def test_load_ignore_patterns_rejects_junction_reparse():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        if not _make_junction(outside, root / ".eolignore"):
            print("skip: junction creation unavailable")
            return
        entry = root / ".eolignore"
        assert not entry.is_symlink()
        st = entry.lstat()
        assert getattr(st, "st_reparse_tag", 0)
        warnings = []
        assert load_ignore_patterns(root, warnings) == []
        link = _link_warnings(warnings)
        assert len(link) == 1
        assert link[0]["path"] == ".eolignore"
        assert "junction" in link[0]["message"]
        assert "reparse" in link[0]["message"]
        assert _is_link_or_reparse(entry)


def test_link_detection_realpath_fallback_layer():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        plain = root / "plain.txt"
        plain.write_text("x", encoding="utf-8")
        assert not _resolves_away_from_parent(plain)
        target = root / "elsewhere"
        target.mkdir()
        link = root / "linked-entry"
        made = _make_junction(target, link)
        if not made:
            made = _make_symlink(plain, link)
        if not made:
            print("skip: no link creation available")
            return
        assert _resolves_away_from_parent(link)
        assert _is_link_or_reparse(link)


def test_load_ignore_patterns_accepts_non_link_reparse_tag():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".eolignore").write_text("dist\n*.log\n", encoding="utf-8")
        entry = root / ".eolignore"
        restore = _patch_path_lstat(
            entry, _PLACEHOLDER_TAG, _reparse_attributes(entry))
        try:
            assert not _is_link_or_reparse(entry)
            warnings = []
            patterns = load_ignore_patterns(root, warnings)
        finally:
            restore()
        assert patterns == ["dist", "*.log"]
        assert warnings == []


def test_load_ignore_patterns_rejects_mount_point_tag():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".eolignore").write_text("dist\n", encoding="utf-8")
        entry = root / ".eolignore"
        restore = _patch_path_lstat(
            entry, _MOUNT_POINT_TAG, _reparse_attributes(entry))
        try:
            assert _is_link_or_reparse(entry)
            warnings = []
            assert load_ignore_patterns(root, warnings) == []
        finally:
            restore()
        link = _link_warnings(warnings)
        assert len(link) == 1
        assert link[0]["path"] == ".eolignore"
        assert "reparse" in link[0]["message"]


def test_link_tag_defeat_falls_back_to_realpath():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "real_ignore.txt").write_text("pom.xml\n", encoding="utf-8")
        link = root / ".eolignore"
        made = _make_junction(outside, link)
        if not made:
            made = _make_symlink(outside / "real_ignore.txt", link)
        if not made:
            print("skip: no link creation available")
            return
        plain_mode = not stat.S_ISLNK(link.lstat().st_mode)
        attributes = (
            getattr(link.lstat(), "st_file_attributes", 0)
            & ~_REPARSE_ATTRIBUTE)
        restore = _patch_path_lstat(link, 0, attributes)
        try:
            if plain_mode:
                assert not link.is_symlink()
            assert _is_link_or_reparse(link)
            warnings = []
            assert load_ignore_patterns(root, warnings) == []
        finally:
            restore()
        assert len(_link_warnings(warnings)) == 1


def test_load_ignore_patterns_rejects_escape_outside_root():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        real = outside / "real_ignore.txt"
        real.write_text("pom.xml\n", encoding="utf-8")
        if not _make_symlink(real, root / ".eolignore"):
            print("skip: symlink creation unavailable")
            return
        original = models_module._is_link_or_reparse
        models_module._is_link_or_reparse = lambda candidate: False
        try:
            warnings = []
            assert load_ignore_patterns(root, warnings) == []
        finally:
            models_module._is_link_or_reparse = original
        link = _link_warnings(warnings)
        assert len(link) == 1
        assert "outside the scan root" in link[0]["message"]


def test_load_ignore_patterns_oversize_bound():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".eolignore").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        warnings = []
        assert load_ignore_patterns(root, warnings) == []
        oversize = _with_category(warnings, "oversize_input")
        assert len(oversize) == 1
        assert oversize[0]["path"] == ".eolignore"
        assert str(MAX_FILE_BYTES) in oversize[0]["message"]


def test_load_ignore_patterns_unreadable_warning():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ignore = root / ".eolignore"
        ignore.write_text("legacy\n", encoding="utf-8")
        handle = None
        try:
            if os.name == "nt":
                import msvcrt
                handle = open(ignore, "r+b")
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                ignore.chmod(0)
                try:
                    with open(ignore, "rb") as probe:
                        probe.read(1)
                except OSError:
                    pass
                else:
                    print("skip: cannot make file unreadable (root?)")
                    return
            warnings = []
            assert load_ignore_patterns(root, warnings) == []
            unreadable = _with_category(warnings, "unreadable_ignore")
            assert len(unreadable) == 1
            assert unreadable[0]["path"] == ".eolignore"
        finally:
            if handle is not None:
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                handle.close()
            elif os.name != "nt":
                ignore.chmod(0o644)


def test_scan_folder_survives_bad_eolignore():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project"
        root.mkdir()
        (root / "pom.xml").write_text(_mini_pom("keep"))
        outside = base / "outside"
        outside.mkdir()
        (outside / "evil_ignore.txt").write_text(
            "pom.xml\n", encoding="utf-8")
        made = _make_junction(outside, root / ".eolignore")
        if not made:
            made = _make_symlink(outside / "evil_ignore.txt",
                                 root / ".eolignore")
        if not made:
            print("skip: no link creation available")
            return
        scan = gc.scan_folder(str(root))
        assert scan["files"] == ["pom.xml"]
        assert [r["artifact"] for r in scan["records"]] == ["keep"]
        assert _link_warnings(scan["warnings"])


def _mini_pom(artifact):
    return ("<project><dependencies><dependency>"
            f"<groupId>org.acme</groupId><artifactId>{artifact}</artifactId>"
            "<version>1.0.0</version></dependency></dependencies></project>")


TESTS = [
    test_load_ignore_patterns_normal_and_multiple,
    test_load_ignore_patterns_absent_is_silent,
    test_load_ignore_patterns_rejects_symlink,
    test_load_ignore_patterns_rejects_junction_reparse,
    test_link_detection_realpath_fallback_layer,
    test_load_ignore_patterns_accepts_non_link_reparse_tag,
    test_load_ignore_patterns_rejects_mount_point_tag,
    test_link_tag_defeat_falls_back_to_realpath,
    test_load_ignore_patterns_rejects_escape_outside_root,
    test_load_ignore_patterns_oversize_bound,
    test_load_ignore_patterns_unreadable_warning,
    test_scan_folder_survives_bad_eolignore,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    if failed:
        print(f"{failed} test(s) failed")
        return 1
    print("OK test_eolignore_safety")
    return 0


if __name__ == "__main__":
    sys.exit(main())
