"""新版 Work 场景：目录与聊天请求使用相同 function，模型从上游动态读取。"""
from unittest.mock import Mock
import pytest
import trae
import providers as P


def test_current_work_catalog_discovers_new_models_without_hardcoding(monkeypatch):
    def post(url, *, headers, json, timeout):
        ids = ['step-5-preview', 'deepseek-v4.1-flash', 'glm-5.3', 'future-model'] if json['function'] == 'solo_work_remote' else ['glm-5.2']
        return Mock(status_code=200, json=lambda: {'config_info_list': [
            {'config_name': mid, 'display_config': {'display_name': mid}}
            for mid in ids]})
    monkeypatch.setattr(trae, '_session', lambda: Mock(post=post))
    assert P.fetch_upstream_models('trae', 'token', strict=True) == [
        'step-5-preview', 'deepseek-v4.1-flash', 'glm-5.3', 'future-model']


@pytest.mark.parametrize('model', ['step-5-preview', 'deepseek-v4.1-flash', 'glm-5.3', 'DeepSeek-V4-Pro-Official'])
def test_work_chat_uses_same_scene_as_catalog(model):
    body = {'model': model, 'messages': [{'role': 'user', 'content': 'hello'}]}
    out = P.normalize_body('trae', body)
    assert out['function'] == 'solo_work_remote'
    assert out['config_name'] == model
    assert 'function' not in body
