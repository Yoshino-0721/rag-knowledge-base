"""回答组装测试：上下文编号、消息构造与引用格式（不调用真实 API）。"""

from src.generate import build_citations, build_context, build_messages, generate
from src.retrieve import Hit

HITS = [
    Hit(text="RAG 是检索增强生成。", source="notes/rag.md", location="", score=0.92),
    Hit(text="向量库用于相似度检索。", source="docs/intro.pdf", location="第 3 页", score=0.81),
]


def test_build_context_numbers_each_hit():
    context = build_context(HITS)

    assert "[1] 来源：notes/rag.md" in context
    assert "[2] 来源：docs/intro.pdf · 第 3 页" in context
    assert "RAG 是检索增强生成。" in context


def test_build_context_without_hits_uses_placeholder():
    assert "无参考资料" in build_context([])


def test_build_messages_contains_system_question_and_context():
    messages = build_messages("什么是 RAG？", HITS)

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "什么是 RAG？" in messages[1]["content"]
    assert "RAG 是检索增强生成。" in messages[1]["content"]


def test_generate_returns_reply_and_citations():
    captured = {}

    def fake_chat(messages):
        captured["messages"] = messages
        return "RAG 是检索增强生成 [1]。"

    answer = generate("什么是 RAG？", HITS, chat_fn=fake_chat)

    assert answer.text.startswith("RAG 是检索增强生成")
    assert len(answer.citations) == 2
    assert answer.citations[0]["index"] == 1
    assert answer.citations[0]["source"] == "notes/rag.md"
    assert answer.citations[1]["location"] == "第 3 页"
    assert "什么是 RAG？" in captured["messages"][1]["content"]


def test_build_citations_matches_input_order():
    citations = build_citations(HITS)

    assert [c["index"] for c in citations] == [1, 2]
    assert citations[0]["score"] == 0.92


def test_answer_as_dict_shape():
    payload = generate("问题", HITS, chat_fn=lambda _: "回答").as_dict()

    assert set(payload) == {"answer", "citations"}
    assert payload["answer"] == "回答"
