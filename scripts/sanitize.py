#!/usr/bin/env python3
"""
sanitize.py — detect & scrub embedded secrets from repo text files.

Replaces secrets with __PLACEHOLDER__ tokens; deploy scripts fill them
from .env at build/push time (see scripts/inject.py). Idempotent —
placeholders are never re-matched.

Usage:
  python3 scripts/sanitize.py             # report only
  python3 scripts/sanitize.py --clean     # rewrite files in place
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".pio", "__pycache__", "artifacts", "node_modules", ".venv"}
SCAN_EXT = {".py", ".cpp", ".h", ".hpp", ".md", ".sh", ".ini", ".txt",
            ".service", ".env"}
SCAN_FILES = {".gitignore"}
# .env.example is a format template by policy — never holds real secrets.
SKIP_FILES = {".env.example"}

# (label, compiled regex, replacement) — specific rules BEFORE generic ones.
RULES = [
    ("TG_BOT_TOKEN",   re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "__TG_BOT_TOKEN__"),
    # NB: pattern split across a concatenation so this file never matches itself.
    # [^\s"]+ (not \S+) so a trailing quote is never swallowed.
    ("PVE_TOKEN",      re.compile("PVEAPI" + r"Token=[^\s\"]+"), "__PVE_TOKEN__"),
    ("UUID_SECRET",    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "__PVE_TOKEN_SECRET__"),
    ("TG_CHAT_ID",     re.compile(
        r'((?:TG_CHAT_ID|CHAT_ID)\s*=\s*["\']?)(\d{7,12})'),
        r"\g<1>__TG_CHAT_ID__"),
    ("TG_PROXY",       re.compile(r"socks5h?://[^\s\"']+"), "__TG_PROXY__"),
    # (?!__) keeps these rules idempotent: a __PLACEHOLDER__ value never re-matches.
    ("WIFI_SSID",      re.compile(r'(WIFI_SSID\s*=\s*)"(?!__)[^"]*"'), r'\1"__WIFI_SSID__"'),
    ("WIFI_PASS",      re.compile(r'(WIFI_PASS\s*=\s*)"(?!__)[^"]*"'), r'\1"__WIFI_PASS__"'),
    ("OTA_PASSWORD",   re.compile(r'(OTA_PASSWORD\s*=\s*)"(?!__)[^"]*"'), r'\1"__OTA_PASSWORD__"'),
    ("WIFI_BSSID_HEX", re.compile(
        r"\{\s*0x[0-9A-Fa-f]{2}\s*(?:,\s*0x[0-9A-Fa-f]{2}\s*){5}\}"),
        "__WIFI_BSSID_BYTES__"),
    ("MAC",            re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), "__MAC__"),
    ("NTFY_DDNS",      re.compile(r"\b[\w-]+\.duckdns\.org\b"), "__NTFY_DDNS_HOST__"),
]


def iter_files():
    for p in sorted(ROOT.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file() and p.name not in SKIP_FILES \
                and (p.suffix in SCAN_EXT or p.name in SCAN_FILES):
            yield p


def scan(text):
    """Return {label: [line_numbers]} without printing any secret content."""
    hits = {}
    for label, rx, _repl in RULES:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.setdefault(label, []).append(line)
    return hits


def clean(text):
    for _label, rx, repl in RULES:
        text = rx.sub(repl, text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true",
                    help="rewrite files in place (default: report only)")
    args = ap.parse_args()

    total = 0
    for path in iter_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue

        hits = scan(text)
        if not hits:
            continue

        n = sum(len(v) for v in hits.values())
        total += n
        detail = ", ".join(f"{k}:{len(v)}" for k, v in hits.items())
        print(f"{path.relative_to(ROOT)}  [{detail}]")
        for label, lines in hits.items():
            print(f"    {label:15s} line(s) {','.join(map(str, lines[:8]))}"
                  + (" …" if len(lines) > 8 else ""))

        if args.clean:
            path.write_text(clean(text))

    mode = "CLEANED" if args.clean else "FOUND"
    print(f"\n{mode}: {total} occurrence(s) across the repo."
          if total else "Repo is clean — no secrets detected.")
    if total and not args.clean:
        print("Run with --clean to scrub.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
