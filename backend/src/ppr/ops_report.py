"""Check reports for IT (Wave 10B-2): one line per check, printed and optionally saved as
Markdown evidence for the go-live checklist (``docs/GO_LIVE_CHECKLIST.md``).

States (D-40):

* ``PASS`` - verified now, by this command;
* ``FAIL`` - wrong; must be fixed before go-live (the command exits 1);
* ``WARN`` - allowed, but must be read and dealt with (e.g. MOCK/DEMO data in production);
* ``OPEN`` - cannot be verified yet: it needs evidence from HOSxP / IT or a decision. An OPEN
  item is never shown, counted or summarised as PASS;
* ``TODO`` - a step still to do (first start), not an error;
* ``INFO`` - information only.

Reports carry counts, column names and the identifiers the operator typed - never names of
people, passwords or connection secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATES = ("PASS", "FAIL", "WARN", "OPEN", "TODO", "INFO")


@dataclass
class CheckReport:
    title: str
    lines: list[tuple[str, str, str]] = field(default_factory=list)  # (state, area, text)

    def add(self, state: str, area: str, text: str) -> None:
        assert state in STATES, state
        self.lines.append((state, area, text))
        print(f"{state:<5} [{area}] {text}")  # noqa: T201 - a command-line report

    def count(self, state: str) -> int:
        return sum(1 for s, _, _ in self.lines if s == state)

    @property
    def failed(self) -> bool:
        return self.count("FAIL") > 0

    def summary(self) -> str:
        """Overall result. OPEN / WARN / TODO are named, never folded into PASS."""
        if self.failed:
            head = "FAIL"
        else:
            extras = [f"{self.count(s)} {s}" for s in ("WARN", "OPEN", "TODO") if self.count(s)]
            head = "PASS" if not extras else "NO FAIL - " + ", ".join(extras) + " (not PASS)"
        return f"{self.title}: {head}"

    def finish(self, out: Path | None) -> int:
        print(self.summary())  # noqa: T201
        if out is not None:
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            rows = [f"| {s} | {a} | {t.replace('|', '/')} |" for s, a, t in self.lines]
            open_rows = [r for r in self.lines if r[0] == "OPEN"]
            text = [
                f"# {self.title}",
                "",
                f"- Run: {stamp}",
                f"- Result: **{self.summary().split(': ', 1)[1]}**",
                "",
                "| State | Area | Check |",
                "|---|---|---|",
                *rows,
                "",
                "## OPEN items (not PASS)",
                "",
                *(
                    [f"- [{a}] {t}" for _, a, t in open_rows]
                    if open_rows
                    else ["- none reported by this command"]
                ),
                "",
            ]
            out.write_text("\n".join(text), encoding="utf-8")
            print(f"report written to {out}")  # noqa: T201
        return 1 if self.failed else 0
