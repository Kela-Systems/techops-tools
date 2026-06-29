"""Put the tool directory on sys.path so tests can `import raythink_app`,
`import raythink_camera`, etc. when pytest is run from the bench root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
