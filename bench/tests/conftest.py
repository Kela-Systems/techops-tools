import sys
from pathlib import Path


# updater.py lives in bench/scripts/ as a flat script (it must run before the
# venv exists); make it importable when pytest runs from bench/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
