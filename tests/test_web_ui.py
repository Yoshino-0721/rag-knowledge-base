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
        "agentToggle",
        "agentStepsInput",
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
    """多轮对话：上下文要由前端携带，并提供清空入口。"""
    assert "conversation" in HTML
    assert "remember(" in HTML
    assert "clearConversation" in HTML
    # 提问时必须把历史一起发出去
    assert "history: conversation" in HTML


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


def test_tool_calls_are_rendered():
    """工具调用同样要看得见 —— 否则「模型查了天气」和「模型编了天气」无从分辨。"""
    assert "renderToolCalls" in HTML
    assert "data.tool_calls" in HTML
    assert "调用工具" in HTML
    assert "tool-state" in HTML, "成功 / 失败要能区分"
    assert "已返回" in HTML


def test_agent_settings_are_wired():
    assert "agent_enabled" in HTML
    assert "agent_max_steps" in HTML
    assert "agentToggle.checked" in HTML


def test_tool_call_rendering_escapes_untrusted_values():
    """工具标签、参数、返回内容都来自外部，必须走转义路径。

    标签与参数拼进 innerHTML，所以要显式 escapeHtml；返回内容用 textContent 写入。
    这条断言看着像在「测源码字符串」，但它守的是一个真实的 XSS 面：
    外部接口的返回一旦被当 HTML 解析，页面就能被注进去。
    """
    body = HTML.split("function renderToolCalls", 1)[1].split("/* ---------- 建库报告", 1)[0]

    assert "escapeHtml(call.label || call.name)" in body, "工具名要转义"
    assert "escapeHtml(args)" in body, "参数要转义"
    assert "pre.textContent = ok ? call.result : call.error" in body, "返回内容要走 textContent"
    assert "innerHTML = call.result" not in body
