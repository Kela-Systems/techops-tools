from __future__ import annotations

from typing import Callable, Iterable, Iterator

from ..access.exec import AccessError
from ..access.grpc import Hub
from ..context import Context
from ..model import Row, StageSpec, amber


def hub(ctx: Context) -> Hub:
    """The run's one hub connection (S3 opens it, S4–S6 reuse it); closed by
    Context.close_devices at the end of the run."""
    h = ctx.facts.get("_hub")
    if h is None:
        h = Hub(ctx.session)
        ctx.facts["_hub"] = h
    return h


def grpc_error(e: Exception) -> str:
    """'CODE: details' for a grpc error, the exception text otherwise."""
    code, details = getattr(e, "code", None), getattr(e, "details", None)
    if callable(code) and callable(details):
        return f"{code().name}: {details()}"
    return f"{type(e).__name__}: {e}"


def guarded(ctx: Context, stage: StageSpec,
            fns: Iterable[Callable[[Context], Row]]) -> Iterator[Row]:
    """Run one function per declared automated row, in declaration order
    (manual rows are asked by the runner). An exception in one row makes that
    row amber and the next row still runs — an engineer sees everything that
    is wrong at once."""
    fns = list(fns)
    rows = [s for s in stage.rows if s.cls != "manual"]
    if len(fns) != len(rows):
        raise ValueError(f"{stage.id}: {len(fns)} row functions for {len(rows)} automated rows")
    for spec, fn in zip(rows, fns):
        if ctx.cancel.is_set():
            yield amber(spec, "run cancelled")
            continue
        try:
            yield fn(ctx)
        except AccessError as e:
            yield amber(spec, ctx.redact(str(e)))
        except Exception as e:  # noqa: BLE001 — a bug in one row must not hide the rest
            yield amber(spec, ctx.redact(f"engine error: {type(e).__name__}: {e}"))
