"""自举安装的归档、恢复与取消回归。"""
import asyncio
import ctypes
import importlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# 隔离 AstrBot 的包初始化副作用，子模块仍按正式包内导入。
PACKAGE = "bootstrap_test_plugin"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
bootstrap = importlib.import_module(f"{PACKAGE}.bootstrap")


def archive(member="project/pyproject.toml"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        if member == "symlink":
            link = zipfile.ZipInfo("project/link")
            link.external_attr = (0o120777 << 16)
            bundle.writestr(link, "../../outside")
            member = "project/pyproject.toml"
        bundle.writestr(member, "[project]\nname='sample'\n")
        bundle.writestr("project/requirements-core.lock.txt", "")
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("member,ok", [("project/pyproject.toml", True), ("../pyproject.toml", False), ("/pyproject.toml", False), ("symlink", False), ("project/C:escape", False), ("project/.. /escape", False), ("project/CON", False)])
async def test_archive_publication(tmp_path, monkeypatch, member, ok):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=archive(member)))
    original = httpx.AsyncClient
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    result = await bootstrap.download_source("https://example.org/source.zip", tmp_path)
    assert result.ok is ok
    assert (tmp_path / "src").exists() is ok
    if ok:
        info = json.loads((tmp_path / "src/.bootstrap-source.json").read_text())
        assert len(info["archive_sha256"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "corrupt"])
async def test_download_failure_unpublished(tmp_path, monkeypatch, failure):
    transport = httpx.MockTransport(lambda request: httpx.Response(503 if failure == "http" else 200, content=b"bad"))
    original = httpx.AsyncClient
    monkeypatch.setattr(bootstrap.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    assert not (await bootstrap.download_source("https://example.org/source.zip", tmp_path)).ok
    assert not (tmp_path / "src").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("uv,fail", [(None, False), ("C:/uv.exe", False), (None, True)])
async def test_install_recovery_and_locked_commands(tmp_path, monkeypatch, uv, fail):
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").touch()
    (source / "requirements-core.lock.txt").touch()
    (source / ".bootstrap-source.json").write_text(json.dumps({"archive_url": "https://example.org/a.zip", "archive_sha256": "a" * 64}))
    commands = []
    async def runner(cmd, cwd):
        commands.append(cmd)
        bootstrap.venv_python(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        bootstrap.venv_python(tmp_path).touch()
        bootstrap.venv_launcher(tmp_path).touch()
        return (1, "failed") if fail and "--require-hashes" in cmd else (0, "")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: uv)
    result = await bootstrap.ensure_instance(tmp_path, None, "", runner=runner)
    assert result.ok is not fail
    assert bootstrap.instance_ready(tmp_path, "") is not fail
    assert any("--require-hashes" in cmd for cmd in commands)
    if not fail:
        assert any("--no-deps" in cmd for cmd in commands)
        assert not bootstrap.instance_ready(tmp_path, "rag")
        before = len(commands)
        assert (await bootstrap.ensure_instance(tmp_path, None, "", runner=runner)).ok
        assert len(commands) == before


@pytest.mark.parametrize("existing", [False, True, "broken"])
def test_config_preserves_existing(tmp_path, existing):
    path = tmp_path / ".stock_robot/config.yaml"
    if existing:
        path.parent.mkdir()
        path.write_text("[broken" if existing == "broken" else "llm:\n  api_key: existing-key\n")
    result = bootstrap.write_config(tmp_path, None, 8765)
    assert result.ok is (existing != "broken")
    if existing is True:
        assert "existing-key" in path.read_text()
    elif existing is False:
        assert yaml.safe_load(path.read_text())["llm"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_command_reaps_child(tmp_path, monkeypatch, cancel):
    pid_path = tmp_path / "pid"
    if not cancel:
        monkeypatch.setattr(bootstrap, "INSTALL_TIMEOUT_SECONDS", .5)
    command = [sys.executable, "-c", f"import os,time;open({str(pid_path)!r},'w').write(str(os.getpid()));time.sleep(60)"]
    task = asyncio.create_task(bootstrap.run_command(command, tmp_path, log_path=tmp_path / "service.log"))
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(.02)
    assert pid_path.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        code, detail = await task
        assert code == 1
        assert "截止时间" in detail
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid_path.read_text()))
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            assert code.value != 259


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    "Authorization: Bearer dummy-review-secret",
    '{"api_key":"dummy-review-secret"}',
    "password = 'dummy-review-secret with spaces'",
    "x" * 8180 + '{"api_key": "dummy-review-secret"}' + "y" * 10000,
], ids=["bearer", "json", "spaces", "oversized"])
async def test_command_log_and_tail_redact_credentials(tmp_path, output):
    path = tmp_path / "service.log"
    code, tail = await bootstrap.run_command(
        [sys.executable, "-c", f"print({output!r})"], tmp_path, log_path=path,
    )
    assert code == 0
    assert "dummy-review-secret" not in tail
    assert "dummy-review-secret" not in path.read_text("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object 进程树回归")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_after_parent_exit_reaps_inherited_pipe_child(tmp_path, monkeypatch, cancel):
    parent_path, child_path = tmp_path / "parent-pid", tmp_path / "child-pid"
    child_script = f"import os,time;open({str(child_path)!r},'w').write(str(os.getpid()));time.sleep(60)"
    parent_script = (
        "import os,subprocess,sys,time,pathlib;"
        f"open({str(parent_path)!r},'w').write(str(os.getpid()));"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        f"p=pathlib.Path({str(child_path)!r});"
        "\nfor _ in range(200):\n if p.exists():break\n time.sleep(.01)"
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    def alive(pid):
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = ctypes.c_ulong()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(status))
            return status.value == 259
        finally:
            assert kernel.CloseHandle(handle)
    # 暂停自动父退出清理，确定性验证取消路径本身关闭 Job。
    watcher_gate = asyncio.Event()
    async def paused_watcher(process, job):
        await watcher_gate.wait()
    if cancel:
        monkeypatch.setattr(bootstrap, "_watch_parent_exit", paused_watcher)
    task = asyncio.create_task(bootstrap.run_command(
        [sys.executable, "-c", parent_script], tmp_path, log_path=tmp_path / "service.log",
    ))
    try:
        for _ in range(200):
            if parent_path.exists() and child_path.exists() and not alive(int(parent_path.read_text())):
                break
            await asyncio.sleep(.02)
        assert parent_path.exists() and child_path.exists()
        assert not alive(int(parent_path.read_text()))
        if cancel:
            assert alive(int(child_path.read_text()))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            code, _ = await asyncio.wait_for(task, 5)
            assert code == 0
        assert not alive(int(child_path.read_text()))
    finally:
        watcher_gate.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # 只针对本用例写出的 PID 清理失败产物。
        if child_path.exists() and alive(int(child_path.read_text())):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", child_path.read_text(), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
