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


def test_group_id_tolerates_copied_label():
    from bot.config import parse_group_id
    assert parse_group_id("ID: -1004321133862") == -1004321133862
    assert parse_group_id("id:-100123") == -100123
    assert parse_group_id(" -100123 ") == -100123
    assert parse_group_id("") is None


def test_group_id_rejects_garbage():
    import pytest
    from bot.config import ConfigError, parse_group_id
    with pytest.raises(ConfigError):
        parse_group_id("my group")


def test_proxy_url_validation():
    import pytest
    from bot.config import ConfigError, parse_proxy_url
    assert parse_proxy_url("") is None
    assert parse_proxy_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert parse_proxy_url("http://u:p@proxy:8080") == "http://u:p@proxy:8080"
    with pytest.raises(ConfigError):
        parse_proxy_url("127.0.0.1:1080")


def test_build_application_with_proxy():
    from bot.config import Config
    from bot.main import build_application
    cfg = Config(bot_token="123:abc", admin_ids=frozenset(), db_path=":memory:",
                 group_id=None, proxy_url="socks5://127.0.0.1:1080")
    app = build_application(cfg)
    assert app is not None
