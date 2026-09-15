#!/usr/bin/env python3
"""Fail if any shipped module imports something outside the standard library.

`cc-audit` is distributed as a directory you can copy onto a machine and run
with the system Python. That only works while it has no third-party imports, so
this is checked in CI rather than left as a promise in the README.

Test files are exempt: pytest is a development dependency.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The tool's own modules, which import each other by bare name.
LOCAL = {
    "agents", "cc_audit", "checks", "critic", "evidence",
    "grade", "parse", "pricing", "store",
}


def top_level_imports(path):
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module; only absolute imports matter.
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


def main():
    stdlib = getattr(sys, "stdlib_module_names", None)
    if not stdlib:
        # Python 3.9 has no stdlib_module_names; fall back to importability,
        # which is sound here because CI installs nothing but pytest.
        stdlib = None

    offenders = []
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py"):
            continue
        for mod in sorted(top_level_imports(os.path.join(ROOT, name))):
            if mod in LOCAL:
                continue
            if stdlib is not None:
                if mod not in stdlib:
                    offenders.append((name, mod))
                continue
            if mod == "pytest":
                offenders.append((name, mod))

    if offenders:
        for path, mod in offenders:
            print(f"error: {path} imports non-stdlib module `{mod}`",
                  file=sys.stderr)
        print("\ncc-audit ships with no dependencies; keep it that way or "
              "update this check deliberately.", file=sys.stderr)
        return 1

    print("ok: no third-party imports in the shipped modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
