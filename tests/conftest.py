import sys
from pathlib import Path

# Make headloss.py importable even when pytest is run without pytest.ini.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
