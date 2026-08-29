#!/usr/bin/env python3
"""
inject.py — fill __PLACEHOLDER__ tokens from environment variables.

Used by deploy scripts: source .env, pipe a source file through this,
push the result straight to the target (or build from it inside /dev/shm).
The cred-embedded output is NEVER written to persistent disk here.

Usage:
  source .env && python3 scripts/inject.py pi/ups-monitor.py > /dev/shm/out.py
  python3 scripts/inject.py /dev/shm/fw/src/main.cpp --inplace

Exits non-zero if any placeholder has no matching env var.
"""
import os
import re
import sys

TOKEN_RX = re.compile(r"__([A-Z0-9_]+)__(?!_)")  # won't match ___X___ oddities


def fill(text):
    def repl(m):
        return os.environ.get(m.group(1), m.group(0))
    return TOKEN_RX.sub(repl, text)


def main():
    args = [a for a in sys.argv[1:] if a != "--inplace"]
    inplace = "--inplace" in sys.argv
    if not args:
        print("usage: inject.py <file> [--inplace]", file=sys.stderr)
        return 2

    path = args[0]
    text = open(path).read()
    out = fill(text)

    missing = sorted(set(TOKEN_RX.findall(out)))
    if missing:
        print("UNFILLED PLACEHOLDERS: " + ", ".join(missing), file=sys.stderr)
        return 1

    if inplace:
        open(path, "w").write(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
