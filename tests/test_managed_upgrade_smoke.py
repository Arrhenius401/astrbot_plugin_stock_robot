"""离线真实 venv/入口程序/HTTP 子进程升级回滚；不等同于 AstrBot 平台验收。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket

import httpx
import pytest

bootstrap = importlib.import_module("stock_robot_plugin_test.bootstrap")
launcher = importlib.import_module("stock_robot_plugin_test.launcher")

pytestmark = pytest.mark.skipif(
    not os.environ.get("STOCK_ROBOT_SMOKE_BUILD_PATH"),
    reason="显式提供已有 setuptools/wheel 路径才运行离线真实环境冒烟",
)


@pytest.mark.asyncio
async def test_real_venv_candidate_failure_restores_legacy_http_service(tmp_path, monkeypatch, caplog):
    async def fixture_source(url, instance, *, extras="", release_tag=None):
        source = instance / "src"
        source.mkdir(parents=True, exist_ok=True)
        (source / "requirements-core.lock.txt").touch()
        (source / "pyproject.toml").write_text(
            '[build-system]\nrequires=["setuptools", "wheel"]\n'
            'build-backend="setuptools.build_meta"\n'
            '[project]\nname="stock-robot-upgrade-probe"\nversion="0.0.1"\n'
            '[project.scripts]\nstock-robot="probe_cli:main"\n'
            '[tool.setuptools]\npy-modules=["probe_cli"]\n', encoding="utf-8",
        )
        (source / "probe_cli.py").write_text(
            "import sys\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
            f"VERSION={release_tag!r}\n"
            "def main():\n"
            " if '--help' in sys.argv:\n"
            "  print('probe '+VERSION); return\n"
            " if VERSION == 'v0.2.0':\n"
            "  print('candidate startup failure', flush=True); raise SystemExit(2)\n"
            " class H(BaseHTTPRequestHandler):\n"
            "  def do_GET(self):\n"
            "   self.send_response(200); self.end_headers()\n"
            "   self.wfile.write(b'{\"status\":\"ok\",\"version\":\"v0.1.0\"}')\n"
            " host=sys.argv[sys.argv.index('--host')+1]\n"
            " port=int(sys.argv[sys.argv.index('--port')+1])\n"
            " HTTPServer((host,port),H).serve_forever()\n", encoding="utf-8",
        )
        (source / ".bootstrap-source.json").write_text(json.dumps({
            "archive_url": url, "archive_sha256": "a" * 64,
            "release_tag": release_tag,
        }))
        return bootstrap.StepResult(True)

    async def offline_runner(cmd, cwd):
        # 只对测试小包复用已有构建后端；真实 venv 与入口脚本由 pip 安装。
        arguments = list(cmd)
        if "install" in arguments:
            arguments += ["--no-index"]
            if "--no-deps" in arguments:
                arguments += ["--no-build-isolation"]
        env = dict(os.environ, PYTHONPATH=os.environ["STOCK_ROBOT_SMOKE_BUILD_PATH"])
        process = await asyncio.create_subprocess_exec(
            *arguments, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 90)
        return process.returncode or 0, output.decode("utf-8", errors="replace")

    monkeypatch.setattr(bootstrap, "download_source", fixture_source)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    instance = tmp_path / "instance"
    result = await bootstrap.ensure_instance(instance, None, "", runner=offline_runner,
                                             expected_release_tag="v0.1.0")
    assert result.ok, result.detail
    (instance / ".stock_robot").mkdir()
    config = instance / ".stock_robot/config.yaml"
    config.write_text("llm: {enabled: false}\n")
    (instance / "reports").mkdir()
    report = instance / "reports/retained.md"
    report.write_text("retained report")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service = launcher.ServiceLauncher(
        base_url=f"http://127.0.0.1:{port}", archive_url=None, extras="",
        auto_install=True, timeout=5, data_dir=tmp_path,
        runner=offline_runner, managed_release_tag="v0.2.0",
    )
    try:
        outcome = await service._prepare(allow_install=True)
        assert outcome.ok, outcome.reason
        assert bootstrap.runtime_path(instance) == instance
        assert service._process is not None and service._process.returncode is None
        async with httpx.AsyncClient() as client:
            health = (await client.get(service.base_url + "/health")).json()
        assert health == {"status": "ok", "version": "v0.1.0"}
        assert config.read_text() == "llm: {enabled: false}\n"
        assert report.read_text() == "retained report"
        candidate = next((instance / ".releases").glob("release-*"))
        code, output = await offline_runner([str(bootstrap.venv_launcher(candidate)), "--help"], instance)
        assert code == 0 and "v0.2.0" in output
        assert "candidate startup failure" in (tmp_path / "service.log").read_text("utf-8")
        assert any("已恢复旧版本" in record.message for record in caplog.records)
    finally:
        await service.stop()
    assert service._process is None
