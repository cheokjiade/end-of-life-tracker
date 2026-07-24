import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECT = {
    "eol_config.c.json": 1,   # >= this many entries carry a policy_note
    "eol_config.d.json": 1,
    "eol_config.e.json": 1,
}
for fname, minimum in EXPECT.items():
    path = os.path.join(ROOT, fname)
    if not os.path.exists(path):
        print(f"skip {fname} (absent - gitignored local artifact)")
        continue
    raw = open(path, encoding="ascii").read()          # ascii open == cp1252-safe proof
    cfg = json.loads(raw)
    notes = [p for p in cfg["products"] if p.get("policy_note")]
    assert len(notes) >= minimum, f"{fname}: {len(notes)} notes, want >= {minimum}"
    for p in notes:
        assert p["policy_note"] == p["policy_note"].encode("ascii", "ignore").decode(), \
            f"{fname}: non-ASCII policy_note on {p.get('label')}"
print("OK test_configs_have_notes")
