#!/usr/bin/env python3
"""Stamp cache-busting versions into app/web/index.html and login.html (no network, no dependencies)."""
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app/web"


def build():
    digest = hashlib.sha256()
    for file in sorted((WEB / "assets").rglob("*")):
        if file.is_file():
            digest.update(file.read_bytes())
    version = digest.hexdigest()[:12]
    for name in ("index.html", "login.html"):
        page = WEB / name
        text = re.sub(r"(assets/[\w.-]+\?v=)[\w-]+", r"\g<1>" + version, page.read_text(encoding="utf-8"))
        page.write_text(text, encoding="utf-8")
    print("Stamped app/web pages with version", version)


if __name__ == "__main__":
    build()
