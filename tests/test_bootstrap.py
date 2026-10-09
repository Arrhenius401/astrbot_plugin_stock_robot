"""自举安装的归档、恢复与取消回归。"""

import asyncio
import ctypes
import importlib
import io
import json
import logging
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


@pytest.mark.asyncio
@pytest.mark.parametrize("uv", [None, "C:/uv.exe"])
async def test_custom_index_and_install_stages(tmp_path, monkeypatch, uv):
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").touch()
    (source / "requirements-core.lock.txt").touch()
    (source / ".bootstrap-source.json").write_text(
        json.dumps(
            {
                "archive_url": "https://example.org/a.zip",
                "archive_sha256": "a" * 64,
            }
        )
    )
    commands = []
    progress = bootstrap.InstallProgress(tmp_path / "service.log")

    async def runner(cmd, cwd):
        commands.append((cmd, progress.stage))
        bootstrap.venv_python(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        bootstrap.venv_python(tmp_path).touch()
        bootstrap.venv_launcher(tmp_path).touch()
        return 0, ""

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: uv)
    result = await bootstrap.ensure_instance(
        tmp_path,
        None,
        "",
        runner=runner,
        progress=progress,
        package_index_url="https://example.org/simple",
    )
    assert result.ok
    installs = [cmd for cmd, _ in commands if "install" in cmd]
    assert len(installs) == 2
    assert all(
        cmd[cmd.index("--index-url") + 1] == "https://example.org/simple"
        for cmd in installs
    )
    assert all(stage == 3 for cmd, stage in commands if "install" in cmd)
    assert commands[-1][1] == 4
    log = progress.log_path.read_text("utf-8")
    assert all(f"[{stage}/5]" in log for stage in range(1, 5))


@pytest.mark.asyncio
async def test_install_timeout_keeps_artifacts_and_reports_stage(tmp_path, monkeypatch):
    async def download(*args, **kwargs):
        (tmp_path / "src").mkdir()
        return bootstrap.StepResult(True)

    async def env(*args, progress, **kwargs):
        progress.enter(3, "安装依赖")
        progress.record(
            "Downloading scipy from https://user:secret@example.org/file?token=secret"
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(bootstrap, "download_source", download)
    monkeypatch.setattr(bootstrap, "make_env", env)
    result = await bootstrap.ensure_instance(
        tmp_path,
        None,
        "",
        runner=None,
        install_timeout=0.02,
        progress=bootstrap.InstallProgress(tmp_path / "service.log"),
    )
    assert not result.ok
    assert "后台安装超时" in result.detail and "[3/5]" in result.detail
    assert "scipy" in result.detail and "secret" not in result.detail
    assert (tmp_path / "src").exists()
    assert not (tmp_path / "install-state.json").exists()


def test_progress_heartbeat_without_output_and_redaction(tmp_path, caplog):
    progress = bootstrap.InstallProgress(tmp_path / "service.log")
    with caplog.at_level(logging.INFO, logger="plugin-test"):
        progress.enter(3, "安装依赖")
        progress.heartbeat()
        progress.record("api_key=secret")
        progress.heartbeat()
    assert "等待安装程序输出" in caplog.text
    assert "[3/5]" in caplog.text and "secret" not in caplog.text


def archive(member="project/pyproject.toml"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        if member == "symlink":
            link = zipfile.ZipInfo("project/link")
            link.external_attr = 0o120777 << 16
            bundle.writestr(link, "../../outside")
            member = "project/pyproject.toml"
        bundle.writestr(member, "[project]\nname='sample'\n")
        bundle.writestr("project/requirements-core.lock.txt", "")
    return output.getvalue()


@pytest.mark.asyncio
async def test_default_latest_release_records_tag_and_reuses_offline(
    tmp_path, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if str(request.url).endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v0.2.0",
                    "id": 42,
                    "draft": False,
                    "prerelease": False,
                },
            )
        assert (
            str(request.url)
            == "https://codeload.github.com/Arrhenius401/stock_robot/zip/refs/tags/v0.2.0"
        )
        return httpx.Response(200, content=archive())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    assert (await bootstrap.download_source(None, tmp_path)).ok
    info = json.loads((tmp_path / "src/.bootstrap-source.json").read_text())
    assert info["release_tag"] == "v0.2.0"
    assert info["release_id"] == 42
    assert info["release_resolution"] == "api"
    assert len(info["archive_sha256"]) == 64
    assert (await bootstrap.download_source(None, tmp_path)).ok
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,body",
    [
        (429, {}, {}),
        (403, {"x-ratelimit-remaining": "0"}, {}),
        (403, {"retry-after": "60"}, {}),
        (403, {}, {"message": "API rate limit exceeded"}),
    ],
)
async def test_rate_limit_web_fallback_and_offline_reuse(
    tmp_path, monkeypatch, status, headers, body
):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(status, headers=headers, json=body)
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://github.com/Arrhenius401/stock_robot/releases/tag/v1%2Bhotfix",
                },
            )
        assert (
            str(request.url)
            == "https://codeload.github.com/Arrhenius401/stock_robot/zip/refs/tags/v1%2Bhotfix"
        )
        return httpx.Response(200, content=archive())

    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    assert (await bootstrap.download_source(None, tmp_path)).ok
    info = json.loads((tmp_path / "src/.bootstrap-source.json").read_text())
    assert info["release_tag"] == "v1+hotfix"
    assert info["release_resolution"] == "web"
    assert "release_id" not in info
    assert len(info["archive_sha256"]) == 64
    assert (await bootstrap.download_source(None, tmp_path)).ok
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,location",
    [
        (200, ""),
        (404, ""),
        (503, ""),
        (302, ""),
        (302, "http://github.com/Arrhenius401/stock_robot/releases/tag/v1"),
        (302, "https://evil.example/Arrhenius401/stock_robot/releases/tag/v1"),
        (302, "https://github.com/other/repo/releases/tag/v1"),
        (302, "https://user@github.com/Arrhenius401/stock_robot/releases/tag/v1"),
        (
            302,
            "https://github.com/Arrhenius401/stock_robot/releases/tag/v1?token=secret",
        ),
        (302, "https://github.com/Arrhenius401/stock_robot/releases/tag/v1#fragment"),
        (302, "https://github.com/Arrhenius401/stock_robot/releases/tag/"),
        (302, "https://github.com/Arrhenius401/stock_robot/releases/tag/%2E%2E/main"),
        (302, "https://github.com/Arrhenius401/stock_robot/releases/tag/v1%3Fwrong"),
    ],
)
async def test_rate_limit_fallback_rejects_untrusted_redirect(
    tmp_path, monkeypatch, status, location
):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(429)
        assert request.url.host == "github.com"
        return httpx.Response(status, headers={"location": location})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    result = await bootstrap.download_source(None, tmp_path)
    assert not result.ok
    assert "限流" in result.detail
    assert not (tmp_path / "src").exists()
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,reason",
    [
        (404, {}, "尚无正式发布"),
        (403, {}, "403"),
        (200, {"tag_name": "v1", "id": 1, "draft": False, "prerelease": True}, "正式"),
        (200, {"tag_name": "", "id": 1, "draft": False, "prerelease": False}, "标签"),
        (200, [], "响应"),
    ],
)
async def test_latest_release_errors_leave_no_source(
    tmp_path, monkeypatch, status, body, reason
):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
            **kw,
        ),
    )
    result = await bootstrap.download_source(None, tmp_path)
    assert not result.ok
    assert reason in result.detail
    assert not (tmp_path / "src").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", ["", "rag"])
async def test_required_lock_missing_does_not_publish_source(
    tmp_path, monkeypatch, extras
):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("project/pyproject.toml", "[project]")
        if extras == "rag":
            bundle.writestr("project/requirements-core.lock.txt", "")
    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=output.getvalue())
            ),
            **kw,
        ),
    )
    result = await bootstrap.download_source(
        "https://example.org/source.zip", tmp_path, extras=extras
    )
    assert not result.ok
    assert "锁" in result.detail
    assert not (tmp_path / "src").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member,ok",
    [
        ("project/pyproject.toml", True),
        ("../pyproject.toml", False),
        ("/pyproject.toml", False),
        ("symlink", False),
        ("project/C:escape", False),
        ("project/.. /escape", False),
        ("project/CON", False),
    ],
)
async def test_archive_publication(tmp_path, monkeypatch, member, ok):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=archive(member))
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    result = await bootstrap.download_source("https://example.org/source.zip", tmp_path)
    assert result.ok is ok
    assert (tmp_path / "src").exists() is ok
    if ok:
        info = json.loads((tmp_path / "src/.bootstrap-source.json").read_text())
        assert len(info["archive_sha256"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "corrupt"])
async def test_download_failure_unpublished(tmp_path, monkeypatch, failure):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503 if failure == "http" else 200, content=b"bad"
        )
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(
        bootstrap.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    assert not (
        await bootstrap.download_source("https://example.org/source.zip", tmp_path)
    ).ok
    assert not (tmp_path / "src").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("uv,fail", [(None, False), ("C:/uv.exe", False), (None, True)])
async def test_install_recovery_and_locked_commands(tmp_path, monkeypatch, uv, fail):
    source = tmp_path / "src"
    source.mkdir()
    (source / "pyproject.toml").touch()
    (source / "requirements-core.lock.txt").touch()
    (source / ".bootstrap-source.json").write_text(
        json.dumps(
            {"archive_url": "https://example.org/a.zip", "archive_sha256": "a" * 64}
        )
    )
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
        path.write_text(
            "[broken" if existing == "broken" else "llm:\n  api_key: existing-key\n"
        )
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
        monkeypatch.setattr(bootstrap, "INSTALL_TIMEOUT_SECONDS", 0.5)
    command = [
        sys.executable,
        "-c",
        f"import os,time;open({str(pid_path)!r},'w').write(str(os.getpid()));time.sleep(60)",
    ]
    task = asyncio.create_task(
        bootstrap.run_command(command, tmp_path, log_path=tmp_path / "service.log")
    )
    for _ in range(100):
        if pid_path.exists():
            break
        await asyncio.sleep(0.02)
    assert pid_path.exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        code, detail = await task
        assert code == 1
        assert "安装命令超时" in detail and "0.5 秒" in detail
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(
            0x1000, False, int(pid_path.read_text())
        )
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            assert code.value != 259


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "Authorization: Bearer dummy-review-secret",
        '{"api_key":"dummy-review-secret"}',
        "password = 'dummy-review-secret with spaces'",
        "x" * 8180 + '{"api_key": "dummy-review-secret"}' + "y" * 10000,
    ],
    ids=["bearer", "json", "spaces", "oversized"],
)
async def test_command_log_and_tail_redact_credentials(tmp_path, output):
    path = tmp_path / "service.log"
    code, tail = await bootstrap.run_command(
        [sys.executable, "-c", f"print({output!r})"],
        tmp_path,
        log_path=path,
    )
    assert code == 0
    assert "dummy-review-secret" not in tail
    assert "dummy-review-secret" not in path.read_text("utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object 进程树回归")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_after_parent_exit_reaps_inherited_pipe_child(
    tmp_path, monkeypatch, cancel
):
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
    kernel.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
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
    task = asyncio.create_task(
        bootstrap.run_command(
            [sys.executable, "-c", parent_script],
            tmp_path,
            log_path=tmp_path / "service.log",
        )
    )
    try:
        for _ in range(200):
            if (
                parent_path.exists()
                and child_path.exists()
                and not alive(int(parent_path.read_text()))
            ):
                break
            await asyncio.sleep(0.02)
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
                "taskkill",
                "/PID",
                child_path.read_text(),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
