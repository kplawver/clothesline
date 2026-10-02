"""Configuration precedence and the loopback-only guarantee."""

import pytest

from clothesline.config import Config, load


def test_defaults_are_loopback_19004():
    config = Config()
    assert (config.host, config.port) == ("127.0.0.1", 19004)
    assert config.database.name == "clothesline.sqlite3"
    assert config.model_dir.name == "models"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert Config(host=host).host == host


def test_non_loopback_and_bad_port_are_refused():
    with pytest.raises(ValueError, match="loopback"):
        Config(host="0.0.0.0")
    with pytest.raises(ValueError, match="Port"):
        Config(port=80)
    with pytest.raises(ValueError, match="Port"):
        Config(port=70000)


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('host = "::1"\nport = 19500\ndata_dir = "/tmp/from-file"\n')
    monkeypatch.setenv("CLOTHESLINE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLOTHESLINE_PORT", "19501")
    monkeypatch.setenv("CLOTHESLINE_DATA_DIR", str(tmp_path / "from-env"))
    config = load(path)
    assert (config.host, config.port) == ("127.0.0.1", 19501)
    assert config.data_dir == tmp_path / "from-env"


def test_file_alone_is_used_when_environment_is_clear(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('port = 19502\n')
    for name in ("CLOTHESLINE_HOST", "CLOTHESLINE_PORT", "CLOTHESLINE_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert load(path).port == 19502


def test_unknown_keys_are_refused(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('prot = 1234\n')
    with pytest.raises(ValueError, match="Unknown configuration keys: prot"):
        load(path)


def test_missing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOTHESLINE_CONFIG", raising=False)
    assert load(tmp_path / "absent.toml").port == 19004