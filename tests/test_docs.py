"""文档资产完整性测试。

README 里的效果图是项目对外展示的门面，最容易在改名 / 替换时悄悄失效，
这里用测试守住：引用的图片必须真实存在，且必须是尺寸合理的有效 PNG。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DEMO = ROOT / "docs" / "demo.png"


def _png_size(path: Path) -> tuple[int, int]:
    """直接读 PNG 的 IHDR 头取宽高，不依赖第三方库。"""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是合法的 PNG 文件"
    return struct.unpack(">II", header[16:24])


def test_readme_exists():
    assert README.is_file(), "缺少 README.md"
    assert len(README.read_text(encoding="utf-8")) > 500


def test_readme_image_refs_all_exist():
    """README 里的相对路径图片必须真实存在（防止图片丢失或改名后链接失效）。"""
    text = README.read_text(encoding="utf-8")
    refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert refs, "README 至少应包含一张效果图"

    for ref in refs:
        if ref.startswith(("http://", "https://")):
            continue  # 徽章等外链不检查
        assert (ROOT / ref).is_file(), f"README 引用了不存在的图片：{ref}"


def test_demo_screenshot_is_valid_png():
    assert DEMO.is_file(), "缺少效果演示截图 docs/demo.png"
    width, height = _png_size(DEMO)
    # 防止误提交缩略图或占位图：演示图要足够大，缩到 README 宽度后文字仍可辨认
    assert width >= 800 and height >= 600, f"截图尺寸过小：{width}x{height}"


def test_readme_demo_section_matches_screenshot():
    """效果演示的文案要与截图内容对应，避免换图后描述还停留在旧截图。"""
    text = README.read_text(encoding="utf-8")
    section = text.split("## 效果演示", 1)
    assert len(section) == 2, "README 缺少「效果演示」小节"

    body = section[1].split("\n## ", 1)[0]
    # 截图展示的是同一份 PDF 上的连续两问，第二问是带指代的追问
    assert "集线器" in body and "交换机" in body, "效果演示文案未随截图更新"
    assert "改写" in body, "效果演示应说明追问改写（截图里有改写提示）"
    assert "RERANK" in body and "向量" in body, "效果演示应说明引用卡片上的两个分数"
    assert "页码" in body, "效果演示应说明引用可定位到页码"


def test_deploy_doc_local_path_examples_follow_repo_location():
    """部署文档里的本机绝对路径示例，末级目录必须就是本仓库所在的目录。

    项目目录一旦被移动或改名（例如从容器根挪进子文件夹），文档里的示例路径
    很容易忘记同步，这条断言能在当场把它拦下来。
    """
    doc = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    refs = re.findall(r"D:/[^\s`*]+", doc)
    assert refs, "DEPLOY.md 应至少给出一个本机路径示例"

    for ref in refs:
        assert ref.rstrip("/").endswith(ROOT.name), f"路径示例与本仓库实际位置不符：{ref}"
BODY_COUNT_PATTERNS = (
    r"\| 测试 \| pytest（(\d+) 项） \|",
    r"(\d+) 项测试全部使用桩对象",
)

# §0 表格里那一行（两仓库的 status.md 是**同一份文件**，两个数字并排）：
#   | 测试 | **471 通过** | **690 通过**（17 deselected，Docker 集成默认跳过） |
# 本项目是**第一列**，第二列是项目二。取错列就等于拿对家的数字来断言，
# 会造出一个永远"看起来在守"的空转守卫 —— 所以下面有专门的自测钉住列选择。
STATUS = ROOT / "docs" / "status.md"
STATUS_COUNT_ROW = re.compile(r"^\| 测试 \| \*\*(\d+) 通过\*\* \| \*\*(\d+) 通过\*\*", re.M)
STATUS_COLUMN = 1


# ---------------------------------------------------------------- 测试数漂移守卫

_DOCKER_MARK = r"pytestmark\s*=\s*pytest\.mark\.docker"


def _docker_only_test_files() -> set[str]:
    """模块级 `pytestmark = pytest.mark.docker` 的文件默认整文件被 deselect。

    这类文件不会出现在 `session.items` 里，做"是否只跑了子集"的判断时必须排除，
    否则每次常规运行都会被误判成子集而跳过守卫 —— 那样守卫等于不存在。
    """
    found = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        if re.search(_DOCKER_MARK, path.read_text(encoding="utf-8")):
            found.add(path.name)
    return found


def _require_full_suite(request) -> None:
    """只跑了子集时不判定数字（否则 `-k` 之类的局部运行会被守卫误伤）。"""
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*.py")} - _docker_only_test_files()
    collected = {item.path.name for item in request.session.items}
    if on_disk - collected:
        pytest.skip(
            f"只跑了部分测试文件，跳过测试数守卫（未收集：{sorted(on_disk - collected)}）"
        )


def _documented_count_from_status(text: str, column: int) -> int | None:
    """从 §0 表格取指定列的测试数；行缺失或格式变了返回 None（**不静默跳过**）。"""
    match = STATUS_COUNT_ROW.search(text)
    if not match:
        return None
    return int(match.group(column))


def test_readme_test_count_matches_reality(request):
    """README 里写的测试数必须与真实执行的用例数一致。

    为什么需要这条：数字徽章是会**漂移**的硬编码 —— 每加一条用例它就过期一次，
    而过期不会让任何测试变红，只会让读到它的人被误导（这个项目一路改下来，
    徽章已经悄悄落后过两次）。
    """
    _require_full_suite(request)

    text = README.read_text(encoding="utf-8")
    badge = re.search(r"tests-(\d+)%20passed", text)
    assert badge, "README 徽章里找不到 tests-<N>%20passed"
    documented = int(badge.group(1))
    actual = len(request.session.items)

    assert actual == documented, (
        f"README 徽章写的是 {documented} 项，实际执行 {actual} 项 —— "
        "请把 README 里的测试数一并更新（徽章与正文两处都要改）。"
        "注意**别用裸数字替换**：README 里还有 HTTP 状态码这类同名数字"
        "（例如登录限流的 429、限流配置里的 300），按数字全局替换会把它一起改掉。"
        "用带上下文的片段替换（`tests-<N>%20passed`、`pytest（<N> 项）`）并断言恰好命中 1 处。"
    )

    for pattern in BODY_COUNT_PATTERNS:
        match = re.search(pattern, text)
        assert match, f"README 里找不到测试数文案：{pattern}"
        assert int(match.group(1)) == actual, (
            f"README 正文写的是 {match.group(1)} 项，实际执行 {actual} 项"
        )


def test_status_table_test_count_matches_reality(request):
    """`docs/status.md` §0 表格里的测试数也必须与真实用例数一致。

    为什么这条比 README 那条更要紧：§0 表格是**新会话自检第 2 步**的核心输入 ——
    它漂移了，下一轮会话会把「文档写的数」和「实际跑出来的数」对不上判成
    **环境漂移**，然后花时间去排查一个根本不存在的问题。
    """
    _require_full_suite(request)

    text = STATUS.read_text(encoding="utf-8")
    documented = _documented_count_from_status(text, STATUS_COLUMN)
    assert documented is not None, (
        "status.md §0 里找不到测试数那一行（格式可能被改过）—— "
        "守卫依赖这一行的形状：`| 测试 | **N 通过** | **N 通过**（…） |`"
    )
    actual = len(request.session.items)
    assert documented == actual, (
        f"status.md §0 表格写的是 {documented} 项，实际执行 {actual} 项 —— "
        "请同步这一行；注意两份 status.md 是**同一个 sha256 的同构文件**，"
        "改完要同步到另一个仓库，且**别用裸数字替换**（表里还有端口、deselected 等数字）。"
    )


def test_status_guard_reads_the_right_column():
    """守卫的自测（防「空转」）：格式变了要认出来，列取错了要当场暴露。

    最容易犯的错是**取错列** —— 读到对家的数字，守卫照样"绿"，但它守的是别人。
    """
    row = "| 测试 | **111 通过** | **222 通过**（17 deselected，Docker 集成默认跳过） |"
    assert _documented_count_from_status(row, 1) == 111, "本项目取第一列"
    assert _documented_count_from_status(row, 2) == 222
    assert _documented_count_from_status("这一行根本不存在", 1) is None
    assert _documented_count_from_status("| 测试 | 111 通过 | 222 通过 |", 1) is None, (
        "格式变了必须返回 None（宁可报错也不要静默认一个错数字）"
    )
