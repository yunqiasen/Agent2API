"""两种容器构建方式的运行时源码应能脱离仓库启动应用。"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.fullbuild"])
def test_packaged_runtime_serves_health_without_repository_sources(tmp_path, dockerfile):
    # 只从最终阶段 COPY 声明的源码启动，避免仓库路径掩盖漏打包。
    instructions = (ROOT / dockerfile).read_text().replace("\\\n", " ").splitlines()
    runtime_start = max(i for i, line in enumerate(instructions)
                        if line.strip().upper().startswith("FROM "))
    bundle = tmp_path / "app"
    bundle.mkdir()
    for line in instructions[runtime_start:]:
        fields = shlex.split(line, comments=True)
        if not fields or fields[0].upper() != "COPY":
            continue
        if any(field.startswith("--from=") for field in fields):
            continue
        for source in fields[1:-1]:
            if source.endswith(".py"):
                for path in ROOT.glob(source):
                    shutil.copy2(path, bundle / path.name)

    environment = dict(os.environ, AGENT2API_DB=str(tmp_path / "packaging.db"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
import requests

def blocked(*args, **kwargs):
    raise AssertionError("packaging smoke test attempted an upstream request")

requests.sessions.Session.request = blocked
import app
response = app.app.test_client().get('/health')
assert response.status_code == 200
assert response.json['ok'] is True
assert response.json['service'] == 'agent2api'
""", str(bundle)],
        cwd=bundle, env=environment, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
