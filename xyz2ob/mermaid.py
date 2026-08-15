"""Mermaid 思维导图生成 + 节点文字消毒。

Obsidian 内置 mermaid 支持 mindmap，但 mindmap 语法对括号极其敏感：
节点文字里出现 ()[]{}"" 会被当成形状语法，直接渲染失败。
所以节点文字一律先消毒，宁可损失标点也要保证能渲染出来。
"""

from __future__ import annotations

import re

# 会破坏 mermaid mindmap 语法的字符 -> 安全替代
_REPLACEMENTS = {
    "(": "（",
    ")": "）",
    "[": "【",
    "]": "】",
    "{": "「",
    "}": "」",
    '"': "",
    "'": "",
    "`": "",
    "|": "／",
    ";": "，",
    "#": "＃",
}
# 全角括号在 mermaid 里是安全的，但成对括号会让节点变长，统一去掉更清爽
_FULLWIDTH_PARENS = re.compile(r"[（(]([^）)]{0,20})[）)]")


def sanitize_node(text: str, max_len: int = 18) -> str:
    """把任意文字变成 mermaid mindmap 里安全的节点文字。"""
    s = (text or "").strip()
    if not s:
        return "未命名"
    # 括号内容改成顿号连接，避免嵌套括号
    s = _FULLWIDTH_PARENS.sub(r"、\1", s)
    for bad, good in _REPLACEMENTS.items():
        s = s.replace(bad, good)
    # 去掉换行和多余空白
    s = re.sub(r"\s+", " ", s)
    # mindmap 里冒号会被解析成 icon/class 修饰符
    s = s.replace(":", "：").replace("：", " ")
    s = re.sub(r"\s+", " ", s).strip("、 ")
    if len(s) > max_len:
        s = s[:max_len].rstrip("、，。 ") + "…"
    return s or "未命名"


def build_mindmap(mindmap: dict, fallback_root: str = "本期要点") -> str:
    """把 JSON 树渲染成 mermaid mindmap 代码块内容（不含 ``` 围栏）。

    mermaid mindmap 靠缩进表达层级，根节点用 root((文字)) 形式。
    """
    root_text = sanitize_node((mindmap or {}).get("root") or fallback_root, max_len=14)
    lines = ["mindmap", f"  root(({root_text}))"]

    def walk(nodes: list, depth: int) -> None:
        if not isinstance(nodes, list) or depth > 4:
            return
        for node in nodes:
            if isinstance(node, str):
                node = {"text": node}
            if not isinstance(node, dict):
                continue
            text = sanitize_node(node.get("text") or node.get("name") or "")
            if not text:
                continue
            lines.append("  " * (depth + 1) + text)
            walk(node.get("children") or [], depth + 1)

    walk((mindmap or {}).get("children") or [], 1)

    # 只有根节点时给个提示，避免出现空图
    if len(lines) == 2:
        lines.append("    内容较短未展开")
    return "\n".join(lines)
