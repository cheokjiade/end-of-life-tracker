import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from eoltracker.parsers import check_product

TODAY = date(2026, 7, 24)

# 1. A truthy policy_note is copied onto the result (manual provider = no network).
entry = {"source": "manual", "label": "PuTTY", "policy_note": "Only newest release supported."}
r = check_product(entry, TODAY)
assert r is not None
assert r.get("policy_note") == "Only newest release supported.", r.get("policy_note")

# 2. No policy_note in the entry -> key absent on the result.
r2 = check_product({"source": "manual", "label": "X"}, TODAY)
assert "policy_note" not in r2, r2

# 3. An empty policy_note is treated as absent.
r3 = check_product({"source": "manual", "label": "Y", "policy_note": ""}, TODAY)
assert "policy_note" not in r3, r3

# 4. Section dividers still return None (and don't crash on the note copy).
assert check_product({"_section": "=== Group ==="}, TODAY) is None

# 5. The unknown-source error path also carries the note (injection is uniform).
r5 = check_product({"source": "nope", "policy_note": "x"}, TODAY)
assert r5 is not None
assert r5.get("policy_note") == "x", r5

print("OK test_policy_injection")
