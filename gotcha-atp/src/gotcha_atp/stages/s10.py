"""S10 — Operator attestations at the C2. All manual: asked on the Manual
steps screen whatever the automated stages found (the engineer's attestation
stands on its own); S10.4 is only offered when S6 flagged a radar 'quiet'."""
from ..model import RowSpec, StageSpec

STAGE = StageSpec(
    id="S10", name="Operator attestations", vantage="engineer at the C2",
    rows=(
        RowSpec("S10.1", "Map shows the unit and 4 radar sectors",
                "unit shown; 4 sectors at the declared (or calibrated) bearings, ~90° apart, no gap",
                "high", "manual",
                prompt="Open C2 with the radar debug layer on. Do you see the unit at its position "
                       "with four radar sectors?"),
        RowSpec("S10.2", "Live video visible in C2", "day + thermal tiles both live", "high", "manual",
                prompt="Are both video tiles (day and thermal) live and moving?"),
        RowSpec("S10.3", "Speaker audible via C2", "audible", "high", "manual",
                prompt="From the C2 speaker control, play the test file. Do you hear it from the "
                       "unit's speaker?"),
        RowSpec("S10.4", "Walk test (optional, offered when a radar was 'quiet')",
                "detections > 0; skipping is allowed and noted", "low", "manual",
                prompt="A radar saw no targets during the sample. Optional: walk through its sector "
                       "while we re-sample.",
                skippable=True, offer_if="quiet_radars"),
    ),
)
