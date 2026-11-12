"""Load workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalise import normalise_header


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    fields: tuple[Field, ...]
    _header_map: dict[str, Field] = field(default_factory=dict, compare=False, repr=False)

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def pii_fields(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.pii)

    def match_header(self, raw_header: str) -> Field | None:
        """Field whose name or alias equals the normalised header, else None (column ignored)."""
        return self._header_map.get(normalise_header(raw_header))


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def _parse_workflow(doc: dict) -> Workflow:
    fields = tuple(
        Field(
            name=f["name"], type=f["type"], required=f.get("required", False),
            pii=f.get("pii", False), reference=f.get("reference", False),
            aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
            default=f.get("default"),
        )
        for f in doc["fields"]
    )
    if sum(f.reference for f in fields) != 1:
        raise ValueError(f"{doc['workflow_id']}: exactly one field must be marked reference")
    if sum(f.type == "phone" for f in fields) != 1:
        raise ValueError(f"{doc['workflow_id']}: exactly one phone field is required")
    header_map: dict[str, Field] = {}
    for f in fields:  # schema order: first field wins if two claim the same header
        for label in (f.name, *f.aliases):
            header_map.setdefault(normalise_header(label), f)
    return Workflow(doc["workflow_id"], fields, header_map)


def load_workflows(schema_dir: Path) -> dict[str, Workflow]:
    out: dict[str, Workflow] = {}
    for path in sorted(Path(schema_dir).glob("*.json")):
        wf = _parse_workflow(json.loads(path.read_text(encoding="utf-8-sig")))
        out[wf.workflow_id] = wf
    return out


def load_dispositions(csv_path: Path) -> list[Disposition]:
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        return [
            Disposition(r["workflow_id"], r["code"], r["label"],
                        r["is_terminal"].strip().lower() == "true",
                        r["is_success"].strip().lower() == "true")
            for r in csv.DictReader(fh)
        ]
