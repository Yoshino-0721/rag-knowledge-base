"""前端页面测试。

这一层的价值不在"测样式"，而在于守住几条容易被改坏的产品约束：
关键交互元素不能少、页面必须自包含（面试演示时断网也能打开）。
"""

from __future__ import annotations

import re

from src.config import PROJECT_ROOT

HTML = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def test_has_mobile_viewport_meta():
    """要支持手机扫码演示，viewport 缺了整页会缩成一团。"""
    assert 'name="viewport"' in HTML
    assert "width=device-width" in HTML


def test_required_element_ids_present():
    """这些 id 是 JS 的挂载点，删任何一个都会让功能静默失效。"""
    required = [
        "messages",
        "input",
        "send",
        "upload",
        "ingest",
        "clearBtn",
        "settingsBtn",
        "filePicker",
        "status",
        "overlay",
        "keyInput",
        "chatModelInput",
        "embedModelInput",
        "embedDimInput",
        "topKInput",
        "rewriteToggle",
        "historyTurnsInput",
        "rerankToggle",
        "rerankCandidatesInput",
        "saveBtn",
        "cancelBtn",
    ]
    missing = [name for name in required if f'id="{name}"' not in HTML]
    assert missing == [], f"缺少元素：{missing}"


def test_page_is_self_contained():
    """不引任何外部脚本 / 样式 / 字体，离线也能完整打开。"""
    assert "<script src=" not in HTML
    assert "<link" not in HTML


def test_only_expected_external_url():
    """页面里只允许出现 API Key 申请地址这一个外链。"""
    urls = set(re.findall(r'https?://[^\s"\'<>]+', HTML))
    unexpected = {u for u in urls if not u.startswith("https://bigmodel.cn/")}
    assert unexpected == set(), f"出现非预期外链：{unexpected}"


def test_core_interaction_hooks_exist():
    """核心交互实现不能被误删。"""
    for token in [
        "renderCitations",   # 引用卡片
        "renderRich",        # 答案轻度 Markdown 渲染
        "cite-ref",          # [1] 引用角标
        "meter-fill",        # 相似度进度条
        "addThinking",       # 检索中的加载动画
        "renderReport",      # 建库报告卡片
        "dragover",          # 拖拽上传
    ]:
        assert token in HTML, f"缺少前端能力：{token}"


def test_answer_is_escaped_before_rendering():
    """答案会被拼成 HTML，必须先转义，防止文档内容里的尖括号注入。"""
    assert "function escapeHtml" in HTML
    assert "renderRich" in HTML and "escapeHtml(text)" in HTML


def test_citation_copy_button_available():
    assert "navigator.clipboard.writeText" in HTML


def test_example_questions_are_wired():
    """空状态里的示例问题应带上可直接提问的 data-q。"""
    assert HTML.count('class="chip"') >= 3
    assert 'data-q=' in HTML


def test_multi_turn_conversation_is_wired():
    """多轮对话：历史已由服务端按 session_id 从库中读取，前端不再携带。

    ``conversation`` / ``remember`` / ``clearConversation`` 仍然保留（本地记账 +
    清空入口），但提问请求里只出现 ``session_id`` —— 老协议必须消失，否则等于
    客户端还在自维护历史，服务端那份就是死数据。
    """
    assert "conversation" in HTML
    assert "remember(" in HTML
    assert "clearConversation" in HTML
    # 提问时只报会话号，历史由服务端从库里读
    assert "session_id" in HTML
    assert "history: conversation" not in HTML


def test_rewrite_hint_is_rendered():
    """追问改写是隐性能力，必须把「实际检索用的问题」显示出来才看得见。"""
    assert "renderRewritten" in HTML
    assert "rewritten" in HTML and "data.query" in HTML
    assert "检索用的问题已改写为" in HTML


def test_rerank_score_is_visible():
    """精排是这一版的核心卖点，前端要能看到它真的起了作用。"""
    assert "rerank_score" in HTML
    assert "Rerank" in HTML
    assert "向量" in HTML  # 向量分保留作对照
