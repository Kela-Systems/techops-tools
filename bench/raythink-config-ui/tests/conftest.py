"""Put the tool directory on sys.path so tests can `import raythink_app`,
`import raythink_camera`, etc. when pytest is run from the bench root."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# The shared bench password is no longer compiled into the code, so a pipeline
# under test has to get one from somewhere — exactly like a real station, which
# reads it from its gitignored config. Supplying it through the environment
# keeps every suite honest about that without putting a password in the repo.
@pytest.fixture(autouse=True)
def _shared_bench_password(monkeypatch):
    monkeypatch.setenv("KELA_NEW_PASSWORD", "test-shared-pw")
