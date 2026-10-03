"""共享准备任务与自有进程生命周期回归。"""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import socket
import sys
import types
from pathlib import Path

import pytest
import respx
from httpx import Response

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = types.ModuleType("launcher_test_plugin")
PACKAGE.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE.__name__, PACKAGE)
launcher = importlib.import_module(f"{PACKAGE.__name__}.launcher")


def make_launcher(tmp_path, **overrides):
    async def runner(*_):
        return 0, ""

    options = {"base_url": "http://127.0.0.1:25618", "archive_url": None, "extras": "",
               "auto_install": True, "timeout": 0.05, "data_dir": tmp_path, "runner": runner}
    options.update(overrides)
    return launcher.ServiceLauncher(**options)


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.killed += 1
        self.returncode = -1

    async def wait(self):
        return self.returncode


@pytest.fixture(autouse=True)
def reap_fake_process(monkeypatch):
    original = launcher.reap_process

    async def reap(proc):
        if isinstance(proc, FakeProcess):
            assert proc.returncode is not None
            return
        await original(proc)

    monkeypatch.setattr(launcher, "reap_process", reap)


@pytest.mark.asyncio
async def test_shared_task_waiters_timeout_and_cancel_do_not_cancel_preparation(tmp_path, monkeypatch):
    service = make_launcher(tmp_path)
    gate = asyncio.Event()
    calls = 0

    async def prepare(allow_install):
        nonlocal calls
        calls += 1
        assert allow_install
        await gate.wait()
        return launcher.ReadyOutcome(True)

    monkeypatch.setattr(service, "_prepare", prepare)
    service.start_background()
    results = await asyncio.gather(*(service.ensure_ready(wait_timeout=0.01) for _ in range(2)))
    assert not any(result.ok for result in results)
    waiter = asyncio.create_task(service.ensure_ready(wait_timeout=1))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert service._task is not None and not service._task.cancelled()
    gate.set()
    assert (await service.ensure_ready(wait_timeout=1)).ok
    assert calls == 1
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,expected", [(200, {"status": "ok"}, True),
    (200, [], False), (200, {"status": "bad"}, False), (503, {"status": "ok"}, False),
    (200, "bad-json", False)])
async def test_health_response_contract(status, payload, expected):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    with respx.mock:
        respx.get("http://127.0.0.1:12345/health").mock(return_value=Response(status, text=body))
        assert await launcher.probe_health("http://127.0.0.1:12345") is expected


@pytest.mark.asyncio
async def test_log_pump_redacts_split_and_oversized_lines(tmp_path):
    service = make_launcher(tmp_path)
    reader = asyncio.StreamReader()
    output = io.StringIO()
    task = asyncio.create_task(service._pump_log(reader, output))
    for block in (b'Authorization: Bearer split-', b'secret\n',
                  b'prefix ' + b'x' * 9000, b' token=long-secret\n',
                  b'{"api_key": "tail-secret"}'):
        reader.feed_data(block)
        await asyncio.sleep(0)
    reader.feed_eof()
    await task
    log = output.getvalue()
    assert all(secret not in log for secret in ("split-secret", "long-secret", "tail-secret"))
    assert "过长服务日志行已略去" in log


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "llm: [", "llm: secret-key"])
async def test_call_does_not_repair_invalid_config_or_install(tmp_path, monkeypatch, content):
    service = make_launcher(tmp_path)
    if content is not None:
        configured(service)
        path = service.instance / ".stock_robot" / "config.yaml"
        path.write_text(content, encoding="utf-8")

    async def health(_):
        return False

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "instance_ready", lambda *_: True)
    result = await service.ensure_ready(wait_timeout=1)
    assert not result.ok and "配置文件" in result.reason
    assert "secret-key" not in result.reason
    assert service._process is None
    if content is not None:
        assert path.read_text("utf-8") == content
    await service.stop()


@pytest.mark.asyncio
async def test_confirmed_exit_allows_exactly_one_restart(tmp_path, monkeypatch):
    service = make_launcher(tmp_path)
    configured(service)
    old = FakeProcess(5)
    service._process = old
    new = FakeProcess()
    spawns = 0

    async def health(_):
        return spawns > 0

    async def spawn(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        return new

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "instance_ready", lambda *_: True)
    monkeypatch.setattr(launcher, "spawn_process", spawn)
    results = await asyncio.gather(*(service.ensure_ready(wait_timeout=1) for _ in range(2)))
    assert all(result.ok for result in results) and spawns == 1
    assert old.terminated == 0 and service._process is new
    await service.stop()


@pytest.mark.asyncio
async def test_real_http_child_start_and_stop(tmp_path, monkeypatch):
    # 动态端口仅用于此冒烟，正式产品仍从 base_url 推导端口。
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service = make_launcher(tmp_path, base_url=f"http://127.0.0.1:{port}", timeout=5)
    configured(service)
    script = tmp_path / "server.py"
    script.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import sys\n"
        "print('Authorization: Bearer raw-bearer-secret', flush=True)\n"
        "print('{\"api_key\": \"raw-json-secret\"}', flush=True)\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'{\"status\":\"ok\"}')\n"
        "HTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()\n",
        encoding="utf-8",
    )
    original_spawn = launcher.spawn_process

    async def spawn(*args, **kwargs):
        return await original_spawn(sys.executable, str(script), args[-3], args[-1], **kwargs)

    monkeypatch.setattr(launcher, "instance_ready", lambda *_: True)
    monkeypatch.setattr(launcher, "spawn_process", spawn)
    proc = None
    try:
        assert (await service.ensure_ready(wait_timeout=8)).ok
        proc = service._process
        assert proc is not None and proc.returncode is None
    finally:
        await service.stop()
    assert proc is not None and proc.returncode is not None
    log = (tmp_path / "service.log").read_text("utf-8")
    assert "raw-bearer-secret" not in log and "raw-json-secret" not in log
    assert "已隐藏" in log
    assert service._log_task is None and service._log is None
    assert not (await service.ensure_ready(wait_timeout=1)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("url,enabled", [("http://remote.example:123", True),
    ("https://localhost:123", True), ("http://localhost:123/path", True),
    ("http://localhost:123", False)])
async def test_reuse_only_never_installs_or_spawns(tmp_path, monkeypatch, url, enabled):
    healthy = False

    async def health(_):
        return healthy

    async def forbidden(*args, **kwargs):
        pytest.fail("复用模式不能安装或创建进程")

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "ensure_instance", forbidden)
    monkeypatch.setattr(launcher, "spawn_process", forbidden)
    service = make_launcher(tmp_path, base_url=url, auto_install=enabled)
    assert not (await service.ensure_ready(wait_timeout=1)).ok
    healthy = True
    assert (await service.ensure_ready(wait_timeout=1)).ok
    assert service._process is None
    await service.stop()


@pytest.mark.asyncio
async def test_live_unhealthy_owned_process_is_retained(tmp_path, monkeypatch):
    async def unhealthy(_):
        return False

    monkeypatch.setattr(launcher, "probe_health", unhealthy)
    service = make_launcher(tmp_path)
    proc = FakeProcess()
    service._process = proc
    result = await service.ensure_ready(wait_timeout=1)
    assert not result.ok and "仍运行" in result.reason
    assert service._process is proc and proc.terminated == 0
    await service.stop()
    assert proc.terminated == 1


def configured(service):
    path = service.instance / ".stock_robot" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("llm:\n  enabled: false\n", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["ready", "exit", "timeout"])
async def test_initial_start_and_failure_cleanup(tmp_path, monkeypatch, behavior):
    service = make_launcher(tmp_path)
    configured(service)
    proc = FakeProcess(1 if behavior == "exit" else None)
    calls = 0

    async def health(_):
        return calls > 0 and behavior == "ready"

    async def spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["cwd"] == service.instance
        assert args[-4:] == ("--host", "127.0.0.1", "--port", "25618")
        return proc

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "instance_ready", lambda *_: True)
    monkeypatch.setattr(launcher, "spawn_process", spawn)
    results = await asyncio.gather(*(service.ensure_ready(wait_timeout=1) for _ in range(2)))
    assert calls == 1
    assert all(r.ok for r in results) == (behavior == "ready")
    if behavior != "ready":
        assert proc.returncode is not None and service._process is None
    await service.stop()
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_stop_during_creation_obtains_and_reaps_process(tmp_path, monkeypatch):
    service = make_launcher(tmp_path)
    configured(service)
    entered, release = asyncio.Event(), asyncio.Event()
    proc = FakeProcess()

    async def health(_):
        return False

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "instance_ready", lambda *_: True)
    monkeypatch.setattr(launcher, "spawn_process", spawn)
    service.start_background()
    await entered.wait()
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    assert not (await service.ensure_ready(wait_timeout=1)).ok
    release.set()
    await stopping
    assert proc.terminated == 1 and service._process is None
    await service.stop()


@pytest.mark.asyncio
async def test_failed_install_is_not_repeated_during_call(tmp_path, monkeypatch):
    calls = 0

    async def health(_):
        return False

    async def install(*args, **kwargs):
        nonlocal calls
        calls += 1
        return launcher.StepResult(False, "安装失败")

    monkeypatch.setattr(launcher, "probe_health", health)
    monkeypatch.setattr(launcher, "instance_ready", lambda *_: False)
    monkeypatch.setattr(launcher, "ensure_instance", install)
    service = make_launcher(tmp_path)
    service.start_background()
    assert not (await service.ensure_ready(wait_timeout=1)).ok
    assert not (await service.ensure_ready(wait_timeout=1)).ok
    assert calls == 1
    await service.stop()
