"""Put the tool directory on sys.path so tests can `import app`, `import
magos_bench`, etc. when pytest is run from the bench root (one shared venv)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_config_file(tmp_path, monkeypatch):
    """apply_settings persists to config/magos.config.json — point every
    configurator at a scratch file so no test rewrites the real one."""
    import app
    import apu_app
    monkeypatch.setattr(app.configurator, "config_path",
                        tmp_path / "magos.config.json")
    monkeypatch.setattr(apu_app.configurator, "config_path",
                        tmp_path / "magos.config.json")


# The shared bench password is no longer compiled into the code, so a pipeline
# under test has to get one from somewhere — exactly like a real station, which
# reads it from its gitignored config. Supplying it through the environment
# keeps every suite honest about that without putting a password in the repo.
@pytest.fixture(autouse=True)
def _shared_bench_password(monkeypatch):
    monkeypatch.setenv("KELA_NEW_PASSWORD", "test-shared-pw")
