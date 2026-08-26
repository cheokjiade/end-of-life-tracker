"""Integrity check for agent-facing docs and skills.

Standalone assertion script (repo convention: no framework, no network).
Verifies: canonical portable skills and thin Claude loaders exist; referenced
repo paths resolve; skill names are valid and unique; Claude does not retain
duplicate workflow logic or a private extractor agent; landmark policies exist.
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
CLAUDE_SKILLS = sorted((ROOT / ".claude" / "skills").rglob("SKILL.md"))
PORTABLE_SKILLS = sorted((ROOT / ".agents" / "skills").rglob("SKILL.md"))
SKILLS = CLAUDE_SKILLS + PORTABLE_SKILLS
CLAUDE_AGENTS = sorted((ROOT / ".claude" / "agents").rglob("*.md"))

# 1. Required deliverables exist
required = DOCS + [
    ROOT / ".claude" / "skills" / name / "SKILL.md"
    for name in ("eol-config", "eol-provider")
] + [
    ROOT / ".agents" / "skills" / name / "SKILL.md"
    for name in ("manage-eol-config", "add-eol-provider")
]
missing = [str(p) for p in required if not p.exists()]
assert not missing, f"missing deliverables: {missing}"
assert {"eol-config", "eol-provider"} <= {p.parent.name for p in CLAUDE_SKILLS}
assert {"manage-eol-config", "add-eol-provider"} <= {p.parent.name for p in PORTABLE_SKILLS}
assert not (ROOT / ".claude" / "agents" / "eol-config-extractor.md").exists()

# 2. Backticked repo-path references resolve (skip <templates>, globs, commands)
path_re = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*\.(?:md|py|json|sh|ps1|tf))`")
dangling = []
for doc in DOCS + SKILLS + CLAUDE_AGENTS:
    text = doc.read_text(encoding="utf-8")
    for ref in sorted(set(path_re.findall(text))):
        if ref in {"package.json", "pom.xml", "build.gradle"}:
            continue
        if any(ch in ref for ch in "<>*"):
            continue
        base = doc.parent if doc in SKILLS and ref.startswith("../") else ROOT
        if not (base / ref).resolve().exists():
            dangling.append(f"{doc.name}: {ref}")
assert not dangling, "dangling path references:\n" + "\n".join(dangling)

# 3. Frontmatter: skills and agents carry name + description
fm_re = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
skill_names = []
for f in SKILLS + CLAUDE_AGENTS:
    m = fm_re.match(f.read_text(encoding="utf-8"))
    assert m, f"{f}: missing frontmatter"
    assert re.search(r"^name:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks name"
    assert re.search(r"^description:\s*\S", m.group(1), re.M), f"{f}: frontmatter lacks description"
    if f.name == "SKILL.md":
        name_match = re.search(r"^name:\s*[\"']?([^\s\"']+)", m.group(1), re.M)
        assert name_match.group(1) == f.parent.name, f"{f}: skill name must match directory"
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name_match.group(1)), f"{f}: invalid portable skill name"
        skill_names.append(name_match.group(1))
assert len(skill_names) == len(set(skill_names)), f"duplicate skill names: {skill_names}"

# 4. No placeholder text in the agent-facing docs/skills
for f in DOCS + SKILLS:
    text = f.read_text(encoding="utf-8")
    assert not re.search(r"\bTBD\b|\bTODO\b", text), f"{f}: placeholder text"

# 5. Landmark sections exist
assert "## Repairing a broken provider" in (ROOT / "docs" / "adding-a-provider.md").read_text(encoding="utf-8")
assert "## Workflows index" in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
commit_text = (ROOT / "docs" / "commit-conventions.md").read_text(encoding="utf-8")
assert "## Git workflow and commits" in agents_text
assert "Do not run routine post-commit audits" in agents_text
assert "For bug-fix batches only" in agents_text
assert "fresh, read-only adversarial subagent" in agents_text
assert "## Commit message format" in commit_text
assert "## Adversarial review for bug fixes" in commit_text
assert "Dispatch exactly one fresh, read-only adversarial subagent" in commit_text
assert "attempt to disprove the fix" in commit_text
assert "new follow-up commit" in commit_text
assert "do not amend the reviewed commit" in commit_text
assert "the required review was not performed" in commit_text
assert "### If a secret is committed" in commit_text

# 6. Claude loaders are aliases, not duplicate workflows
loader_targets = {
    "eol-config": "../../../.agents/skills/manage-eol-config/SKILL.md",
    "eol-provider": "../../../.agents/skills/add-eol-provider/SKILL.md",
}
for name, target in loader_targets.items():
    loader = (ROOT / ".claude" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert target in loader, f"{name}: missing canonical target"
    assert len(loader.splitlines()) <= 15, f"{name}: loader contains copied workflow logic"
    assert "## Workflow" not in loader and "## Verify" not in loader

# 7. Canonical skills retain the capabilities formerly split across Claude files
manage_text = (ROOT / ".agents" / "skills" / "manage-eol-config" / "SKILL.md").read_text(encoding="utf-8")
provider_text = (ROOT / ".agents" / "skills" / "add-eol-provider" / "SKILL.md").read_text(encoding="utf-8")
for landmark in ("**Generate:**", "**Update:**", "Live-check every new or changed", "pre-existing versus newly introduced error rows", "inputs intentionally skipped"):
    assert landmark in manage_text, f"manage-eol-config missing capability: {landmark}"
for landmark in ("**Add:**", "**Repair:**", "defensive-parsing", "network-free", "live smoke"):
    assert landmark in provider_text, f"add-eol-provider missing capability: {landmark}"
for name, text in (("manage-eol-config", manage_text), ("add-eol-provider", provider_text)):
    assert "repository root is three directories above" in text, f"{name}: repository root is undefined"
    assert "Run all repository commands from that root" in text, f"{name}: command working directory is undefined"

agent_facing = "\n".join(f.read_text(encoding="utf-8") for f in DOCS + SKILLS)
assert "eol-config-extractor" not in agent_facing
assert "E:\\Git\\endoflife" not in agent_facing

print("check_agent_docs: OK")
