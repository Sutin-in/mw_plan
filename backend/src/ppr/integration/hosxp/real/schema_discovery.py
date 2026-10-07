"""Read-only HOSxP schema discovery: table and column NAMES only, never row data.

Used once per site to learn the real schema (spec §47 evidence) before anyone writes the
site query file. It reads database metadata only (``information_schema`` through
SQLAlchemy's inspector); it never selects from a table, so no patient or business data
can appear in its output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine, inspect


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    comment: str | None


@dataclass(frozen=True)
class TableInfo:
    name: str
    kind: str  # "table" or "view"
    comment: str | None
    columns: tuple[ColumnInfo, ...]


def discover(engine: Engine, match: Iterable[str], *, schema: str | None = None) -> list[TableInfo]:
    """Tables/views whose name, or any column name, contains one of ``match`` (case-
    insensitive). An empty ``match`` lists everything."""
    words = [m.lower() for m in match if m.strip()]
    insp = inspect(engine)
    names = [(n, "table") for n in insp.get_table_names(schema=schema)] + [
        (n, "view") for n in insp.get_view_names(schema=schema)
    ]
    out: list[TableInfo] = []
    for name, kind in sorted(names):
        cols = insp.get_columns(name, schema=schema)
        col_names = [str(c["name"]) for c in cols]
        hay = [name.lower(), *(c.lower() for c in col_names)]
        if words and not any(w in h for w in words for h in hay):
            continue
        try:
            comment = insp.get_table_comment(name, schema=schema).get("text")
        except NotImplementedError:
            comment = None
        out.append(
            TableInfo(
                name,
                kind,
                comment,
                tuple(
                    ColumnInfo(
                        str(c["name"]),
                        str(c["type"]),
                        bool(c.get("nullable", True)),
                        c.get("comment"),
                    )
                    for c in cols
                ),
            )
        )
    return out


def to_markdown(tables: list[TableInfo], *, title: str) -> str:
    lines = [f"# {title}", "", "Metadata only: table and column names/types. No row data.", ""]
    for t in tables:
        head = f"## {t.name} ({t.kind})"
        lines += [head, ""] + ([f"_{t.comment}_", ""] if t.comment else [])
        lines += ["| column | type | nullable | comment |", "|---|---|---|---|"]
        lines += [
            f"| {c.name} | {c.type} | {'yes' if c.nullable else 'no'} | {c.comment or ''} |"
            for c in t.columns
        ]
        lines.append("")
    lines.append(f"_{len(tables)} table(s)/view(s)._")
    return "\n".join(lines) + "\n"
