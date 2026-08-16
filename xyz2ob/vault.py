"""写入 Obsidian vault：序号命名、判重、索引维护、表达查重。

命名与索引规范来自 vault 里的 `_Rule.md`：
- 文件名 `001 中文核心主旨.md`，三位序号递增，不含冒号
- `_Index.md` 里既要归入主题分类，也要在「全部笔记（按时间）」追加一行
  （早期版本写的是 `_索引.md`，现已合并统一到 `_Index.md`，遇到旧文件自动迁移）
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .errors import PodError

_SEQ_RE = re.compile(r"^(\d{3})\s")
_EID_IN_FM_RE = re.compile(r"^eid:\s*([0-9a-fA-F]{24})\s*$", re.M)
# 表达小节下的条目标题：### 1. xxx（编号可有可无）
_EXPR_HEADING_RE = re.compile(r"^###\s*(?:\d+[.、]\s*)?(.+?)\s*$", re.M)
# 「优秀表达沉淀 / 优秀表达提炼 / 值得学习的英文表达」这类小节标题
_EXPR_SECTION_RE = re.compile(r"^##\s+.*表达.*$", re.M)
# 词条式积累文件（整篇就是一个表达清单）里，每条表达自己就是一个 ## 标题
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_ALL_NOTES_HEADING = "## 全部笔记（按时间）"
# 小节里的占位行，如 _(暂无)_ / _(新增笔记时在此追加：…)_，有真实条目后就该消失
_PLACEHOLDER_RE = re.compile(r"^_\(.*\)_$")

INDEX_NAME = "_Index.md"
LEGACY_INDEX_NAMES = ("_索引.md",)

INDEX_TEMPLATE = """---
title: 播客笔记索引
type: MOC
created: {today}
---

# 播客笔记索引

> 按主题分类的播客笔记导航页。每新增一篇笔记，在对应主题下追加双链，并在「全部笔记（按时间）」补一行。
> 生成规则见 [[_Rule]]。

## 科技与 AI

_(暂无)_

## 商业与职场

_(暂无)_

## 文化与思考

_(暂无)_

## 语言与学习

_(暂无)_

## 其他

_(暂无)_

---

## 全部笔记（按时间）

"""


def next_seq(notes_dir: Path) -> int:
    """扫描目录里已有的三位序号，返回下一个。"""
    max_seq = 0
    if notes_dir.exists():
        for p in notes_dir.glob("*.md"):
            m = _SEQ_RE.match(p.name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def find_existing(notes_dir: Path, eid: str) -> Path | None:
    """按 frontmatter 里的 eid 查是否已经记过这一集。"""
    if not notes_dir.exists():
        return None
    for p in notes_dir.glob("*.md"):
        try:
            head = p.read_text(encoding="utf-8")[:1200]
        except OSError:
            continue
        m = _EID_IN_FM_RE.search(head)
        if m and m.group(1).lower() == eid.lower():
            return p
    return None


def collect_known_expressions(
    vault_path: Path,
    notes_dir: Path,
    exclude: Path | None = None,
    extra_dirs: Sequence[str] = (),
) -> list[str]:
    """收集知识库里已积累过的表达，喂给模型做查重。

    覆盖范围：笔记目录本身，加上 `EXTRA_SCAN_DIRS` 里配置的其他目录
    （相对 vault 根目录，比如另一个笔记目录，或一份长期维护的表达清单）。

    两种文件格式都能认：
    - 笔记型：只在「…表达…」小节内部取 `###` 条目，避免把「要点展开」的小标题误当成表达；
    - 词条型：整篇没有「表达」小节时，退化为把每个 `##` 标题当作一条表达。

    exclude：--force 重跑某一集时传入这一集自己的旧笔记，把它排除在「历史积累」之外。
    """
    scan_dirs: list[Path] = [notes_dir]
    for raw in extra_dirs:
        rel = raw.strip().strip("/")
        if rel:
            scan_dirs.append(vault_path / rel)

    sources: list[Path] = []
    seen_dirs: set[Path] = set()
    for d in scan_dirs:
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        if d.exists() and d.is_dir():
            sources.extend(sorted(d.glob("*.md")))

    known: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        expr = raw.strip().strip("`*").strip()
        # 条目标题常写成「表达 — 释义」，只留表达本身
        expr = re.split(r"\s+[—–-]{1,2}\s+", expr, maxsplit=1)[0].strip()
        if not expr or len(expr) > 60:
            return
        key = expr.lower()
        if key not in seen:
            seen.add(key)
            known.append(expr)

    for p in sources:
        if p.name.startswith("_"):  # 跳过 _Rule / _Index
            continue
        if exclude is not None and p.name == exclude.name:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        sections = _expr_sections(text)
        if sections:
            for section in sections:
                for m in _EXPR_HEADING_RE.finditer(section):
                    add(m.group(1))
            continue
        # 词条型清单：整篇就是表达，每条一个 ## 标题
        for m in _H2_RE.finditer(text):
            heading = m.group(1).strip()
            # 跳过「1. 开场闲聊」这类场景小标题和结构性标题
            if re.match(r"^\d", heading) or any(
                k in heading for k in ("总结", "规则", "索引", "目录")
            ):
                continue
            add(heading)
    return known


def _expr_sections(text: str) -> list[str]:
    """切出笔记里所有「…表达…」二级小节的正文。"""
    sections: list[str] = []
    for m in _EXPR_SECTION_RE.finditer(text):
        start = m.end()
        nxt = re.search(r"^##\s+", text[start:], re.M)
        end = start + nxt.start() if nxt else len(text)
        sections.append(text[start:end])
    return sections


def write_note(
    notes_dir: Path,
    seq: int,
    cn_title: str,
    content: str,
    *,
    overwrite_path: Path | None = None,
) -> Path:
    notes_dir.mkdir(parents=True, exist_ok=True)
    if overwrite_path is not None:
        target = overwrite_path
    else:
        target = notes_dir / f"{seq:03d} {cn_title}.md"
        # 极小概率同名，加后缀避免覆盖
        n = 2
        while target.exists():
            target = notes_dir / f"{seq:03d} {cn_title} ({n}).md"
            n += 1
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise PodError(f"写入笔记失败：{exc}", f"确认目录可写：{notes_dir}") from exc
    return target


def _insert_under_heading(text: str, heading: str, entry: str) -> str:
    """在指定 ## 小节末尾追加一行；小节里的 _(暂无)_ 占位会被替换掉。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == heading.strip():
            start = i
            break
    if start is None:
        # 没有这个小节就补一个
        return text.rstrip("\n") + f"\n\n{heading}\n\n{entry}\n"

    # 本小节结束位置：下一个 ## 标题、分隔线，或文件尾
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("## ") or s == "---":
            end = j
            break

    section = [ln for ln in lines[start + 1 : end] if not _PLACEHOLDER_RE.match(ln.strip())]
    # 掐掉首尾空行，统一由外面补一个，避免反复追加后空行越积越多
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    section.append(entry)
    section.append("")

    new_lines = lines[: start + 1] + [""] + section + lines[end:]
    return "\n".join(new_lines).rstrip("\n") + "\n"


def resolve_index_path(notes_dir: Path) -> Path:
    """返回索引文件路径，顺手把早期的 `_索引.md` 合并/迁移到 `_Index.md`。

    历史上工具写 `_索引.md`、手工建过 `_Index.md`，两个文件并存过。
    现在统一到 `_Index.md`：
    - 只有旧文件 → 直接改名
    - 两个都在 → 保留内容更完整（更长）的那份，另一份改名为 .bak 留底
    """
    target = notes_dir / INDEX_NAME
    for legacy_name in LEGACY_INDEX_NAMES:
        legacy = notes_dir / legacy_name
        if not legacy.exists():
            continue
        if legacy.name == target.name:  # 同一个文件（大小写不敏感的盘上会撞上）
            continue
        try:
            if not target.exists():
                legacy.rename(target)
            else:
                legacy_text = legacy.read_text(encoding="utf-8")
                target_text = target.read_text(encoding="utf-8")
                if len(legacy_text) > len(target_text):
                    target.write_text(legacy_text, encoding="utf-8")
                legacy.rename(notes_dir / f"{legacy_name}.bak")
        except OSError:
            pass
    return target


def update_index(
    notes_dir: Path,
    note_path: Path,
    one_liner: str,
    category: str = "其他",
) -> Path | None:
    """把新笔记登记到 _Index.md：主题分类 + 全部笔记（按时间）。

    已登记过的笔记（--force 重跑）会刷新描述并按新分类归位，
    「全部笔记」里的原始日期保持不动。
    """
    index_path = resolve_index_path(notes_dir)
    note_name = note_path.stem
    today = date.today().isoformat()

    link = f"[[{note_name}]]"
    cat_entry = f"- {link}" + (f" — {one_liner}" if one_liner else "")
    all_entry = f"- {link} - {today}"

    try:
        if index_path.exists():
            text = index_path.read_text(encoding="utf-8")
        else:
            text = INDEX_TEMPLATE.format(today=today)

        already_listed = link in text
        if already_listed:
            # 删掉分类小节里的旧条目（可能分类和一句话都变了），
            # 「全部笔记」那行保留，日期以第一次生成为准。
            all_i = text.find(_ALL_NOTES_HEADING)
            head, tail = (text[:all_i], text[all_i:]) if all_i >= 0 else (text, "")
            head = "\n".join(
                ln for ln in head.splitlines() if not ln.strip().startswith(f"- {link}")
            )
            # splitlines 会吃掉结尾换行，不补回来会让 --- 和下一个标题黏在一起
            text = (head.rstrip("\n") + "\n\n" + tail) if tail else head

        text = _insert_under_heading(text, f"## {category}", cat_entry)
        if not already_listed:
            text = _insert_under_heading(text, _ALL_NOTES_HEADING, all_entry)
        index_path.write_text(text, encoding="utf-8")
        return index_path
    except OSError:
        # 索引更新失败不该让整次运行算失败
        return None
