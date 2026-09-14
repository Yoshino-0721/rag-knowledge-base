"""多用户前端护栏：登录页、管理后台页与工作台的新契约。

与 ``test_web_ui.py`` 的分工：
- ``test_web_ui.py`` 守的是 index.html 里**既有的**交互能力（引用卡片、改写提示、
  Rerank 展示、示例问题、转义……）—— 那些一条都不能少，本文件不去重复它们；
- 本文件守的是**这次多用户改造新加的东西**：三个页面各自自包含、登录页与后台页的
  结构、以及工作台请求协议的变更（不再由前端携带 history，改带 session_id）。

协议变更那一条是重点：改造前前端把 ``history`` 一起发给服务端，改造后历史由服务端
按 ``session_id`` 从数据库读 —— 这条断言必须跟着翻转，否则会变成"测试在守一个
已经不存在的协议"，两边同时腐烂。
"""

from __future__ import annotations

import re

import pytest

from src.config import PROJECT_ROOT

PAGES = {
    "index": PROJECT_ROOT / "web" / "index.html",
    "login": PROJECT_ROOT / "web" / "login.html",
    "admin": PROJECT_ROOT / "web" / "admin.html",
}


def read_page(name: str) -> str:
    path = PAGES[name]
    assert path.is_file(), f"缺少前端文件：{path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 三个页面共同约束


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_exists(name):
    read_page(name)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_is_self_contained(name):
    """零 CDN：不引外部脚本 / 样式 / 字体，断网也能完整打开。"""
    html = read_page(name)

    assert "<script src=" not in html
    assert "<link" not in html
    assert not re.search(r"<script[^>]+src\s*=", html)
    # 任何 src= / href= 指向外网都算违约（登录页连"申请 Key"的外链都刻意不放）
    remote = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html)
    assert remote == [], f"{name}.html 出现外网资源：{remote}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_has_mobile_viewport(name):
    html = read_page(name)
    assert 'name="viewport"' in html
    assert "width=device-width" in html


@pytest.mark.parametrize("name", ["index", "admin"])
def test_page_escapes_untrusted_text(name):
    """把不可信文本拼进 HTML 的页面必须有 escapeHtml。

    index 展示文档内容与模型答案，admin 展示的是**别人的**文档名与消息正文 ——
    admin 漏一处转义就是跨用户 XSS。登录页刻意不在名单里：它只用 textContent
    写错误提示，不拼 HTML，给它加一个用不到的 escapeHtml 反而是死代码。
    """
    html = read_page(name)
    assert "function escapeHtml" in html
    for token in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert token in html, f"{name}.html 的 escapeHtml 未覆盖 {token}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_boot_is_deferred_not_top_level(name):
    """启动逻辑必须挂在 DOMContentLoaded 上，顶层不得有副作用。

    这不是洁癖：项目二会用 ``new Function(整个 <script>)`` 求值脚本做渲染断言，
    只 stub ``document`` / ``window`` / ``fetch``，**没有 ``localStorage`` / ``location``**。
    顶层碰它们会直接抛 ReferenceError，把那条断言打崩。
    """
    html = read_page(name)
    assert "DOMContentLoaded" in html


# ---------------------------------------------------------------- 登录页


def test_login_page_has_login_and_register_tabs():
    html = read_page("login")

    for element_id in ("loginForm", "registerForm", "loginAccount", "loginPassword",
                       "regUsername", "regEmail", "regPassword", "loginBtn", "regBtn"):
        assert f'id="{element_id}"' in html, f"登录页缺少元素 #{element_id}"

    assert "登录" in html and "注册" in html


def test_login_page_uses_the_auth_contract():
    """必须与 /api/auth/* 的真实契约对齐，否则页面根本登录不了。"""
    html = read_page("login")

    assert "/api/auth/login" in html
    assert "/api/auth/register" in html
    assert "auth_token" in html          # localStorage 键名
    assert "Authorization" in html and "Bearer" in html
    assert "'/admin'" in html or '"/admin"' in html   # 管理员落点
    assert "role" in html                # 按角色跳转


# ---------------------------------------------------------------- 工作台（新契约）


def test_index_gates_on_token_and_redirects_to_login():
    html = read_page("index")

    assert "auth_token" in html
    assert "'/login'" in html or '"/login"' in html
    assert "/api/auth/me" in html


def test_index_uses_bearer_header_for_all_requests():
    """所有请求都要带登录态：必须有一个统一的请求封装，而不是散落的裸 fetch。"""
    html = read_page("index")

    assert "Authorization" in html
    assert "apiFetch" in html or "authFetch" in html
    assert "Bearer" in html


def test_index_has_session_sidebar():
    """左侧会话栏：新建 / 切换 / 删除。"""
    html = read_page("index")

    assert "/api/sessions" in html
    for element_id in ("sessionList", "newSessionBtn"):
        assert f'id="{element_id}"' in html, f"工作台缺少元素 #{element_id}"
    assert "/messages" in html           # 切换会话时拉历史消息与引用


def test_index_sends_session_id_instead_of_client_history():
    """协议翻转：历史由服务端按 session_id 从库里读，前端不再携带 history。"""
    html = read_page("index")

    assert "session_id" in html
    # 老的写法必须消失 —— 否则等于客户端还在自己维护历史，服务端那份就是死数据
    assert "history: conversation" not in html


def test_index_hides_settings_from_non_admin():
    """站点设置只对管理员开放：前端要按角色决定是否展示入口。"""
    html = read_page("index")

    assert "settingsBtn" in html         # 既有 id 不能丢
    assert "admin" in html               # 角色判断
    assert "changePwd" in html or "change-password" in html
    assert "logout" in html.lower()


# ---------------------------------------------------------------- 管理后台


def test_admin_page_has_all_five_tabs():
    html = read_page("admin")

    for tab in ("stats", "users", "sessions", "documents", "system"):
        assert f'data-tab="{tab}"' in html, f"管理后台缺少 {tab} 面板"
        assert f'id="panel-{tab}"' in html, f"管理后台缺少 panel-{tab} 容器"

    for label in ("统计面板", "用户管理", "会话管理", "数据管理", "系统操作"):
        assert label in html, f"管理后台缺少标签文案：{label}"


def test_admin_page_calls_the_real_admin_endpoints():
    """逐条对齐 /api/admin/* 的真实路由，别写出一个调不到数据的后台。"""
    html = read_page("admin")

    for path in (
        "/api/admin/stats",
        "/api/admin/users",
        "/api/admin/sessions",
        "/api/admin/documents",
        "/api/admin/system/reindex",
        "/api/admin/system/clear-cache",
        "/api/admin/system/health",
    ):
        assert path in html, f"管理后台没有调用 {path}"

    assert "/reset-password" in html


def test_admin_page_shows_denied_state_for_normal_users():
    """普通用户打开 /admin 要看到明确的无权限提示，而不是一个空壳界面。

    注意：这只是体验；真正的闸门是服务端的 require_admin（API 一律 403）。
    """
    html = read_page("admin")

    assert "权限" in html
    assert "/api/auth/me" in html


def test_admin_page_has_no_execution_entry():
    """后台只是只读审计入口，不提供任何"以某用户身份执行"的能力。"""
    html = read_page("admin")

    assert "/api/ask" not in html
    assert "/query" not in html
    assert "eval(" not in html
