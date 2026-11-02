#!/usr/bin/env python3
"""Create initial web credentials without printing or passing passwords in argv."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true",
                        help="save a random password in config/web-admin-access.txt (0600)")
    args = parser.parse_args()
    os.umask(0o077)
    import notification_settings as settings
    import web_admin

    if web_admin.AUTH_PATH.exists():
        parser.error("Credentials already exist; initialization will not overwrite them.")
    access_path = settings.PROJECT / "config/web-admin-access.txt"
    if args.generate and access_path.exists():
        parser.error("The generated-password file already exists; refusing to overwrite it.")
    if args.generate:
        password = secrets.token_urlsafe(32)
    else:
        password = getpass.getpass("管理密码（16–256 位）: ")
        repeated = getpass.getpass("再次输入管理密码: ")
        if password != repeated:
            parser.error("两次密码不一致。")
        if not 16 <= len(password) <= 256:
            parser.error("密码长度须为 16–256 位。")
    (settings.PROJECT / "data").mkdir(mode=0o750, parents=True, exist_ok=True)
    web_admin.initialize_auth(password)
    if args.generate:
        settings._atomic(access_path, {"url": web_admin.ORIGIN + "/bitget/", "password": password})
        print("Random password saved to:", access_path)
        print("The file is private (0600). Read it only through your server's secure session.")
    else:
        print("Password hash created. The plaintext password was not saved.")
    print("Open:", web_admin.ORIGIN + "/bitget/")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print("Initialization failed:", str(error), file=sys.stderr)
        raise SystemExit(1)
