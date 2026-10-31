"""插件与自建服务之间的布尔设置同步。"""

import importlib
import json

import yaml
from stock_robot_plugin_test.disk_support import FileLock


def sync_module():
    return importlib.import_module("stock_robot_plugin_test.service_settings")


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
    return sync_module().sync_service_settings(
        config, data_dir, tmp_path / "instance"
    )


def outcomes(result):
    return {item.setting: item for item in result}


def baseline(tmp_path):
    payload = json.loads(
        (tmp_path / "data" / "service-settings-sync.json").read_text("utf-8")
    )
    return payload["settings"]


def service_value(path, section, key):
    document = yaml.safe_load(path.read_text("utf-8"))
    return document[section][key]


def test_defaults_do_not_write_service_config(tmp_path):
    path = make_instance(tmp_path)
    before = path.read_text("utf-8")
    config = Config()

    result = sync(tmp_path, config)

    assert {item.action for item in result} == {"in_sync"}
    assert path.read_text("utf-8") == before
    assert baseline(tmp_path) == {"extra_analysis": False, "report_cleanup": True}
    assert config.saved == 0


def test_first_sync_writes_explicit_plugin_values(tmp_path):
    path = make_instance(tmp_path)
    config = Config(extra_analysis_enabled=True, cleanup_previous_reports=False)

    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "pushed"
    assert result["report_cleanup"].action == "pushed"
    assert service_value(path, "analysis", "extra_enabled") is True
    assert service_value(path, "maintenance", "report_cleanup_enabled") is False
    assert baseline(tmp_path) == {"extra_analysis": True, "report_cleanup": False}


def test_explicit_service_values_win_on_first_upgrade(tmp_path):
    make_instance(tmp_path, {
        "llm": {"enabled": False},
        "analysis": {"extra_enabled": True},
        "maintenance": {"report_cleanup_enabled": False},
    })
    config = Config(extra_analysis_enabled=False, cleanup_previous_reports=True)

    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "adopted"
    assert result["report_cleanup"].action == "adopted"
    assert config["extra_analysis_enabled"] is True
    assert config["cleanup_previous_reports"] is False
    assert config.saved == 1


def test_plugin_change_is_pushed_after_baseline(tmp_path):
    path = make_instance(tmp_path)
    config = Config()
    assert {item.action for item in sync(tmp_path, config)} == {"in_sync"}

    config["extra_analysis_enabled"] = True
    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "pushed"
    assert result["report_cleanup"].action == "in_sync"
    assert service_value(path, "analysis", "extra_enabled") is True
    assert result["report_cleanup"].service_value is True
    assert config.saved == 0


def test_service_change_is_adopted_and_mirrored(tmp_path):
    path = make_instance(tmp_path)
    config = Config()
    assert {item.action for item in sync(tmp_path, config)} == {"in_sync"}

    path.write_text(
        yaml.safe_dump(
            {
                "llm": {"enabled": False},
                "analysis": {"extra_enabled": True},
                "maintenance": {"report_cleanup_enabled": False},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "adopted"
    assert result["report_cleanup"].action == "adopted"
    assert config["extra_analysis_enabled"] is True
    assert config["cleanup_previous_reports"] is False
    assert config.saved == 1


def test_cross_key_changes_are_combined_in_one_pass(tmp_path):
    """一个键由插件改、另一个由服务端改时不能互相覆盖。"""
    path = make_instance(tmp_path)
    config = Config()
    sync(tmp_path, config)

    document = yaml.safe_load(path.read_text("utf-8"))
    document["maintenance"] = {"report_cleanup_enabled": False}
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    config["extra_analysis_enabled"] = True

    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "pushed"
    assert result["report_cleanup"].action == "adopted"
    assert service_value(path, "analysis", "extra_enabled") is True
    assert config["cleanup_previous_reports"] is False


def test_both_side_change_to_same_value_keeps_service_and_warns(tmp_path):
    path = make_instance(tmp_path)
    config = Config()
    sync(tmp_path, config)

    document = yaml.safe_load(path.read_text("utf-8"))
    document["analysis"] = {"extra_enabled": True}
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    config["extra_analysis_enabled"] = True

    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "in_sync"
    assert "同时修改" in result["extra_analysis"].warning
    assert baseline(tmp_path)["extra_analysis"] is True


def test_deleted_service_key_restores_default(tmp_path):
    path = make_instance(tmp_path)
    config = Config(extra_analysis_enabled=True)
    assert outcomes(sync(tmp_path, config))["extra_analysis"].action == "pushed"

    document = yaml.safe_load(path.read_text("utf-8"))
    document.pop("analysis", None)
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "adopted"
    assert config["extra_analysis_enabled"] is False


def test_plugin_save_failure_keeps_baseline_and_reports_failure(tmp_path):
    make_instance(tmp_path, {"analysis": {"extra_enabled": True}})

    class Broken(Config):
        def save_config(self):
            raise OSError("配置只读")

    config = Broken()
    result = outcomes(sync(tmp_path, config))

    assert result["extra_analysis"].action == "adopt_failed"
    assert result["extra_analysis"].warning == "插件配置保存失败"
    saved = json.loads(
        (tmp_path / "data" / "service-settings-sync.json").read_text("utf-8")
    )["settings"]
    assert "extra_analysis" not in saved


def test_missing_service_config_returns_missing(tmp_path):
    config = Config()
    result = sync_module().sync_service_settings(
        config, tmp_path / "data", tmp_path / "missing"
    )
    assert {item.action for item in result} == {"missing"}
    assert config.saved == 0


def test_invalid_service_config_is_not_modified(tmp_path):
    path = tmp_path / "instance" / ".stock_robot" / "config.yaml"
    path.parent.mkdir(parents=True)
    original = "analysis: [\n"
    path.write_text(original, encoding="utf-8")

    result = sync(tmp_path, Config(extra_analysis_enabled=True))

    assert {item.action for item in result} == {"invalid"}
    assert path.read_text("utf-8") == original


def test_locked_service_config_is_skipped(tmp_path):
    path = make_instance(tmp_path)
    lock = FileLock(path.with_name("config.yaml.lock"))
    assert lock.acquire()
    try:
        result = sync(tmp_path, Config(extra_analysis_enabled=True))
    finally:
        lock.close()

    assert {item.action for item in result} == {"locked"}
    document = yaml.safe_load(path.read_text("utf-8"))
    assert "analysis" not in document


def test_legacy_retention_baseline_is_migrated(tmp_path):
    path = make_instance(tmp_path, {"maintenance": {"report_cleanup_enabled": False}})
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "retention-sync.json").write_text(
        json.dumps({"value": False, "mtime_ns": 1}), encoding="utf-8"
    )
    config = Config(cleanup_previous_reports=False)

    result = outcomes(
        sync_module().sync_service_settings(config, data_dir, tmp_path / "instance")
    )

    assert result["report_cleanup"].action == "in_sync"
    assert baseline(tmp_path)["report_cleanup"] is False
    assert path.exists()
