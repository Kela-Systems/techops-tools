"""Put the tool directory on sys.path so tests can `import app`, `import
magos_bench`, etc. when pytest is run from the bench root (one shared venv)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
