"""Phase 1 scope guard.

Fails (exit 1) if any scanned file name or file content refers to capabilities that
are explicitly OUT of Phase 1 scope (spec §3, §46.13):

* PlanFin (Phase 2)
* GL / general ledger / Microsoft Access integration
* PO creation or modification
* Payment workflow
* Writing back to HOSxP

The guard scans application code only (backend/src, and later backend/migrations and
frontend). Documentation is not scanned, because the spec itself must name these
exclusions.

Usage:
    python tools/validators/phase_guard.py            # scan default roots
    python tools/validators/phase_guard.py PATH ...   # scan given roots
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "src",
    REPO_ROOT / "backend" / "migrations",
    REPO_ROOT / "frontend" / "src",
    REPO_ROOT / "frontend" / "views",
    REPO_ROOT / "frontend" / "public",
)

SCANNED_SUFFIXES = {
    ".py",
    ".pyi",
    ".sql",
    ".toml",
    ".ini",
    ".cfg",
    ".json",
    ".yaml",
    ".yml",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".html",
    ".css",
    ".ejs",
    ".jinja",
    ".j2",
}

SKIPPED_DIRS = {"__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}

# (rule id, human description, compiled pattern). All case-insensitive.
FORBIDDEN: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("PLANFIN", "PlanFin is Phase 2", re.compile(r"plan[\s_\-]*fin", re.I)),
    (
        "GL",
        "GL / general ledger is out of scope",
        re.compile(r"general[\s_\-]*ledger|(?<![a-z0-9])gl(?:_|(?![a-z0-9]))", re.I),
    ),
    (
        "MSACCESS",
        "Microsoft Access integration is out of scope",
        re.compile(r"ms[\s_\-]*access|\.accdb\b|\.mdb\b", re.I),
    ),
    (
        "PO",
        "PO creation/modification is out of scope",
        re.compile(
            r"purchase[\s_\-]*order"
            r"|(?:create|new|make|update|modify|edit)[\s_\-]*po(?![a-z0-9])"
            r"|(?<![a-z0-9])po[\s_\-]*(?:creat|modif|updat|edit)",
            re.I,
        ),
    ),
    ("PAYMENT", "Payment workflow is out of scope", re.compile(r"payment", re.I)),
    (
        "HOSXP_WRITE",
        "Direct modification of HOSxP is out of scope",
        re.compile(
            r"write[\s_\-]*back|(?:write|update|insert|delete)[\s_\-]*(?:to[\s_\-]*)?hosxp", re.I
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int  # 0 = file/dir name
    rule: str
    description: str
    excerpt: str

    def render(self, base: Path) -> str:
        try:
            shown = self.path.relative_to(base)
        except ValueError:
            shown = self.path
        where = f"{shown}:{self.line}" if self.line else f"{shown} (name)"
        return f"{where}: [{self.rule}] {self.description}: {self.excerpt!r}"


def _iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIPPED_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def scan(roots: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for root in roots:
        for path in _iter_files(root):
            for rule, desc, pat in FORBIDDEN:
                m = pat.search(path.name)
                if m:
                    findings.append(Finding(path, 0, rule, desc, m.group(0)))
            if not path.is_file() or path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for rule, desc, pat in FORBIDDEN:
                    m = pat.search(line)
                    if m:
                        findings.append(Finding(path, lineno, rule, desc, m.group(0)))
    return findings


def main(argv: list[str]) -> int:
    roots = [Path(a).resolve() for a in argv] if argv else list(DEFAULT_ROOTS)
    findings = scan(roots)
    scanned = [r for r in roots if r.exists()]
    if findings:
        print(f"phase_guard: FAIL - {len(findings)} out-of-scope reference(s):")
        for f in findings:
            print("  " + f.render(REPO_ROOT))
        return 1
    print(f"phase_guard: PASS - no out-of-scope references in {len(scanned)} root(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
