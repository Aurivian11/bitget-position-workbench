#!/usr/bin/env python3
"""Build the dashboard from local HTML fragments; no network or extra dependency."""
from pathlib import Path
import hashlib
import re
ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app/web"
FRAGMENTS = WEB / "fragments"
def build():
    sections = []
    for name in ["document-head", "sidebar", "dashboard", "notifications", "settings", "guide", "document-footer"]:
        text = (FRAGMENTS / (name + ".html")).read_text(encoding="utf-8")
        if name == "guide":
            text = text.replace('<div id="guide-content" class="guide-grid"></div>',
                '<div id="guide-content" class="guide-grid">' +
                (FRAGMENTS / "guide-content.html").read_text(encoding="utf-8") + '</div>')
        sections.append(text)
    digest = hashlib.sha256()
    for file in sorted((WEB / "assets").rglob("*")):
        if file.is_file() and file.suffix in {".js", ".css"}:
            digest.update(file.read_bytes())
    text = re.sub(r'\?v=20261102', '?v=' + digest.hexdigest()[:12], "".join(sections))
    (WEB / "index.html").write_text(text, encoding="utf-8")
    login = (FRAGMENTS / "login.html").read_text(encoding="utf-8")
    login = re.sub(r"\?v=20261102", "?v=" + digest.hexdigest()[:12], login)
    (WEB / "login.html").write_text(login, encoding="utf-8")
    print("Built app/web/index.html and app/web/login.html")
if __name__ == "__main__":
    build()
