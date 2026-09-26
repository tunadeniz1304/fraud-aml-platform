"""N3: personal accounts for prod come from USERS_FILE (written by scripts/create_user.py)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.create_user as cu
from app.security.auth import AuthError, UserDirectory

PW = "correct-horse-battery"


def _prod(users_file: Path) -> SimpleNamespace:
    return SimpleNamespace(seed_demo_users=False, environment="prod", users_file=str(users_file))


def test_prod_directory_serves_the_provisioned_accounts(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    cu.upsert(path, "ayse.k", "kidemli_analist", "Ayşe K.", PW)
    cu.upsert(path, "mehmet.t", "admin", "", PW)
    cu.upsert(path, "ayse.k", "kidemli_analist", "Ayşe K.", PW + "!")  # rotate, no duplicate
    assert PW not in path.read_text(encoding="utf-8")
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2

    users = UserDirectory.from_settings(_prod(path))
    senior = users.authenticate("ayse.k", PW + "!")
    assert senior.role == "kidemli_analist" and senior.display_name == "Ayşe K."
    assert users.authenticate("mehmet.t", PW).display_name == "mehmet.t"
    with pytest.raises(AuthError):
        users.authenticate("ayse.k", PW)
    assert "analist" not in users.users  # no demo accounts in prod


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"username": "svc", "role": "service", "password_hash": "pbkdf2$x"}, "rol"),
        ({"username": "x", "role": "admin", "password_hash": "plaintext"}, "özeti"),
        ({"username": "a:b", "role": "admin", "password_hash": "pbkdf2$x"}, "':'"),
    ],
)
def test_a_bad_users_file_stops_startup(tmp_path: Path, entry: dict, message: str) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        UserDirectory.from_settings(_prod(path))


def test_cli_rejects_short_passwords_and_reads_stdin(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "users.json"
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cu.main([str(path), "ali", "analist", "--stdin"]) == 1
    assert not path.exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert cu.main([str(path), "ali", "analist", "Ali", "--stdin"]) == 0
    users = UserDirectory.from_settings(_prod(path))
    assert users.authenticate("ali", PW).role == "analist"
