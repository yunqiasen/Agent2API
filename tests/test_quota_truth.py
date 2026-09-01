"""额度业务错误不能伪装成空额度。"""

import providers as P


def test_collect_quota_surfaces_http_200_business_errors(isolated_db, monkeypatch):
    """WorkBuddy/CodeBuddy 的 200+非零 code 必须进入 errors。"""
    monkeypatch.setattr(P, "fetch_user_resource", lambda *a, **k: {
        "code": 9001, "msg": "上游业务失败",
    })
    monkeypatch.setattr(P, "fetch_dosage_notify", lambda *a, **k: {
        "code": 0, "data": {},
    })
    monkeypatch.setattr(P, "fetch_payment_type", lambda *a, **k: {
        "code": 0, "data": {},
    })
    out = P.collect_quota("workbuddy", "token")
    assert out["errors"] == ["user-resource: code=9001: 上游业务失败"]
    assert out["total"] == 0


def test_billing_endpoints_reject_http_errors_without_business_code(monkeypatch):
    """额度端点 HTTP 失败即使 JSON 没有 code，也不能当作空额度成功。"""
    import pytest

    class Resp:
        status_code = 500
        text = '{"data":{}}'

        def json(self):
            return {"data": {}}

    class Session:
        def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(P, "_session", lambda: Session())
    for fn in (P.fetch_user_resource, P.fetch_dosage_notify, P.fetch_payment_type):
        with pytest.raises(P.ProviderError, match="HTTP 500"):
            fn("workbuddy", "token")
