"""Make the flat module layout importable from the tests.

The CLI is a set of top-level modules that import each other by bare name
(`import pricing`), which is how it runs as `python3 cc_audit.py`. Putting the
repo root on the path keeps the tests importing exactly what ships, rather than
a repackaged copy that could drift from it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
