"""测试基座：每个测试用独立临时数据库，隔离生产数据。"""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """独立 SQLite：set/get 设置与账号读写不污染生产库。"""
    monkeypatch.setenv("AGENT2API_DB", str(tmp_path / "test.db"))
    import db
    import importlib
    importlib.reload(db)
    db.init_db()
    yield db


@pytest.fixture(autouse=True)
def block_unstubbed_network(monkeypatch):
    """回归只访问测试替身，新增接入分支也不会意外请求真实上游。"""
    import requests

    def blocked(*args, **kwargs):
        raise AssertionError("test network boundary was not stubbed")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
