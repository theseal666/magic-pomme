"""Put src/ on sys.path for test collection.

pyproject.toml's ``pythonpath`` setting only applies when pytest picks this
directory as its rootdir. A conftest.py is loaded whenever anything beneath it
is collected, so this keeps the tests importable even when pytest is run from
a parent directory and sweeps the project up incidentally.
"""

import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
