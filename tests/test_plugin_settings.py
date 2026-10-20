"""六项配置及旧版迁移的确定性回归。"""

import importlib
import json
from pathlib import Path

import pytest


def settings():
    return importlib.import_module("stock_robot_plugin_test.plugin_settings")


def test_schema_visible_fields_and_plain_labels():
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text("utf-8"))
    visible = {key: value for key, value in schema.items() if not value.get("invisible")}
    assert list(visible) == ["cleanup_previous_reports", "service_port", "timeout_seconds",
                             "startup_timeout_seconds", "install_timeout_seconds", "package_index_url"]
    assert visible["cleanup_previous_reports"]["description"] == "清理历史原始报告"
    assert visible["cleanup_previous_reports"]["default"] is True
    assert visible["install_timeout_seconds"]["default"] == 1800
    assert visible["package_index_url"]["default"] == ""
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in visible["package_index_url"]["hint"]


@pytest.mark.parametrize("value", [0, -1, 65536, True, "abc", "12.5", 12.5])
def test_invalid_port_rejected(value):
    with pytest.raises((ValueError, TypeError), match="端口"):
        settings().resolve_port({"service_port": value})


def test_legacy_local_port_migration_and_new_port_precedence():
    assert settings().resolve_port({}) == 25618
    assert settings().resolve_port({"base_url": "http://localhost:18080", "service_port": 25618}) == 18080
    assert settings().resolve_port({"base_url": "http://127.0.0.1:18080/", "service_port": 18081}) == 18081


@pytest.mark.parametrize("url", ["http://remote.example:80", "https://localhost:1234",
                               "http://localhost:1234/prefix", "http://user:secret@localhost:1234",
                               "http://localhost:1234?token=secret"])
def test_unsupported_legacy_url_never_silently_switches(url):
    with pytest.raises(ValueError, match="旧服务地址") as error:
        settings().resolve_port({"base_url": url, "service_port": 25618})
    assert "secret" not in str(error.value)


def test_custom_archive_is_not_silently_replaced():
    with pytest.raises(ValueError, match="自定义源码"):
        settings().resolve_legacy_extras({"source_archive_url": "https://example.org/private.zip"}, Path("missing"))


def test_port_migration_is_saved_once_and_keeps_backup(tmp_path):
    class Config(dict):
        saved = 0

        def save_config(self):
            self.saved += 1
            Path(self.config_path).write_text(json.dumps(self), encoding="utf-8")

    config = Config(base_url="http://localhost:18080", service_port=25618,
                    cleanup_previous_reports=False, install_timeout_seconds=2400)
    config.config_path = str(tmp_path / "config.json")
    Path(config.config_path).write_text(json.dumps(config), encoding="utf-8")
    original = Path(config.config_path).read_bytes()
    data = tmp_path / "data"
    assert settings().migrate_port(config, data) == 18080
    assert config.saved == 1 and config["base_url"] == ""
    assert (data / "config-before-port-migration.json").read_bytes() == original
    config["service_port"] = 18081
    assert settings().migrate_port(config, data) == 18081
    assert config.saved == 1
    assert config["cleanup_previous_reports"] is False
    assert config["install_timeout_seconds"] == 2400


def test_existing_ready_rag_install_is_preserved(tmp_path):
    bootstrap = importlib.import_module("stock_robot_plugin_test.bootstrap")
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").touch()
    info = {"archive_url": "https://example.org/tag.zip", "archive_sha256": "a" * 64,
            "release_tag": "v0.2.1"}
    (source / ".bootstrap-source.json").write_text(json.dumps(info))
    for path in (bootstrap.venv_python(tmp_path), bootstrap.venv_launcher(tmp_path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "install-state.json").write_text(json.dumps({
        "version": 1, "extras": "rag", "source": info, "dependency_strategy": "locked-requirements-v1",
    }))
    assert settings().resolve_legacy_extras({}, tmp_path) == "rag"


def test_new_instance_uses_core_only(tmp_path):
    assert settings().resolve_legacy_extras({}, tmp_path) == ""
