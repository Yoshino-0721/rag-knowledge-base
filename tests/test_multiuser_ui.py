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
import shutil
import subprocess
from pathlib import Path

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


def test_login_page_handles_forced_password_change():
    """被强制改密的账号在登录页就要被按在改密上（模块 6.5 / 9 的前端一半）。

    服务端另有硬闸门（改密前除 /api/auth/me 与改密接口外一律 403），所以前端
    要做的是两件事，这里各断言一件：

    1. **不假装有"跳过"这条路** —— 改密屏有输入框与提交按钮，且标签栏会被隐藏；
    2. **改密后换用响应里新签发的 token** —— 漏了这步，改密成功后的下一个请求
       就是 401，用户会以为"改密把账号弄坏了"。
    """
    html = read_page("login")

    for element_id in ("changePwdForm", "pwdCurrent", "pwdNew", "pwdConfirm",
                       "pwdBtn", "authTabs"):
        assert f'id="{element_id}"' in html, f"登录页缺少元素 #{element_id}"

    assert "must_change_password" in html
    assert "/api/auth/change-password" in html
    assert "data.token" in html

    # 之前那版页面写着"默认管理员 admin / admin123" —— 默认口令早就取消了，
    # 这句话不能再回来（它会把用户引到一个根本不存在的口令上）。
    assert "admin123" not in html


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


def test_index_has_forced_password_change_gate():
    """首页的强制改密闸门：元素、文案位、以及"关不掉"那一行守卫都得在。

    行为层面由 `index_pwd_flow_check.js` 真跑一遍；这里守的是静态契约 ——
    id 被改名 / 守卫那行被顺手删掉时，测试要立刻红，而不是等用户点一下 Esc 发现。
    """
    html = read_page("index")

    assert 'id="pwdTitle"' in html
    assert 'id="pwdSub"' in html
    assert 'id="pwdNotice"' in html
    assert "function forcePasswordChange" in html
    assert "if (pwdForced) return;" in html      # 取消 / Esc / 点遮罩 / 右上角共用的守卫
    assert "openPwdDialog(false)" in html        # 主动改密：不能被点击事件对象污染成强制模式
    assert "await forcePasswordChange()" in html  # 改完才继续加载业务数据


# ---------------------------------------------------------------- 设计令牌（docs/design-tokens.md）

# 模块 11 逐页收敛：改完一页就把它加进来，之后不许再退回硬编码颜色。
CONVERGED_PAGES = ("login", "index", "admin")


@pytest.mark.parametrize("name", sorted(PAGES))
def test_pages_only_reference_defined_design_tokens(name):
    """每个 `var(--x)` 都要在 `:root` 里有定义。

    收敛令牌时最容易犯的错是"改名改了一半"：引用了不存在的令牌时浏览器会**静默**丢弃
    那条声明 —— 页面看着"只是有点不对"，功能测试却全绿。这类错误只能静态抓。
    """
    css = "\n".join(
        re.findall(r"<style[^>]*>(.*?)</style>", read_page(name), re.S)
    )
    root = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert root, f"{name} 页面没有 :root 令牌块"

    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", root.group(1)))
    used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", css))

    assert used <= defined, f"{name} 引用了未定义的令牌：{sorted(used - defined)}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_css_is_structurally_sound(name):
    """样式块的括号配平、声明形状、`var()` 闭合。

    浏览器对 CSS 的错误处理是**静默**的：花括号不配平会吞掉它后面的所有规则，
    `var(` 少一个右括号会让整条声明失效 —— 表现只是"页面有点不对"，功能测试全绿。
    模块 11 是**大面积机械改样式**，这类错误必须由静态检查兜住。
    """
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", read_page(name), re.S))
    assert css, f"{name} 页面没有样式块"

    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, f"{name} 样式块出现多余的 `}}`"
    assert depth == 0, f"{name} 样式块花括号不配平（结束时深度 {depth}）"
    assert css.count("(") == css.count(")"), f"{name} 样式块圆括号不配平"
    assert ";;" not in css, f"{name} 样式块出现空声明"

    for match in re.finditer(r"var\(", css):
        tail = css[match.start():match.start() + 80]
        assert re.match(r"var\(\s*--[a-z0-9-]+\s*[,)]", tail), (
            f"{name} 有未闭合或形状不对的 var()：{tail[:40]!r}"
        )


def test_converged_pages_have_no_hardcoded_colors():
    """已收敛的页面里，`:root` 之外不应再有颜色字面量。

    这是把模块 11 的成果锁住的那道棘轮：令牌集中定义之后，新写的样式一旦又手抄一个
    `#4c8dff`，就再也回不到"一处改、全站变"的状态了。
    （代码高亮配色属于内容语义，等 index 收敛时单独收进 `--code-*` 再放宽这条。）
    """
    for name in CONVERGED_PAGES:
        html = read_page(name)
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        root = re.search(r":root\s*\{(.*?)\}", css, re.S)
        assert root, f"{name} 页面没有 :root 令牌块"

        outside = css.replace(root.group(0), "")
        leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", outside)
        assert not leftovers, (
            f"{name} 页面在 :root 之外还有 {len(leftovers)} 处硬编码颜色："
            f"{sorted(set(leftovers))[:8]} —— 请改用已定义的令牌"
        )


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


def test_admin_page_has_forced_password_change_gate():
    """后台同样要认 must_change_password：管理员也会被建号流程闸住。

    行为层面由 `admin_pwd_flow_check.js` 真跑一遍；这里守静态契约。
    """
    html = read_page("admin")

    assert 'id="pwdTitle"' in html
    assert 'id="pwdSub"' in html
    assert 'id="pwdNotice"' in html
    assert "function forcePasswordChange" in html
    assert "if (pwdForced) return;" in html
    assert "openPwdDialog(false)" in html
    assert "await forcePasswordChange()" in html


def test_admin_page_builds_accounts_with_one_time_password():
    """建号入口：表单 -> POST /api/admin/users，且**一次性口令必须落在页面上**。

    服务端只存 bcrypt 哈希，口令一旦没显示出来就再也拿不回（只能再去点重置）——
    所以这条断言盯的是"口令有没有真的出现在界面里 + 有没有说清只出现这一次"，
    而不是"有没有调这个接口"。
    """
    html = read_page("admin")

    assert 'id="newUserName"' in html
    assert 'id="newUserEmail"' in html
    assert 'id="newUserRole"' in html
    assert 'id="newUserBtn"' in html
    assert 'id="newUserResult"' in html
    assert "api('/api/admin/users'" in html
    assert "只显示这一次" in html


# ---------------------------------------------------------------- 行为断言（Node）

MANAGED_NODE = Path(
    r"C:\Users\YoshinoCiallo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
)


def _node_binary():
    found = shutil.which("node")
    if found:
        return found
    return str(MANAGED_NODE) if MANAGED_NODE.is_file() else None


def test_login_flow_behaviour_assertions_pass():
    """用 Node 驱动登录页的**真实脚本**，验证强制改密流程的行为。

    静态断言只能证明"页面里有这些元素"，证明不了"到底放不放行、改密后有没有换
    token" —— 而这两件恰恰是前端最该守住的地方。浏览器在本机不可用
    （Edge 在会话中运行时 CLI 不输出 DOM），所以用 Node + DOM 替身求值真实脚本，
    与项目二 `ui_render_check.js` 同一思路。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过登录页流程断言")

    script = Path(__file__).resolve().parent / "login_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["login"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"登录页流程断言失败：\n{result.stdout}\n{result.stderr}"


def test_index_pwd_flow_behaviour_assertions_pass():
    """用 Node 驱动首页的**真实脚本**，验证"被闸住 → 改密 → 才放行"这条闸门。

    `must_change_password=true` 的账号在改密前，服务端除身份与改密接口外一律 403；
    首页必须先把人按在关不掉的改密框上，改完换用新 token 再继续加载业务数据。
    这条链路跨了「按钮绑定 / 关闭守卫 / token 轮换 / boot 放行」四件事，
    静态断言一条都证明不了 —— 守卫那行删掉，页面照样"看起来是对的"。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过首页流程断言")

    script = Path(__file__).resolve().parent / "index_pwd_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["index"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"首页流程断言失败：\n{result.stdout}\n{result.stderr}"


def test_admin_pwd_flow_behaviour_assertions_pass():
    """用 Node 驱动后台的**真实脚本**，验证闸门与建号两件事的行为。

    后台是权限最高的页面，而它有两个"看起来对、实际会漏"的地方：被闸住的管理员
    如果照常去拉 /api/admin/*（只会拿到一串 403），界面就成了空壳；建号返回的一次性
    口令如果不落在页面上，它在库里就再也拿不回来。两条都只有真跑一遍才守得住。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过后台流程断言")

    script = Path(__file__).resolve().parent / "admin_pwd_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["admin"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"后台流程断言失败：\n{result.stdout}\n{result.stderr}"


# ---------------------------------------------------------------- 深色页面的令牌一致性

# 深色主题的两个页面（登录 / 后台）共用的令牌前缀
DARK_SHARED_PREFIXES = ("--accent", "--surface", "--text")

# 必须两边都有、且必须真的进入比对的令牌 —— 没有它这条守卫就可能空转（假绿）
DARK_REQUIRED_TOKENS = (
    "--accent",
    "--accent-hover",
    "--accent-soft",
    "--surface",
    "--surface-2",
    "--text",
    "--text-2",
)


def _root_tokens(page: str) -> dict:
    """取页面 :root 里的令牌（名 -> 归一化后的值）。"""
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", read_page(page), re.S))
    root = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert root, f"{page} 页面没有 :root 令牌块"
    tokens = {}
    for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root.group(1)):
        tokens[name] = " ".join(value.split()).lower()
    return tokens


def test_dark_pages_share_the_same_accent_surface_text_tokens():
    """login 与 admin 的强调色 / 底色 / 文字令牌必须同值。

    这两个页面是同一个产品的**深色一套**，本该"一处改、两页同变"。但 pass 1 只改了
    登录页的强调色，后台一直留着旧值 —— 直到目视评审才被发现，中间没有任何东西守着。
    这条守卫补上那个缺口：它比对的是**解析出来的实际值**，不是硬编码的期望色，
    所以以后调色只需改页面，不用改测试。
    """
    login = _root_tokens("login")
    admin = _root_tokens("admin")

    shared_names = {
        name
        for name in (set(login) & set(admin))
        if name.startswith(DARK_SHARED_PREFIXES)
    }

    # 先确认比对不是空转：核心令牌必须真的在集合里
    for name in DARK_REQUIRED_TOKENS:
        assert name in shared_names, (
            f"{name} 应当同时定义在 login 与 admin 的 :root 里 —— "
            "缺了它，这条守卫就少守一块（别让它悄悄空转）"
        )

    mismatched = {
        name: (login[name], admin[name])
        for name in sorted(shared_names)
        if login[name] != admin[name]
    }
    assert not mismatched, (
        "深色页面（login vs admin）的令牌不一致："
        + "；".join(f"{k}: {v[0]} != {v[1]}" for k, v in mismatched.items())
    )


# ---------------------------------------------------------------- 取值收敛（pass 2）

# 已收敛"取值"的页面：逐页加锁（pass 2 顺序 login -> index -> admin）。
# 空元组是**刻意的起点** —— 机制先落地，页面做完一页加一页。
LENGTH_TOKEN_PAGES = ("login",)

# 只看这几类属性：间距 / 字号 / 圆角。其余（宽度、定位、阴影、行高…）不在本轮范围。
LENGTH_PROPS = ("padding", "margin", "gap", "border-radius", "font-size")
_RAW_PX = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)px")
# 放行的裸值：0 = 本来就没有间距；1px = 发丝线（分隔线 / 内联 code 的竖向微调）
_ALLOWED_PX = {"0", "1"}


def _raw_lengths(css: str) -> list:
    """返回"没走令牌的 px 长度"（只看 LENGTH_PROPS 里的属性）。"""
    findings = []
    for prop, value in re.findall(r"([a-z-]+)\s*:\s*([^;{}]+);", css):
        if prop not in LENGTH_PROPS and not prop.startswith(("padding-", "margin-")):
            if not prop.endswith("-gap"):
                continue
        if "var(" in value:
            continue
        for number in _RAW_PX.findall(value):
            if number not in _ALLOWED_PX:
                findings.append(f"{prop}: {' '.join(value.split())}")
                break
    return findings


def test_length_guard_detects_raw_values():
    """守卫自身的自测：否则页面白名单空着时它会静默空转。"""
    sample = (
        ".x{padding:9px 11px;margin:0;gap:var(--space-2);"
        "border-radius:50%;font-size:13.5px;padding-top:var(--space-3)}"
    )
    found = _raw_lengths(sample)
    assert len(found) == 2, f"应当只命中 padding 与 font-size，实际：{found}"
    assert any(item.startswith("padding:") for item in found), found
    assert any(item.startswith("font-size:") for item in found), found


def test_converged_pages_use_only_token_lengths():
    """已收敛页面里，间距 / 字号 / 圆角不得再出现裸 px。

    与颜色那条棘轮同理：pass 2 把取值收敛成令牌之后，新写的样式一旦又手写
    `padding: 9px 11px`，就再也回不到"一处改、全站变"的状态。
    """
    for name in LENGTH_TOKEN_PAGES:
        css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", read_page(name), re.S))
        root = re.search(r":root\s*\{(.*?)\}", css, re.S)
        assert root, f"{name} 页面没有 :root 令牌块"

        findings = _raw_lengths(css.replace(root.group(0), ""))
        assert not findings, (
            f"{name} 页面还有 {len(findings)} 处没走令牌的长度：{findings[:6]}"
        )
