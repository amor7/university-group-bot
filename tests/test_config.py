import pytest

from bot.config import PROJECT_ROOT, ConfigError, load_config, parse_admin_ids


def test_parse_admin_ids():
    assert parse_admin_ids(" 1, 2 ,,3") == {1, 2, 3}
    assert parse_admin_ids(None) == frozenset()
    with pytest.raises(ConfigError):
        parse_admin_ids("1,abc")


def test_load_config(tmp_path, monkeypatch):
    for k in ("BOT_TOKEN", "ADMIN_IDS", "DB_PATH", "GROUP_ID"):
        monkeypatch.delenv(k, raising=False)
    empty = tmp_path / "empty.env"
    empty.write_text("")
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_config(str(empty))
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=x:y\nADMIN_IDS=10,20\nGROUP_ID=-100\n")
    cfg = load_config(str(env))
    assert cfg.admin_ids == {10, 20} and cfg.group_id == -100
    assert cfg.db_path == str(PROJECT_ROOT / "bot.db")  # relative to the project, not cwd


def test_default_contact(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_CONTACT", raising=False)
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=x:y\n", encoding="utf-8")
    assert load_config(str(env)).default_contact is None
    monkeypatch.setenv("DEFAULT_CONTACT", " @support ")
    assert load_config(str(env)).default_contact == "@support"


def test_resolve_db_path(tmp_path):
    from bot.config import resolve_db_path
    assert resolve_db_path("bot.db", tmp_path) == str(tmp_path / "bot.db")
    assert resolve_db_path("data/x.db", tmp_path) == str(tmp_path / "data" / "x.db")
    absolute = str(tmp_path / "abs.db")
    assert resolve_db_path(absolute, tmp_path / "elsewhere") == absolute
    assert resolve_db_path(":memory:") == ":memory:"


def test_env_read_from_project_root_not_cwd(tmp_path, monkeypatch):
    import bot.config as config_mod
    for k in ("BOT_TOKEN", "DB_PATH", "GROUP_ID", "ADMIN_IDS"):
        monkeypatch.delenv(k, raising=False)
    root, cwd = tmp_path / "project", tmp_path / "somewhere"
    root.mkdir()
    cwd.mkdir()
    (root / ".env").write_text("BOT_TOKEN=a:b\nDB_PATH=data.db\n", encoding="utf-8")
    (cwd / ".env").write_text("BOT_TOKEN=wrong\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", root)
    monkeypatch.chdir(cwd)
    cfg = load_config()
    assert cfg.bot_token == "a:b" and cfg.db_path == str(root / "data.db")
