"""真实文件的留存边界、恢复、归属保护和容量淘汰。"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from stock_robot_plugin_test import report_storage
from stock_robot_plugin_test.disk_support import FileLock, RotatingLog, safe_path
from stock_robot_plugin_test.report_storage import ReportStorage


@pytest.fixture
def store(tmp_path):
    now = [1_800_000_000.0]
    storage = ReportStorage(tmp_path / "中文目录", clock=lambda: now[0])
    assert storage._lock.acquire()
    storage.enabled = True
    storage.now = now
    yield storage
    storage._lock.close()


def completed(store, state="success", markdown="完整中文\n报告\n", image=True):
    task = store.begin(markdown, "000300", "index")
    store.finish(
        task,
        SimpleNamespace(png=b"png", width=100, height=100) if image else None,
        state,
        "complete",
        "" if state == "success" else "TimeoutError",
    )
    return store.root / task


def test_queued_delivery_removes_report_and_diagnostic_after_72_hours(store):
    task = store.begin("等待报告", "00700.HK", "stock")
    store.finish(task, None, "failed", "queued_delivery", "TimeoutError")
    directory = store.root / task
    diagnostic = store.diagnostics / f"{task}.json"
    store.now[0] += 72 * 3600 - 1
    store.clean()
    assert directory.exists() and diagnostic.exists()
    store.now[0] += 1
    store.clean()
    assert not directory.exists() and not diagnostic.exists()


@pytest.mark.parametrize(
    "state,hours", [("success", 24), ("failed", 72), ("cancelled", 72)]
)
def test_time_boundaries_and_diagnostic_survives(store, state, hours):
    directory = completed(store, state)
    assert artifact(directory).read_bytes() == "完整中文\n报告\n".encode()
    store.now[0] += hours * 3600 - 1
    store.clean()
    assert directory.exists()
    store.now[0] += 1
    store.clean()
    assert not directory.exists()
    diagnostic = store.diagnostics / f"{directory.name}.json"
    assert json.loads(diagnostic.read_text("utf-8"))["cleanup_reason"] == "expired"
    assert "完整中文" not in diagnostic.read_text("utf-8")
    store.now[0] = 1_800_000_000 + 7 * 86400
    store.clean()
    assert not diagnostic.exists()


def test_expiration_precedes_capacity_eviction(store):
    oldest_failed = completed(store, "failed")
    store.now[0] += 3600
    newer_success = completed(store)
    store.now[0] += 24 * 3600
    store.artifact_budget = sum(p.stat().st_size for p in oldest_failed.iterdir()) + 1
    store.clean()
    assert oldest_failed.exists()
    assert not newer_success.exists()


def test_capacity_evicts_oldest_and_retains_diagnostic(store):
    old = completed(store)
    store.now[0] += 1
    new = completed(store)
    store.artifact_budget = sum(p.stat().st_size for p in new.iterdir())
    store.clean()
    assert not old.exists() and new.exists()
    record = json.loads((store.diagnostics / f"{old.name}.json").read_text("utf-8"))
    assert record["cleanup_reason"] == "capacity"


def test_active_task_survives_zero_budget_and_expiration(store):
    task = store.begin("active", "1", "stock")
    store.artifact_budget = 0
    store.now[0] += 10 * 86400
    store.clean()
    assert artifact(store.root / task).exists()
    assert (
        json.loads((store.root / task / "task.json").read_text())["state"] == "running"
    )
    store.finish(task, None, "failed", "file", "OSError")
    assert not (store.root / task).exists()


def test_crash_recovery_starts_failure_window(store):
    task = store.begin("crashed", "1", "stock")
    store.active.clear()  # 模拟旧进程已退出，独占锁已由新进程取得。
    store.now[0] += 100 * 86400
    store.clean()
    directory = store.root / task
    meta = json.loads((directory / "task.json").read_text())
    assert meta["state"] == "interrupted" and meta["ended"] == store.now[0]
    store.now[0] += 72 * 3600
    store.clean()
    assert not directory.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "no-metadata",
        "invalid-json",
        "wrong-owner",
        "path-escape",
        "nan",
        "unknown-file",
    ],
)
def test_unknown_or_untrusted_content_is_preserved(store, mutation):
    directory = completed(store)
    path = directory / "task.json"
    meta = json.loads(path.read_text())
    if mutation == "no-metadata":
        path.unlink()
    elif mutation == "invalid-json":
        path.write_text("{")
    elif mutation == "wrong-owner":
        meta["owner"] = "another-plugin"
        path.write_text(json.dumps(meta))
    elif mutation == "path-escape":
        meta["files"] = ["../../valuable.txt"]
        path.write_text(json.dumps(meta))
    elif mutation == "nan":
        meta["ended"] = float("nan")
        path.write_text(json.dumps(meta))
    else:
        (directory / "valuable.txt").write_text("user data")
    store.artifact_budget = 0
    store.now[0] += 10 * 86400
    store.clean()
    assert artifact(directory).exists()


def test_diagnostic_capacity_is_independent(store):
    first = completed(store)
    store.now[0] += 1
    second = completed(store)
    second_record = store.diagnostics / f"{second.name}.json"
    store.diagnostic_budget = second_record.stat().st_size
    store.clean()
    assert first.exists() and second.exists()
    assert not (store.diagnostics / f"{first.name}.json").exists()
    assert second_record.exists()


def test_link_or_junction_does_not_escape(store, tmp_path):
    outside = tmp_path / "valuable"
    outside.mkdir()
    sentinel = outside / "report.md"
    sentinel.write_text("keep")
    store.root.mkdir(parents=True)
    link = store.root / ("a" * 32)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        assert not safe_path(link / "report.md")
        store.artifact_budget = 0
        store.clean()
        assert sentinel.read_text() == "keep"
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_write_and_delete_failures_do_not_destroy_other_data(store, monkeypatch):
    def no_space(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_artifact", no_space)
    assert store.begin("text", "1", "stock") is None
    assert not store.active
    monkeypatch.undo()
    directory = completed(store)
    real_unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path.suffix == ".md":
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    store.artifact_budget = 0
    store.clean()
    assert artifact(directory).exists()
    assert (directory / "task.json").exists()


def test_partial_png_write_is_not_retained_as_valid_image(store, monkeypatch):
    task = store.begin("complete report", "000300", "index")
    real_write = Path.write_bytes

    def no_space(path, content):
        if path.name.endswith(".png.tmp"):
            real_write(path, content[:1])
            raise OSError("disk full during PNG")
        return real_write(path, content)

    monkeypatch.setattr(Path, "write_bytes", no_space)
    store.finish(
        task,
        SimpleNamespace(png=b"png", width=100, height=100),
        "failed",
        "image",
        "OSError",
    )
    directory = store.root / task
    assert artifact(directory).exists()
    assert not list(directory.glob("*.png*"))
    store.artifact_budget = 0
    store.clean()
    assert not directory.exists()


@pytest.mark.asyncio
async def test_raw_cleanup_default_busy_and_ownership_gates(store):
    calls = []
    store.clean_raw = lambda: calls.append("raw")
    store.can_clean_raw = lambda: True
    await store.sweep()
    assert not calls
    store.cleanup_raw = True
    store.busy = lambda: True
    await store.sweep()
    assert not calls
    store.busy = lambda: False
    store.can_clean_raw = lambda: False
    await store.sweep()
    assert not calls
    store.can_clean_raw = lambda: True
    await store.sweep()
    assert calls == ["raw"]


def test_formatter_hash_matches_managed_v022_release():
    assert report_storage.FORMATTER_SHA256 == (
        "45b3bcb92f932ccdb11e3e7c6a76ccb628bdaa471206fceff7165d4850c4d7da"
    )


def test_raw_reports_verified_layout_and_unknown_files(store, monkeypatch):
    formatter = store.data_dir / "instance/src/src/report/formatter.py"
    formatter.parent.mkdir(parents=True)
    formatter.write_bytes(b"verified fixture formatter")
    monkeypatch.setattr(
        report_storage,
        "FORMATTER_SHA256",
        hashlib.sha256(formatter.read_bytes()).hexdigest(),
    )
    month = store.data_dir / "instance/reports/index/000300/2026-10"
    month.mkdir(parents=True)
    old = month / "000300_20261001_120000.md"
    recent = month / "000300_20261010_120000.md"
    unknown = month / "user-report.md"
    for path in (old, recent, unknown):
        path.write_text("keep unless expired known report")
        os.utime(path, (store.now[0], store.now[0]))
    os.utime(old, (store.now[0] - 7 * 86400 - 1,) * 2)
    os.utime(unknown, (store.now[0] - 20 * 86400,) * 2)
    formatter.write_bytes(b"unsupported version")
    store.clean_raw()
    assert old.exists()
    formatter.write_bytes(b"verified fixture formatter")
    store.clean_raw()
    assert not old.exists() and recent.exists() and unknown.exists()


def test_raw_removes_only_verified_empty_subdirectories(store, monkeypatch):
    formatter = store.data_dir / "instance/src/src/report/formatter.py"
    formatter.parent.mkdir(parents=True)
    formatter.write_bytes(b"known")
    monkeypatch.setattr(
        report_storage, "FORMATTER_SHA256", hashlib.sha256(b"known").hexdigest()
    )
    month = store.data_dir / "instance/reports/stock/600519/2026-10"
    month.mkdir(parents=True)
    old = month / "600519_20261001_120000.md"
    old.write_text("report")
    os.utime(old, (store.now[0] - 8 * 86400,) * 2)
    other = store.data_dir / "instance/reports/backtests/keep.md"
    other.parent.mkdir()
    other.write_text("keep")
    store.clean_raw()
    assert not month.parent.exists()
    assert other.read_text() == "keep"


@pytest.mark.asyncio
async def test_cross_instance_lock_disables_deletion(store):
    second = ReportStorage(store.data_dir)
    await second.start()
    assert not second.enabled
    assert second.begin("report", "1", "stock") is None
    await second.stop()


@pytest.mark.asyncio
async def test_cancelled_io_waits_for_worker_before_releasing(store):
    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        release.wait(5)

    task = asyncio.create_task(store.io(work))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_log_rotation_tail_and_expiration(tmp_path):
    path = tmp_path / "service.log"
    path.write_bytes(b"oldest-" * 200)
    log = RotatingLog(path, max_bytes=100, count=5)
    log.write("first\n")
    assert all(p.stat().st_size <= 100 for p in tmp_path.glob("service.log*"))
    for i in range(20):
        log.write(f"line-{i:02d}-" + "x" * 30 + "\n")
    files = list(tmp_path.glob("service.log.[1-4]"))
    assert len(files) == 4 and path.stat().st_size <= 100
    assert b"line-19" in path.read_bytes()
    oldest = path.with_name("service.log.4")
    os.utime(oldest, (1,) * 2)
    log.write("")
    assert not oldest.exists() and path.exists()


def test_log_migration_preserves_archive_age_and_utf8(tmp_path):
    path = tmp_path / "service.log"
    path.write_text("中文" * 50, encoding="utf-8")
    archive = path.with_name("service.log.1")
    archive.write_bytes(b"old" * 100)
    os.utime(archive, (1,) * 2)
    RotatingLog(path, max_bytes=101).write("")
    assert not archive.exists()
    assert path.stat().st_size <= 101
    assert path.read_text("utf-8")


def test_log_cross_process_lock(tmp_path):
    module = Path(report_storage.__file__).with_name("disk_support.py")
    lock_path = tmp_path / "service.log.lock"
    script = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('disk', {str(module)!r}); "
        "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
        f"lock=mod.FileLock(mod.Path({str(lock_path)!r})); "
        "assert lock.acquire(); print('ready',flush=True); sys.stdin.readline(); lock.close()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        path = tmp_path / "service.log"
        RotatingLog(path).write("must skip")
        assert not path.exists()
    finally:
        child.communicate("done\n", timeout=10)
    RotatingLog(path).write("accepted")
    assert path.read_text() == "accepted"


def test_log_threaded_writes_remain_complete(tmp_path):
    path = tmp_path / "service.log"
    writers = [
        threading.Thread(target=lambda i=i: RotatingLog(path).write(f"完整日志-{i}\n"))
        for i in range(30)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert set(path.read_text("utf-8").splitlines()) == {
        f"完整日志-{i}" for i in range(30)
    }


def test_file_lock_releases_after_close(tmp_path):
    first = FileLock(tmp_path / "lock")
    second = FileLock(tmp_path / "lock")
    assert first.acquire() and not second.acquire()
    first.close()
    assert second.acquire()
    second.close()


def artifact(directory, suffix="md"):
    return next(directory.glob("*." + suffix), directory / ("report." + suffix))


@pytest.mark.parametrize("state,hours", [("success", 24), ("failed", 72)])
def test_gold_delivery_copies_expire_and_keep_diagnostics(store, state, hours):
    task = store.begin("黄金报告原文", "GOLD", "gold")
    assert task is not None
    store.finish(task, SimpleNamespace(png=b"image", width=100, height=100),
                 state, "complete", "")
    directory = store.root / task
    meta = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "gold" and meta["symbol"] == "GOLD"
    assert {Path(name).suffix for name in meta["files"]} == {".md", ".png"}
    assert all(name.startswith("gold_GOLD_") for name in meta["files"])
    assert artifact(directory).read_text(encoding="utf-8") == "黄金报告原文"
    store.now[0] += hours * 3600 - 1
    store.clean()
    assert directory.exists()
    store.now[0] += 1
    store.clean()
    assert not directory.exists()
    diagnostic = json.loads((store.diagnostics / f"{task}.json").read_text(encoding="utf-8"))
    assert diagnostic["kind"] == "gold" and diagnostic["cleanup_reason"] == "expired"


def test_interrupted_gold_delivery_keeps_failure_retention_window(store):
    task = store.begin("黄金报告原文", "GOLD", "gold")
    assert task is not None
    store.active.clear()
    store.clean()
    directory = store.root / task
    assert json.loads((directory / "task.json").read_text())["state"] == "interrupted"
    store.now[0] += 72 * 3600 - 1
    store.clean()
    assert directory.exists()
    store.now[0] += 1
    store.clean()
    assert not directory.exists()


def test_raw_cleanup_keeps_old_gold_reports(store, monkeypatch):
    formatter = store.data_dir / "instance/src/src/report/formatter.py"
    formatter.parent.mkdir(parents=True)
    formatter.write_bytes(b"known")
    monkeypatch.setattr(report_storage, "FORMATTER_SHA256", hashlib.sha256(b"known").hexdigest())
    gold = store.data_dir / "instance/reports/gold/GOLD/2026-10/GOLD_20261001_120000_1234.md"
    gold.parent.mkdir(parents=True)
    gold.write_text("黄金原始报告", encoding="utf-8")
    os.utime(gold, (store.now[0] - 20 * 86400,) * 2)
    store.clean_raw()
    assert gold.read_text(encoding="utf-8") == "黄金原始报告"
