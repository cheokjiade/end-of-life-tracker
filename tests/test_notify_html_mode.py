"""html_file local-vs-Lambda behaviour tests (audit R-12) - network-free.

Covers: local mode keeping the relative reports/ layout, Lambda mode skipping
relative (and non-/tmp absolute) destinations instead of attempting writes,
Lambda mode honouring an explicit destination under /tmp, and traversal
attempts being rejected lexically. No real /tmp access is needed: tests
retarget notify.LAMBDA_TMP_ROOT to a temp dir.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eoltracker.notify as notify


def plan(path, in_lambda):
    return notify._html_output_plan(path, in_lambda)


# --- pure decision matrix ----------------------------------------------------
skip, root = plan("eol_report.html", False)
assert skip is None and root == "reports", (skip, root)

skip, root = plan("eol_report.html", True)
assert skip is not None and root is None, (skip, root)
assert "/tmp" in skip, skip

skip, root = plan("/opt/out/eol_report.html", True)
assert skip is not None and root is None, (skip, root)
skip, root = plan("\\Windows\\Temp\\r.html", True)   # backslashes never qualify
assert skip is not None and root is None, (skip, root)
skip, root = plan("/tmpfoo/r.html", True)            # prefix look-alike rejected
assert skip is not None and root is None, (skip, root)
skip, root = plan("/tmp", True)                       # root is not a file path
assert skip is not None and root is None, (skip, root)
skip, root = plan("/tmp/../etc/r.html", True)        # traversal rejected
assert skip is not None and root is None, (skip, root)
skip, root = plan("", True)
assert skip is not None and root is None, (skip, root)

skip, root = plan("/tmp/reports/eol_report_a.html", True)
assert skip is None and root == "/tmp/reports", (skip, root)
print("OK html output decision matrix")

# --- end-to-end: local mode keeps writing reports/<project>/Y/M/D -------------
original_cwd = os.getcwd()
sandbox = tempfile.mkdtemp(prefix="eol_local_html_")
try:
    os.chdir(sandbox)
    out = notify.send_notifications(
        {"notifications": [{"type": "html_file", "path": "eol_report_a.html"}]},
        "t", "<html/>", "s")
    o = out[0]
    assert o["delivered"] is True and o["skipped"] is False, o
    assert o["output"], o
    written = [
        os.path.join(dirpath, fn)
        for dirpath, _dirs, files in os.walk(os.path.join(sandbox, "reports"))
        for fn in files
    ]
    assert len(written) == 1, written
    rel = os.path.relpath(written[0], sandbox)
    parts = rel.split(os.sep)
    # reports/a/<year>/<month>/<day>/eol_report_a_<stamp>.html
    assert parts[0] == "reports" and parts[1] == "a", rel
    assert len(parts) == 6, rel
    assert all(p.isdigit() for p in parts[2:5]), rel
    assert parts[5].startswith("eol_report_a_") and parts[5].endswith(".html"), rel
    with open(written[0], encoding="utf-8") as f:
        assert f.read() == "<html/>"
finally:
    os.chdir(original_cwd)
    shutil.rmtree(sandbox, ignore_errors=True)
print("OK local mode report layout preserved")

# --- end-to-end: Lambda mode skips relative path, writes nothing -------------
os.environ[notify.LAMBDA_MARKER_ENV] = "test-fn"
sandbox = tempfile.mkdtemp(prefix="eol_lambda_skip_")
try:
    os.chdir(sandbox)
    before = sorted(os.listdir("."))
    out = notify.send_notifications(
        {"notifications": [{"type": "html_file", "path": "eol_report.html"}]},
        "t", "<html/>", "s")
    o = out[0]
    assert o["delivered"] is False and o["attempted"] is False and o["skipped"] is True, o
    assert "local-only" in o["detail"], o["detail"]
    assert sorted(os.listdir(".")) == before, "nothing may be written in cwd"
finally:
    os.chdir(original_cwd)
    shutil.rmtree(sandbox, ignore_errors=True)
    del os.environ[notify.LAMBDA_MARKER_ENV]
print("OK Lambda mode rejects relative html_file destination")

# --- end-to-end: explicit tmp destination delivers, files land under it -------
os.environ[notify.LAMBDA_MARKER_ENV] = "test-fn"
sandbox = tempfile.mkdtemp(prefix="eol_lambda_ok_")
posix_root = sandbox.replace("\\", "/")
old_root = notify.LAMBDA_TMP_ROOT
notify.LAMBDA_TMP_ROOT = posix_root
try:
    out = notify.send_notifications(
        {"notifications": [{"type": "html_file",
                            "path": posix_root + "/out/eol_report_b.html"}]},
        "t", "<html/>", "s")
    o = out[0]
    assert o["delivered"] is True and o["skipped"] is False, o
    assert o["output"], o
    written = []
    for dirpath, _dirs, files in os.walk(sandbox):
        for fn in files:
            written.append(os.path.join(dirpath, fn))
    assert len(written) == 1, written
    # project segment 'b' + dated folders derived from base name/time.
    parts = os.path.relpath(written[0], sandbox).split(os.sep)
    assert "b" in parts, parts
finally:
    notify.LAMBDA_TMP_ROOT = old_root
    del os.environ[notify.LAMBDA_MARKER_ENV]
    shutil.rmtree(sandbox, ignore_errors=True)
print("OK Lambda mode honours explicit /tmp destination")

print("OK test_notify_html_mode")
