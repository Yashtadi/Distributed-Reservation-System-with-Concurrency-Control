import sys
from pathlib import Path


# Ensure repo root is importable for tests (so `import server` works)
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

