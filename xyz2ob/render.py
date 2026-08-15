"""渲染 Obsidian Markdown 笔记。

结构对齐 vault 里已有的 YouTube 笔记规范：frontmatter 放元信息，正文分节。
"""

from __future__ import annotations

import re
from datetime import date

from .mermaid import build_mindmap
from .scrape import Episode

_UNSAFE_FILENAME = re.compile(r'[:\\/*?"<>|\n\r\t]')


def _yaml_str(value: str) -> str:
    """自由文本字段一律加双引号 —— 标题里出现冒号、引号、方括号是常态，
    不加引号迟早踩到 YAML 解析坑，宁可牺牲一点观感。"""
    s = (value or "").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return '""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_tag(value: str) -> str:
    """标签：安全时保持裸值，让 Obsidian 属性面板显示得干净。"""
    s = (value or "").replace("\r", " ").replace("\n", " ").strip()
    if not s:
        return '""'
    if re.search(r'[:\[\]{}#&*!|>\'"%@`,]', s) or s[0] == "-" or " " in s:
        return _yaml_str(s)
    return s


def safe_filename(text: str, max_len: int = 40) -> str:
    """清掉 macOS / iCloud 上会出问题的字符。"""
    s = _UNSAFE_FILENAME.sub(" ", text or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip("、，。 ")
    return s or "未命名单集"


def _is_ts(timestamp: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", (timestamp or "").strip()))


def _ts_prefix(timestamp: str) -> str:
    """行内时间戳标注（要点、表达用）。"""
    ts = (timestamp or "").strip()
    return f"`{ts}` " if _is_ts(ts) else ""


def _ts_link(timestamp: str, url: str) -> str:
    """金句用：时间点 + 回链单集页。

    小宇宙网页端不支持 ?t= 跳转（实测参数被忽略），所以链接指向单集页本身，
    时间点作为文字标注，方便在 App 里手动定位。
    """
    ts = (timestamp or "").strip()
    if not _is_ts(ts):
        return ""
    return f"[{ts}]({url})"


def render_note(
    *,
    episode: Episode,
    data: dict,
    transcript_source: str,
    seq: int,
    cn_title: str,
) -> str:
    """产出完整的 Markdown 文本。"""
    today = date.today().isoformat()
    tags = data.get("tags") or ["podcast"]
    if "podcast" not in tags:
        tags = ["podcast", *tags]

    lines: list[str] = ["---"]
    lines.append(f"title: {_yaml_str(cn_title)}")
    lines.append(f"source: {episode.url}")
    lines.append(f"podcast: {_yaml_str(episode.podcast)}")
    lines.append(f"episode_title: {_yaml_str(episode.title)}")
    if episode.author:
        lines.append(f"host: {_yaml_str(episode.author)}")
    lines.append(f"published: {episode.pub_date_local}")
    lines.append(f"duration: {episode.duration_hms}")
    lines.append(f"created: {today}")
    lines.append("tags:")
    for tag in tags:
        lines.append(f"  - {_yaml_tag(tag)}")
    lines.append("status: 待读")
    lines.append(f"transcript_source: {transcript_source}")
    lines.append(f"eid: {episode.eid}")
    lines.append("---")
    lines.append("")

    lines.append(f"# {cn_title}")
    lines.append("")
    if data.get("one_liner"):
        lines.append(f"> [!abstract] 一句话")
        lines.append(f"> {data['one_liner']}")
        lines.append("")

    # 核心结论
    lines.append("## 核心结论")
    lines.append("")
    conclusions = data.get("core_conclusions") or []
    if conclusions:
        for i, item in enumerate(conclusions, 1):
            lines.append(f"{i}. **{item['point']}**")
            if item.get("detail"):
                lines.append(f"   {item['detail']}")
        lines.append("")
    else:
        lines.append("_未提取到_")
        lines.append("")

    # 思维导图
    lines.append("## 思维导图")
    lines.append("")
    lines.append("```mermaid")
    lines.append(build_mindmap(data.get("mindmap") or {}, fallback_root=cn_title))
    lines.append("```")
    lines.append("")

    # 思维框架（他们是怎么想的）—— 承接思维导图，再进入要点细节
    frames = data.get("thinking_frames") or []
    if frames:
        lines.append("## 思维框架")
        lines.append("")
        for i, item in enumerate(frames, 1):
            name = item.get("name") or f"思考模式 {i}"
            lines.append(f"### {i}. {name}")
            lines.append("")
            how = item.get("how") or ""
            prefix = _ts_prefix(item.get("timestamp", ""))
            if how:
                lines.append(f"{prefix}{how}" if prefix else how)
                lines.append("")
            if item.get("transfer"):
                lines.append(f"**可迁移到**：{item['transfer']}")
                lines.append("")

    # 要点展开
    lines.append("## 要点展开")
    lines.append("")
    key_points = data.get("key_points") or []
    if key_points:
        for i, item in enumerate(key_points, 1):
            heading = item.get("heading") or f"要点 {i}"
            lines.append(f"### {i}. {heading}")
            lines.append("")
            body = item.get("body") or ""
            prefix = _ts_prefix(item.get("timestamp", ""))
            if body:
                lines.append(f"{prefix}{body}" if prefix else body)
            elif prefix:
                lines.append(prefix.strip())
            lines.append("")
    else:
        lines.append("_未提取到_")
        lines.append("")

    # 金句摘录
    lines.append("## 金句摘录")
    lines.append("")
    quotes = data.get("quotes") or []
    if quotes:
        for item in quotes:
            lines.append(f"> {item['text']}")
            tail = []
            link = _ts_link(item.get("timestamp", ""), episode.url)
            if link:
                tail.append(link)
            if item.get("note"):
                tail.append(item["note"])
            if tail:
                lines.append(">")
                lines.append(f"> — {' · '.join(tail)}")
            lines.append("")
    else:
        lines.append("_未提取到_")
        lines.append("")

    # 优秀表达沉淀
    lines.append("## 优秀表达沉淀")
    lines.append("")
    expressions = data.get("expressions") or []
    if expressions:
        for i, item in enumerate(expressions, 1):
            lang = (item.get("lang") or "").strip()
            is_cn = lang == "中"
            lines.append(f"### {i}. {item['expr']}")
            lines.append("")
            if item.get("meaning"):
                # 中文条目记的是「这句话在讨论里干什么」，英文条目记释义
                label = "作用" if is_cn else "含义"
                badge = f"（{lang}）" if lang in ("中", "英") else ""
                lines.append(f"**{label}**{badge}：{item['meaning']}")
                lines.append("")
            if item.get("usage"):
                label = "怎么用" if is_cn else "用法"
                lines.append(f"**{label}**：{item['usage']}")
                lines.append("")
            if item.get("quote"):
                prefix = _ts_prefix(item.get("timestamp", ""))
                lines.append(f"原句：{prefix}*「{item['quote']}」*")
                lines.append("")
    else:
        lines.append("_未提取到_")
        lines.append("")

    # 备注
    lines.append("---")
    lines.append("")
    source_label = {
        "official": "小宇宙官方文字稿",
        "asr": "本地 Whisper 转录",
        "manual": "手动提供的文字稿",
    }.get(transcript_source, transcript_source)
    lines.append(
        f"> 文字稿来源：{source_label}｜笔记由 xyz2ob 自动生成于 {today}，"
        f"序号 {seq:03d}｜[原单集]({episode.url})"
    )
    lines.append("")

    return "\n".join(lines)
