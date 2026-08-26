"""Integrity check for agent-facing docs and skills.

Standalone assertion script (repo convention: no framework, no network).
Verifies: required deliverables exist; backticked repo-path references in the
agent docs resolve; skill/agent frontmatter carries name + description; no
placeholder text; the provider-repair section exists.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCS = [
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "docs" / "adding-a-provider.md",
    ROOT / "docs" / "commit-conventions.md",
    ROOT / "docs" / "updating-a-config.md",
]
CLAUDE_SKILLS = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
PORTABLE_SKILLS = sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md"))
SKILLS = CLAUDE_SKILLS + PORTABLE_SKILLS
AGENTS = sorted((ROOT / ".claude" / "agents").glob("*.md"))

# 1. Required deliverables exist
required = DOCS + [
    ROOT / ".claude" / "skills" / name / "SKILL.md"
    for name in ("eol-config", "add-eol-provider")
] + [ROOT / ".agents" / "skills" / "manage-eol-config" / "SKILL.md"]
missing = [str(p) for p in required if not p.exists()]
assert not missing, f"missing deliverables: {missing}"

# 2. Backticked repo-path references resolve (skip <templates>, globs, commands)
path_re = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*\.(?:md|py|json|sh|ps1|tf))`")
dangling = []
for doc in DOCS + SKILLS + AGENTS:
    text = doc.read_text(encoding="utf-8")
    for ref in sorted(set(path_re.findall(text))):
        if ref in {"package.json", "pom.xml", "build.gradle"}:
            continue
        if any(ch in ref for ch in "<>*"):
            continue
        if not (ROOT / ref).exists():
            dangling.append(f"{doc.name}: {ref}")
assert not dangling, "dangling path references:\n" + "\n".join(dangling)

# 3. Frontmatter: skills and agents carry name + description
fm_re = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
for f in SKILLS + AGENTS:
    m = fm_re.match(f.read_text(encoding="utf-8"))
    assert m, f"{f}: missing frontmatter"
    assert re.search(r"^name:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks name"
    assert re.search(r"^description:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks description"
    if f.name == "SKILL.md":
        name_match = re.search(r"^name:\s*[\"']?([^\s\"']+)", m.group(1), re.M)
        assert name_match.group(1) == f.parent.name, f"{f}: skill name must match directory"

# 4. No placeholder text in the agent-facing docs/skills
for f in DOCS + SKILLS:
    text = f.read_text(encoding="utf-8")
    assert not re.search(r"\bTBD\b|\bTODO\b", text), f"{f}: placeholder text"

# 5. Landmark sections exist
assert "## Repairing a broken provider" in (ROOT / "docs" / "adding-a-provider.md").read_text(encoding="utf-8")
assert "## Workflows index" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
assert "## Git workflow and commits" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
assert "## Commit message format" in (ROOT / "docs" / "commit-conventions.md").read_text(encoding="utf-8")
assert "## Update mode" in (ROOT / ".claude" / "agents" / "eol-config-extractor.md").read_text(encoding="utf-8")
assert ".agents/skills/manage-eol-config/SKILL.md" in (
    ROOT / ".claude" / "skills" / "eol-config" / "SKILL.md"
).read_text(encoding="utf-8")

print("check_agent_docs: OK")
