"""Shared test setup.

These tests cover only pure logic (no browser, no network, no store accounts),
so they run anywhere in well under a second.
"""

import sys
from pathlib import Path

# Import the app the same way main.py does, from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
