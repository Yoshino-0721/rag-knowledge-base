"""文档资产完整性测试。

README 里的效果图是项目对外展示的门面，最容易在改名 / 替换时悄悄失效，
这里用测试守住：引用的图片必须真实存在，且必须是尺寸合理的有效 PNG。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

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
    # 截图展示的是同一份 PDF 上的连续追问，倒数第二问是带指代的追问，
    # 最后一问是「知识库 + 工具」的混合问题
    assert "集线器" in body and "交换机" in body, "效果演示文案未随截图更新"
    assert "改写" in body, "效果演示应说明追问改写（截图里有改写提示）"
    assert "RERANK" in body and "向量" in body, "效果演示应说明引用卡片上的两个分数"
    assert "页码" in body, "效果演示应说明引用可定位到页码"
    assert "工具" in body, "效果演示应说明截图里的工具调用卡片"


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
