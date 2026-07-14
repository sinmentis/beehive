from unittest.mock import patch

import pytest

from beehive.auth.passwords import verify_password
from beehive.db import app_state
from beehive.db.connection import connect


def test_set_admin_password_stores_verifiable_hash(tmp_path):
    from scripts.set_admin_password import set_admin_password
    db_path = str(tmp_path / "t.db")
    set_admin_password(db_path, "my-new-password")

    conn = connect(db_path)
    hashed = app_state.get(conn, "admin_password_hash")
    assert hashed is not None
    assert verify_password(hashed, "my-new-password") is True
    assert verify_password(hashed, "wrong-password") is False


def test_set_admin_password_bootstraps_schema_on_fresh_db(tmp_path):
    """Regression guard matching the Slice 1 final-review fix: this is a standalone CLI
    entrypoint too, so it must not assume some other process already created the schema."""
    from scripts.set_admin_password import set_admin_password
    db_path = str(tmp_path / "brand_new.db")
    set_admin_password(db_path, "pw")  # must not raise "no such table"


def test_main_rejects_mismatched_passwords(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--db-path", str(tmp_path / "t.db")])
    with patch("scripts.set_admin_password.getpass.getpass", side_effect=["pw1", "pw2"]):
        from scripts.set_admin_password import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_main_rejects_empty_password(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--db-path", str(tmp_path / "t.db")])
    with patch("scripts.set_admin_password.getpass.getpass", side_effect=["", ""]):
        from scripts.set_admin_password import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_main_sets_password_on_matching_input(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr("sys.argv", ["prog", "--db-path", db_path])
    with patch("scripts.set_admin_password.getpass.getpass",
               side_effect=["good-pw", "good-pw"]):
        from scripts.set_admin_password import main
        main()

    conn = connect(db_path)
    hashed = app_state.get(conn, "admin_password_hash")
    assert verify_password(hashed, "good-pw") is True
