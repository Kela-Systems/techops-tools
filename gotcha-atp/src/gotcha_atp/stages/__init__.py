"""The stage catalogue, in run order. Each module declares `STAGE` (its rows)
and, when implemented, `run(ctx)` yielding one Row per non-manual spec."""
from __future__ import annotations

from types import ModuleType
from typing import Optional

from ..model import RowSpec, StageSpec
from . import s0, s1, s2, s3, s4, s5, s6, s7, s8, s9, s10

MODULES: tuple[ModuleType, ...] = (s0, s1, s2, s3, s4, s5, s6, s7, s8, s9, s10)
STAGES: tuple[StageSpec, ...] = tuple(m.STAGE for m in MODULES)
BY_ID: dict[str, ModuleType] = {m.STAGE.id: m for m in MODULES}


def all_specs() -> list[tuple[StageSpec, RowSpec]]:
    return [(st, spec) for st in STAGES for spec in st.rows]


def find_row(row_id: str) -> Optional[tuple[StageSpec, RowSpec]]:
    for st, spec in all_specs():
        if spec.id == row_id:
            return st, spec
    return None


def _check_catalogue() -> None:
    ids = [spec.id for _, spec in all_specs()]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise RuntimeError(f"duplicate row ids in the catalogue: {sorted(dupes)}")


_check_catalogue()
