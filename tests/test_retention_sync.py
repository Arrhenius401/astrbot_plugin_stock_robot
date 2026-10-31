"""报告清理开关在插件与自建分析服务配置之间的双向同步。"""

import importlib
import json

import yaml
from stock_robot_plugin_test.disk_support import FileLock


def sync_module():
    return importlib.import_module("stock_robot_plugin_test.retention_sync")


class Config(dict):
    """最小 AstrBot 配置替身，记录 save_config 调用。"""

    def __init__(self, **values):
        super().__init__(**values)
        self.saved = 0

    def save_config(self):
        self.saved += 1


def make_instance(tmp_path, document=None):
    path = tmp_path / "instance" / ".stock_robot" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(document or {"llm": {"enabled": False}}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def sync(tmp_path, config):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return sync_module().sync_cleanup_setting(
        config, data_dir, tmp_path / "instance"
    )


def service_value(path):
    document = yaml.safe_load(path.read_text("utf-8"))
    return document["maintenance"]["report_cleanup_enabled"]


def test_first_sync_seeds_plugin_value(tmp_path):
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports=False)

    outcome = sync(tmp_path, config)

    assert outcome.action == "pushed"
    assert service_value(path) is False
    document = yaml.safe_load(path.read_text("utf-8"))
    assert document["llm"] == {"enabled": False}
    baseline = json.loads((tmp_path / "data" / "retention-sync.json").read_text("utf-8"))
    assert baseline["value"] is False


def test_explicit_service_value_wins_on_first_upgrade(tmp_path):
    path = make_instance(
        tmp_path, {"maintenance": {"report_cleanup_enabled": True}}
    )
    config = Config(cleanup_previous_reports=False)

    outcome = sync(tmp_path, config)

    assert outcome.action == "adopted"
    assert config["cleanup_previous_reports"] is True
    assert config.saved == 1
    assert service_value(path) is True


def test_plugin_change_is_pushed_after_baseline(tmp_path):
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports=True)
    assert sync(tmp_path, config).action == "pushed"

    config["cleanup_previous_reports"] = False
    outcome = sync(tmp_path, config)

    assert outcome.action == "pushed"
    assert service_value(path) is False
    assert config.saved == 0


def test_service_change_is_adopted_after_baseline(tmp_path):
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports=True)
    assert sync(tmp_path, config).action == "pushed"

    path.write_text(
        yaml.safe_dump(
            {
                "llm": {"enabled": False},
                "maintenance": {"report_cleanup_enabled": False},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    outcome = sync(tmp_path, config)

    assert outcome.action == "adopted"
    assert config["cleanup_previous_reports"] is False
    assert config.saved == 1


def test_unrelated_touch_makes_service_value_authoritative(tmp_path):
    """服务脚本或配置页改动过配置文件时，以服务端现有值为准。"""
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports=True)
    assert sync(tmp_path, config).action == "pushed"

    document = yaml.safe_load(path.read_text("utf-8"))
    document["llm"] = {"enabled": True}
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    config["cleanup_previous_reports"] = False

    outcome = sync(tmp_path, config)

    assert outcome.action == "adopted"
    assert config["cleanup_previous_reports"] is True
    assert service_value(path) is True


def test_deleted_service_key_restores_default_and_mirrors(tmp_path):
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports=False)
    assert sync(tmp_path, config).action == "pushed"

    path.write_text(
        yaml.safe_dump({"llm": {"enabled": False}}, allow_unicode=True),
        encoding="utf-8",
    )
    outcome = sync(tmp_path, config)

    assert outcome.action == "adopted"
    assert config["cleanup_previous_reports"] is True


def test_missing_service_config_is_ignored(tmp_path):
    config = Config(cleanup_previous_reports=False)

    outcome = sync(tmp_path, config)

    assert outcome.action == "missing"
    assert config.saved == 0


def test_invalid_service_config_is_not_modified(tmp_path):
    path = tmp_path / "instance" / ".stock_robot" / "config.yaml"
    path.parent.mkdir(parents=True)
    original = "maintenance: [\n"
    path.write_text(original, encoding="utf-8")

    outcome = sync(tmp_path, Config(cleanup_previous_reports=False))

    assert outcome.action == "invalid"
    assert path.read_text("utf-8") == original


def test_locked_service_config_is_skipped(tmp_path):
    path = make_instance(tmp_path)
    lock = FileLock(path.with_name("config.yaml.lock"))
    assert lock.acquire()
    try:
        outcome = sync(tmp_path, Config(cleanup_previous_reports=False))
    finally:
        lock.close()

    assert outcome.action == "locked"
    document = yaml.safe_load(path.read_text("utf-8"))
    assert "maintenance" not in document


def test_non_boolean_plugin_value_defaults_to_enabled(tmp_path):
    path = make_instance(tmp_path)
    config = Config(cleanup_previous_reports="关闭")

    outcome = sync(tmp_path, config)

    assert outcome.action == "pushed"
    assert service_value(path) is True


def test_corrupt_baseline_is_treated_as_first_sync(tmp_path):
    path = make_instance(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "retention-sync.json").write_text("不是 JSON", encoding="utf-8")

    outcome = sync(tmp_path, Config(cleanup_previous_reports=False))

    assert outcome.action == "pushed"
    assert service_value(path) is False
