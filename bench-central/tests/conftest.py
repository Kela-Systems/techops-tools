import sys
from pathlib import Path

# The collector is a flat script-style deployable (no package); make it
# importable when pytest runs from bench-central/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
