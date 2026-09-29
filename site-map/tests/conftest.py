import sys
from pathlib import Path

# The tool is a flat set of modules (like bench-central and deploy-tracker),
# so tests import them off the package root rather than an installed dist.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def runs_in_tmp(tmp_path, monkeypatch):
    """No test writes into the real runs/ directory.

    Without this, running the suite buried the actual survey logs under a
    hundred fixtures - which defeats the point of keeping them.
    """
    import runlog
    # RUNS_DIR alone is enough now that RunLog resolves it per instance.
    # Patching RunLog.directory was the belt to this braces and did nothing:
    # the dataclass had already baked the default into its __init__, which is
    # why the HTTP tests kept writing into the real runs/ regardless.
    monkeypatch.setattr(runlog, "RUNS_DIR", tmp_path / "runs")
