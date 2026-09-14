import sys
from pathlib import Path

# The tool is a flat set of modules (like bench-central and deploy-tracker),
# so tests import them off the package root rather than an installed dist.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
