from unittest.mock import MagicMock, patch


def test_main_creates_app_with_db_path_and_runs_uvicorn(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/custom.db")
    fake_app = MagicMock()
    with patch("scripts.run_web.create_app", return_value=fake_app) as mock_create, \
         patch("scripts.run_web.uvicorn.run") as mock_run:
        from scripts.run_web import main
        main()
    mock_create.assert_called_once_with("/tmp/custom.db")
    mock_run.assert_called_once_with(fake_app, host="0.0.0.0", port=8000)


def test_main_defaults_db_path(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    with patch("scripts.run_web.create_app") as mock_create, \
         patch("scripts.run_web.uvicorn.run"):
        from scripts.run_web import main
        main()
    mock_create.assert_called_once_with("/data/beehive.db")
