"""Static Terraform deployment-safety checks (issue #5 remediation).

Standalone assertion script (repo convention: no framework, no network).
Parses the .tf sources and .gitignore with regexes to assert:

1. every provider used by resource/data types is declared in
   required_providers, and each constraint is a narrow three-component
   pessimistic pin (no bare "~> X.0" drift ranges);
2. S3 versioning stays enabled on the config bucket and nothing expires
   noncurrent versions (the rollback mechanism documented in
   terraform/README.md depends on it);
3. the config-object version IDs remain exposed as a stack output;
4. the dependency lock file is committable (.gitignore) and - when present -
   actually pins the declared providers at constraint-compatible versions.

Run from the repository root: python tests/check_terraform_infra.py
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TF = os.path.join(ROOT, "terraform")


def read(name):
    with open(os.path.join(TF, name), encoding="utf-8") as f:
        return f.read()


def tf_files():
    for name in sorted(os.listdir(TF)):
        if name.endswith(".tf"):
            yield name, read(name)


# --- 1. provider declarations ------------------------------------------------
declared = {}
for name, text in tf_files():
    block = re.search(
        r"required_providers\s*\{(.*?)\n  \}", text, re.S)
    if not block:
        continue
    body = block.group(1)
    for m in re.finditer(
        r'(\w+)\s*=\s*\{[^{}]*?source\s*=\s*"([^"]+)"[^{}]*?'
        r'version\s*=\s*"([^"]+)"',
        body,
        re.S,
    ):
        local_name, source_addr, constraint = m.groups()
        assert "/".join(source_addr.split("/")[-2:]) == \
            f"hashicorp/{local_name}", (
                name, source_addr, local_name)
        declared[local_name] = constraint

assert set(declared) == {"aws"}, declared
for prov, constraint in declared.items():
    # narrow = three-component pessimistic pin: allows only patch releases of
    # one minor line. Bare "~> 5.0"-style ranges are rejected as too broad.
    assert re.fullmatch(r"~> \d+\.\d+\.\d+", constraint), (prov, constraint)

used_prefixes = set()
for name, text in tf_files():
    # resource/data types are "<provider>_<resource>[_...]"; the leading
    # segment is the provider local name.
    used_prefixes.update(
        t.split("_", 1)[0] for t in re.findall(
            r'(?:resource|data)\s+"([^"]+)"', text))
# terraform_data is a built-in Terraform resource, not a registry provider.
used_prefixes.discard("terraform")
undeclared = used_prefixes - set(declared)
assert not undeclared, f"provider types used but not declared: {undeclared}"

# --- 2. S3 versioning on the config bucket -----------------------------------
main_tf = read("main.tf")
m = re.search(
    r'resource\s+"aws_s3_bucket_versioning"\s+"config"\s*\{(.*?)\n\}',
    main_tf,
    re.S,
)
assert m, "aws_s3_bucket_versioning 'config' resource missing"
assert re.search(r"bucket\s*=\s*aws_s3_bucket\.config\.id", m.group(1))
assert re.search(r'status\s*=\s*"Enabled"', m.group(1))
assert 'resource "terraform_data" "validate_eol_config"' in main_tf
assert "lambda_function.py" in main_tf and "--validate" in main_tf
object_block = re.search(
    r'resource\s+"aws_s3_object"\s+"eol_config"\s*\{(.*?)\n\}',
    main_tf,
    re.S,
)
assert object_block and "aws_s3_bucket_versioning.config" in object_block.group(1)
assert "terraform_data.validate_eol_config" in object_block.group(1)

for name, text in tf_files():
    assert "noncurrent_version_expiration" not in text, (
        f"{name}: expiring noncurrent versions would destroy rollback data "
        "documented in terraform/README.md")

# --- 3. object version IDs stay exposed --------------------------------------
outputs_tf = read("outputs.tf")
m = re.search(r'output\s+"config_object_version_ids"\s*\{(.*?)\n\}',
              outputs_tf, re.S)
assert m, "config_object_version_ids output missing"
assert "o.version_id" in m.group(1)

# --- 4. lockfile handling ------------------------------------------------------
gitignore_path = os.path.join(ROOT, ".gitignore")
with open(gitignore_path, encoding="utf-8") as f:
    gitignore = f.read()
for still_ignored in (".terraform/", "terraform.tfstate",
                      "terraform.tfvars", "lambda.zip", "candidate*.json"):
    assert still_ignored in gitignore, still_ignored
# unignored since issue #5: the lock file should be committed, not filtered out
assert ".terraform.lock.hcl" not in gitignore
lock_path = os.path.join(TF, ".terraform.lock.hcl")
if os.path.exists(lock_path):
    with open(lock_path, encoding="utf-8") as f:
        lock_text = f.read()
    locked = {}
    for m in re.finditer(
        r'provider\s+"([^"]+)"\s*\{(.*?)\n\}', lock_text, re.S
    ):
        addr, body = m.groups()
        ver = re.search(r'version\s*=\s*"([^"]+)"', body)
        hashes = re.findall(r'"h1:[^"]+"', body)
        registry_hashes = re.findall(r'"zh:[^"]+"', body)
        assert addr not in locked, f"duplicate provider block {addr}"
        assert ver, f"missing version in {addr}"
        assert hashes, f"no h1 checksums recorded for {addr}"
        assert registry_hashes, f"no signed-registry zh checksums recorded for {addr}"
        locked[addr] = ver.group(1)
    for prov, constraint in declared.items():
        addr = f"registry.terraform.io/hashicorp/{prov}"
        assert addr in locked, f"lockfile missing required provider {addr}"
        floor = constraint.removeprefix("~> ").split(".")
        major, minor, patch = (int(p) for p in floor)
        v_major, v_minor, v_patch = (
            int(p) for p in locked[addr].split("."))
        lo = (v_major, v_minor, v_patch) >= (major, minor, patch)
        hi = (v_major, v_minor) < (major, minor + 1)
        assert lo and hi, (
            f"locked {addr} {locked[addr]} violates constraint {constraint}")
    print("check_terraform_infra: lockfile OK "
          f"({', '.join(sorted(locked))})")
else:
    print("check_terraform_infra: SKIP - .terraform.lock.hcl not generated yet; "
          "run terraform init once and commit it (see terraform/README.md)")

print("OK check_terraform_infra")
