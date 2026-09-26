"""Add or update a personal account in the ``USERS_FILE`` user list.

    python scripts/create_user.py users.json ayse.k kidemli_analist "Ayşe K."
    printf '%s' "$PW" | python scripts/create_user.py users.json ayse.k analist --stdin

The password is read interactively (or from stdin with ``--stdin``), never
from the command line, and only its PBKDF2 hash is written. Point the
application at the file with ``USERS_FILE=/path/users.json``; this is how a
prod deployment (where demo users are refused) gets the distinct senior
analysts and admins that maker-checker needs.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security.auth import ROLE_RANK, hash_password

MIN_PASSWORD_LENGTH = 12
ROLES = sorted(r for r in ROLE_RANK if r != "service")


def upsert(path: Path, username: str, role: str, display_name: str, password: str) -> None:
    if role not in ROLES:
        raise ValueError(f"rol şunlardan biri olmalı: {', '.join(ROLES)}")
    if not username or ":" in username:
        raise ValueError("Kullanıcı adı boş olamaz ve ':' içeremez")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"parola en az {MIN_PASSWORD_LENGTH} karakter olmalı")
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries = [e for e in entries if e.get("username") != username]
    entries.append(
        {
            "username": username,
            "role": role,
            "display_name": display_name or username,
            "password_hash": hash_password(password),
        }
    )
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("users_file", type=Path)
    parser.add_argument("username")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("display_name", nargs="?", default="")
    parser.add_argument("--stdin", action="store_true", help="parolayı stdin'den oku")
    args = parser.parse_args(argv)
    if args.stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        password = getpass.getpass("Parola: ")
        if password != getpass.getpass("Parola (tekrar): "):
            print("Parolalar eşleşmiyor", file=sys.stderr)
            return 1
    try:
        upsert(args.users_file, args.username, args.role, args.display_name, password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.username} ({args.role}) → {args.users_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
