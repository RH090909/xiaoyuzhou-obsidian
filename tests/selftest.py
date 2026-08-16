"""自测：不依赖 LLM，验证解析、渲染、消毒、写入逻辑。

跑法：python3 tests/selftest.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xyz2ob import vault
from xyz2ob.errors import PodError
from xyz2ob.llm import _extract_json, _normalize, _pick_category, _verify_expressions
from xyz2ob.mermaid import build_mindmap, sanitize_node
from xyz2ob.render import render_note, safe_filename
from xyz2ob.scrape import extract_eid, html_to_text
from xyz2ob.xyzclient import format_ts

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [✓] {name}")
    else:
        FAIL += 1
        print(f"  [×] {name} {extra}")


# ---------- 1. eid 解析 ----------
print("\n1. 链接解析")
EID = "672cb0f082eb19451dedeedc"
check("完整链接", extract_eid(f"https://www.xiaoyuzhoufm.com/episode/{EID}") == EID)
check("带参数", extract_eid(f"https://www.xiaoyuzhoufm.com/episode/{EID}?s=eyJ1IjoiYWJjIn0%3D") == EID)
check("裸 eid", extract_eid(EID.upper()) == EID)
check(
    "App 分享文案",
    extract_eid(
        f"【EP.03 播客头像的背后故事】听起来不错\nhttps://www.xiaoyuzhoufm.com/episode/{EID}?s=xxx\n分享自小宇宙"
    )
    == EID,
)
try:
    extract_eid("这里没有链接")
    check("无链接应报错", False)
except PodError:
    check("无链接应报错", True)

# ---------- 2. mermaid 消毒 ----------
print("\n2. mermaid 节点消毒（关键：括号会让 mindmap 渲染失败）")
check("圆括号被处理", "(" not in sanitize_node("增长飞轮(核心)") and ")" not in sanitize_node("增长飞轮(核心)"))
check("方括号被处理", "[" not in sanitize_node("AI[大模型]应用"))
check("引号被去掉", '"' not in sanitize_node('所谓"真实感"'))
check("冒号被替换", ":" not in sanitize_node("结论:要做长期主义"))
check("竖线被替换", "|" not in sanitize_node("A|B"))
check("超长截断", len(sanitize_node("一二三四五六七八九十一二三四五六七八九十二十字以上")) <= 19)
check("空值兜底", sanitize_node("") == "未命名")

nasty = {
    "root": "AI时代的内容(策略)",
    "children": [
        {
            "text": "供给侧变化: 成本归零",
            "children": [{"text": "UGC[爆发]"}, {"text": '"人格化"溢价'}],
        },
        {"text": "需求侧", "children": [{"text": "注意力|稀缺"}]},
        "字符串节点也要能吃下",
    ],
}
mm = build_mindmap(nasty)
print("\n  生成的 mermaid：")
for ln in mm.splitlines():
    print("    " + ln)
check("以 mindmap 开头", mm.splitlines()[0] == "mindmap")
check("有 root(( )) 且内部无脏字符", "root((" in mm.splitlines()[1])
body = "\n".join(mm.splitlines()[2:])
check("正文无危险字符", not any(c in body for c in '[]{}"|'), body)
check("字符串节点被收录", "字符串节点也要能吃下" in mm)
check("一级分支缩进 4 空格", any(ln.startswith("    ") and not ln.startswith("      ") for ln in mm.splitlines()[2:]))
check("二级分支缩进更深", any(ln.startswith("      ") for ln in mm.splitlines()[2:]))

# ---------- 3. JSON 提取容错 ----------
print("\n3. 模型输出 JSON 提取容错")
check("裸 JSON", _extract_json('{"a":1}')["a"] == 1)
check("代码块包裹", _extract_json('```json\n{"a":2}\n```')["a"] == 2)
check("前后有废话", _extract_json('好的，结果如下：\n{"a":3}\n希望有帮助')["a"] == 3)
check("行尾多余逗号", _extract_json('{"a":4,}')["a"] == 4)
try:
    _extract_json("完全不是 JSON")
    check("垃圾输入应报错", False)
except PodError:
    check("垃圾输入应报错", True)

# ---------- 4. 字段归一化 ----------
print("\n4. 字段归一化（模型用了别名/字符串数组）")
norm = _normalize(
    {
        "title": "备用标题字段",
        "summary": "一句话",
        "tags": ["#AI", "商业", "商业"],
        "conclusions": ["纯字符串结论"],
        "points": [{"title": "小标题", "detail": "内容", "timestamp": "00:10:00"}],
        "quotes": ["纯字符串金句", {"quote": "另一种键名", "why": "理由"}],
        "thinking_frames": [{"frame": "先问它何时失效", "body": "怎么用的", "apply": "评审时用"}],
        "expressions": [{"expression": "别名字段", "meaning": "意思"}],
    }
)
check("title 兜到 cn_title", norm["cn_title"] == "备用标题字段")
check("summary 兜到 one_liner", norm["one_liner"] == "一句话")
check("tags 去重去井号", norm["tags"] == ["AI", "商业"], str(norm["tags"]))
check("字符串结论可用", norm["core_conclusions"][0]["point"] == "纯字符串结论")
check("points 别名", norm["key_points"][0]["heading"] == "小标题")
check("quotes 两种形态", len(norm["quotes"]) == 2 and norm["quotes"][1]["text"] == "另一种键名")
check("expressions 别名", norm["expressions"][0]["expr"] == "别名字段")
check(
    "thinking_frames 别名",
    norm["thinking_frames"][0]["name"] == "先问它何时失效"
    and norm["thinking_frames"][0]["how"] == "怎么用的"
    and norm["thinking_frames"][0]["transfer"] == "评审时用",
    str(norm["thinking_frames"]),
)
check("thinking_frames 字符串形态", _normalize({"thinking_frames": ["只有名字"]})["thinking_frames"][0]["name"] == "只有名字")
check("thinking_frames 缺字段时为空列表", _normalize({})["thinking_frames"] == [])

# ---------- 5. 文件名与 YAML 安全 ----------
print("\n5. 文件名 / YAML 安全")
check("冒号被清掉", ":" not in safe_filename("增长的本质：复利"))
check("斜杠被清掉", "/" not in safe_filename("A/B 测试怎么做"))
check("引号被清掉", '"' not in safe_filename('所谓"增长"'))
check("空值兜底", safe_filename("") == "未命名单集")

# ---------- 6. 端到端渲染 + 写入 ----------
print("\n6. 渲染并写入临时 vault")
from xyz2ob.scrape import Episode

ep = Episode(
    eid=EID,
    title='【EP.03】增长的本质：复利与"耐心"',  # 故意带冒号和引号
    podcast="阎尽其祥",
    author="主播 A & 主播 B",
    pub_date="2024-11-07T22:00:00.000Z",
    duration_seconds=2072,
    audio_url="https://media.xyzcdn.net/x.m4a",
    media_id="x.m4a",
    shownotes_text="本期聊了增长",
    url=f"https://www.xiaoyuzhoufm.com/episode/{EID}",
)
data = _normalize(
    {
        "cn_title": "增长的本质：复利与耐心",
        "category": "商业与职场",
        "tags": ["商业", "增长"],
        "one_liner": "增长不是找技巧，是把一件对的事重复足够久。",
        "core_conclusions": [
            {"point": "复利的前提是不中断", "detail": "中断一次，前面的积累几乎重置。"},
            {"point": "耐心是稀缺资源", "detail": "多数人输在第 3 个月。"},
        ],
        "mindmap": nasty,
        "thinking_frames": [
            {
                "name": "先问它何时失效",
                "how": "他们不先问复利好不好，而是先问什么情况下复利不成立，把中断当成主变量。",
                "transfer": "写方案时先写这套逻辑在什么条件下会崩，再写收益。",
                "timestamp": "00:08:40",
            },
            {"name": "把抽象降到可观察", "how": "耐心这种感觉被换算成第 3 个月的留存动作。", "transfer": ""},
        ],
        "key_points": [
            {"heading": "为什么大家都做不到", "timestamp": "00:05:12", "body": "因为**反馈太慢**。\n列表也要能渲染。"},
            {"heading": "怎么办", "timestamp": "", "body": "把周期缩短到周。"},
        ],
        "quotes": [
            {"text": "所有人都想要复利，但没人愿意等。", "timestamp": "00:12:30", "note": "戳人"},
            {"text": "没有时间戳的金句也要能渲染。", "timestamp": "", "note": ""},
        ],
        "expressions": [
            {
                "expr": "把周期缩短",
                "lang": "中",
                "meaning": "把讨论从空谈拉回可执行节奏，属于收口",
                "usage": "对方越说越大时用。例句：这个方向我认，但我们先把周期缩短到一周来验。",
                "quote": "你得先把周期缩短",
                "timestamp": "00:20:01",
            },
            {
                "expr": "circle back",
                "lang": "英",
                "meaning": "回头再聊、稍后再议",
                "usage": "会上暂时定不了时用。例句：Let's circle back on pricing after the data lands.",
                "quote": "we can circle back later",
                "timestamp": "00:22:10",
            },
        ],
    }
)

tmp = Path(tempfile.mkdtemp(prefix="vault_test_"))
notes = tmp / "03 INPUT" / "Podcasts"
notes.mkdir(parents=True)
(notes / "001 旧笔记.md").write_text("---\neid: aaaaaaaaaaaaaaaaaaaaaaaa\n---\n", encoding="utf-8")

seq = vault.next_seq(notes)
check("序号递增到 002", seq == 2, f"got {seq}")

cn_title = safe_filename(data["cn_title"])
md = render_note(episode=ep, data=data, transcript_source="official", seq=seq, cn_title=cn_title)
p = vault.write_note(notes, seq, cn_title, md)
idx = vault.update_index(notes, p, data["one_liner"], data["category"])

check("文件名带三位序号", p.name.startswith("002 "), p.name)
check("文件名无冒号", ":" not in p.name, p.name)
check("索引已创建", idx is not None and idx.exists())
check("索引文件名统一为 _Index.md", idx.name == "_Index.md", idx.name)

itext = idx.read_text(encoding="utf-8")
check("索引含双链", f"[[{p.stem}]]" in itext)
check("索引归入了主题分类", f"## {data['category']}" in itext)
# 双链必须同时出现在主题分类和「全部笔记」两处
check("索引里出现两次（分类 + 全部笔记）", itext.count(f"[[{p.stem}]]") == 2, str(itext.count(f"[[{p.stem}]]")))
cat_i = itext.index(f"## {data['category']}")
all_i = itext.index("## 全部笔记（按时间）")
check("分类小节里的占位已被替换", "_(暂无)_" not in itext[cat_i:itext.find("##", cat_i + 3)])
check("其他分类的占位仍保留", "_(暂无)_" in itext)
check("全部笔记那行带日期", re.search(r"- \[\[.+\]\] - \d{4}-\d{2}-\d{2}", itext[all_i:]) is not None)

# 再写一篇，验证不会破坏已有内容
data2 = dict(data)
data2["category"] = "科技与 AI"
p2 = vault.write_note(notes, 3, "第二篇测试", md.replace(EID, "aaaaaaaaaaaaaaaaaaaaaaab"))
vault.update_index(notes, p2, "第二篇的一句话", data2["category"])
itext2 = idx.read_text(encoding="utf-8")
check("第二篇也进了索引", f"[[{p2.stem}]]" in itext2)
check("第一篇的条目没被弄丢", f"[[{p.stem}]]" in itext2)
check("两个分类小节都在", "## 商业与职场" in itext2 and "## 科技与 AI" in itext2)
check("重复登记不会写成两条", (vault.update_index(notes, p2, "刷新后的一句话", "其他") is not None) and idx.read_text(encoding="utf-8").count(f"[[{p2.stem}]]") == 2)
itext3 = idx.read_text(encoding="utf-8")
check("重跑会刷新索引描述", "刷新后的一句话" in itext3)
check("重跑会按新分类归位", f"- [[{p2.stem}]] — 刷新后的一句话" in itext3.split("## 其他")[1].split("##")[0], itext3)
check("原来的分类里不再残留", f"[[{p2.stem}]]" not in itext3.split("## 科技与 AI")[1].split("##")[0])
check("刷新后分隔线没和标题黏在一起", "---##" not in itext3)
check("全部笔记小节仍是独立标题", "\n## 全部笔记（按时间）" in itext3)

# 旧索引文件迁移：历史上工具写过 _索引.md，现在统一到 _Index.md
mig = Path(tempfile.mkdtemp(prefix="vault_mig_")) / "Podcasts"
mig.mkdir(parents=True)
(mig / "_索引.md").write_text("# 播客笔记索引\n\n## 其他\n\n- [[旧条目]]\n", encoding="utf-8")
resolved = vault.resolve_index_path(mig)
check(
    "旧 _索引.md 自动迁移成 _Index.md",
    resolved.name == "_Index.md" and resolved.exists() and not (mig / "_索引.md").exists(),
)
check("迁移后旧内容没丢", "旧条目" in resolved.read_text(encoding="utf-8"))

mig2 = Path(tempfile.mkdtemp(prefix="vault_mig2_")) / "Podcasts"
mig2.mkdir(parents=True)
(mig2 / "_Index.md").write_text("# 短的\n", encoding="utf-8")
(mig2 / "_索引.md").write_text("# 长的\n\n## 其他\n\n- [[有内容的条目]]\n", encoding="utf-8")
resolved2 = vault.resolve_index_path(mig2)
check("两份并存时保留内容更全的那份", "有内容的条目" in resolved2.read_text(encoding="utf-8"))
check("被合并的旧文件留了 .bak 备份", (mig2 / "_索引.md.bak").exists())

# --force 重跑时，本集自己的旧笔记不该算进「历史积累」
uniq_note = notes / "004 唯一表达测试.md"
uniq_note.write_text(
    "---\neid: aaaaaaaaaaaaaaaaaaaaaaac\n---\n\n## 优秀表达沉淀\n\n### 1. 这条只在这一篇里\n\n",
    encoding="utf-8",
)
check("独有表达能被收进查重清单", "这条只在这一篇里" in vault.collect_known_expressions(tmp, notes))
check(
    "--force 时排除本集自己的表达",
    "这条只在这一篇里" not in vault.collect_known_expressions(tmp, notes, exclude=uniq_note),
)
uniq_note.unlink()

# EXTRA_SCAN_DIRS：额外目录也要参与查重，两种文件格式都要认
extra_notes = tmp / "04 English"
extra_notes.mkdir(parents=True, exist_ok=True)
(extra_notes / "长期积累.md").write_text(
    "# 表达清单\n\n## loop you in\n\n把你也拉进来同步。\n\n## 1. 这是场景小标题\n\n## 目录总结\n",
    encoding="utf-8",
)
extra_dir2 = tmp / "03 KNOWLEDGE" / "Videos"
extra_dir2.mkdir(parents=True, exist_ok=True)
(extra_dir2 / "001 视频笔记.md").write_text(
    "## 要点展开\n\n### 不该被收的小标题\n\n## 优秀表达沉淀\n\n### 1. 视频里的表达\n",
    encoding="utf-8",
)
known_extra = vault.collect_known_expressions(
    tmp, notes, extra_dirs=("04 English", "03 KNOWLEDGE/Videos")
)
check("额外目录里的词条型表达能收到", "loop you in" in known_extra, str(known_extra))
check("额外目录里的笔记型表达能收到", "视频里的表达" in known_extra, str(known_extra))
check("词条型文件里的场景小标题不会被当成表达", "这是场景小标题" not in known_extra)
check("额外目录里的要点小标题不会被当成表达", "不该被收的小标题" not in known_extra)
check(
    "不配置额外目录时不会扫到那些目录",
    "loop you in" not in vault.collect_known_expressions(tmp, notes),
)
check(
    "额外目录填空字符串不会报错",
    isinstance(vault.collect_known_expressions(tmp, notes, extra_dirs=("", "  ")), list),
)

# 表达兜底校验：文字稿里查不到的表达要被剔掉
fake_transcript = "[00:01:00] 你得先把周期缩短，别一上来就谈星辰大海，go do it jump。\n" * 3
verified = _verify_expressions(
    {
        "expressions": [
            {"expr": "把周期缩短", "quote": "你得先把周期缩短", "timestamp": "00:01:00"},
            {"expr": "定价锚", "quote": "就是有个定价锚了嘛", "timestamp": "00:40:45"},
            {"expr": "星辰大海", "quote": "这也太抽象了", "timestamp": "01:14:33"},
            {"expr": "go do it jump", "lang": "英", "quote": "go do it jump", "timestamp": "00:01:00"},
        ]
    },
    fake_transcript,
    lambda msg: None,
)
exprs = [e["expr"] for e in verified["expressions"]]
check("查无此表达的条目被剔掉", "定价锚" not in exprs, str(exprs))
check("表达存在的条目保留", "把周期缩短" in exprs and "星辰大海" in exprs, str(exprs))
check("中文节目里的英文条目被剔掉", "go do it jump" not in exprs, str(exprs))
check(
    "英文节目里的英文条目会保留",
    "circle back"
    in [
        e["expr"]
        for e in _verify_expressions(
            {"expressions": [{"expr": "circle back", "lang": "英", "quote": ""}]},
            "we can circle back on that later, let's park it for now. " * 20,
            lambda msg: None,
        )["expressions"]
    ],
)
check(
    "原句对不上时只清掉原句",
    verified["expressions"][-1]["expr"] == "星辰大海"
    and verified["expressions"][-1]["quote"] == ""
    and verified["expressions"][-1]["timestamp"] == "",
    str(verified["expressions"][-1]),
)

# 表达查重收集
known = vault.collect_known_expressions(tmp, notes)
check("能收集到已有表达", "把周期缩短" in known, str(known))
check("查重列表已去重", len(known) == len(set(known)))
check("要点小标题不会被当成表达", "为什么大家都做不到" not in known, str(known))
check("思维框架名不会被当成表达", "先问它何时失效" not in known, str(known))

text = p.read_text(encoding="utf-8")
for section in [
    "## 核心结论",
    "## 思维导图",
    "## 思维框架",
    "## 要点展开",
    "## 金句摘录",
    "## 优秀表达沉淀",
]:
    check(f"含 {section}", section in text)
check(
    "思维框架排在思维导图之后、要点展开之前",
    text.index("## 思维导图") < text.index("## 思维框架") < text.index("## 要点展开"),
)
check("思维框架含可迁移到", "**可迁移到**：写方案时先写这套逻辑" in text)
check("思维框架时间戳用行内代码", "`00:08:40`" in text)
check("没有 transfer 的框架也能渲染", "### 2. 把抽象降到可观察" in text)
check("中文表达标作用与怎么用", "**作用**（中）：" in text and "**怎么用**：" in text)
check("英文表达标含义与用法", "**含义**（英）：" in text and "**用法**：" in text)
check("含 mermaid 围栏", "```mermaid" in text and text.count("```") >= 2)
check("frontmatter 里 episode_title 被引号包住", 'episode_title: "' in text)
check("eid 写进 frontmatter", f"eid: {EID}" in text)
check("要点时间戳用行内代码", "`00:05:12`" in text)
check("金句时间戳回链单集页", f"[00:12:30]({ep.url})" in text, )
check("表达出处时间戳保留", "`00:20:01`" in text)
check("无时间戳的金句不会留下空标记", "— ·" not in text and "[]()" not in text and "``" not in text.replace("```mermaid", "").replace("```", ""))

# 模型没给思维框架时，这一节整体省略，不留空壳
data_no_frames = dict(data)
data_no_frames["thinking_frames"] = []
text_no_frames = render_note(
    episode=ep, data=data_no_frames, transcript_source="official", seq=9, cn_title="无框架版"
)
check("没有思维框架时不渲染该小节", "## 思维框架" not in text_no_frames)
check("其他小节照常渲染", "## 要点展开" in text_no_frames and "## 优秀表达沉淀" in text_no_frames)

# 分类归一化
check("分类原样匹配", _pick_category("科技与 AI") == "科技与 AI")
check("分类简写兜底", _pick_category("AI") == "科技与 AI", _pick_category("AI"))
check("分类空值兜底", _pick_category("") == "其他")
check("分类未知兜底", _pick_category("宇宙学") == "其他", _pick_category("宇宙学"))

# frontmatter 必须能被真正的 YAML 解析器吃下（Obsidian 就靠这个）
try:
    import yaml  # type: ignore

    fm_raw = text.split("---")[1]
    fm = yaml.safe_load(fm_raw)
    check("frontmatter 是合法 YAML", isinstance(fm, dict), str(fm)[:80])
    check("YAML 解析出的标题含冒号原样保留", "：" in str(fm.get("episode_title")), str(fm.get("episode_title")))
    check("YAML tags 解析成列表", isinstance(fm.get("tags"), list), str(fm.get("tags")))
except ImportError:
    print("  [-] 跳过 YAML 解析校验（未安装 pyyaml）")

# 去重逻辑
found = vault.find_existing(notes, EID)
check("能按 eid 找到已存在笔记", found == p, str(found))
check("不存在的 eid 返回 None", vault.find_existing(notes, "ffffffffffffffffffffffff") is None)

# ---------- 7. 其他小工具 ----------
print("\n7. 杂项")
check("时间戳格式化", format_ts(3_723_000) == "01:02:03", format_ts(3_723_000))
check("时长 hms", ep.duration_hms == "34:32", ep.duration_hms)
check("发布日期转北京时间", ep.pub_date_local == "2024-11-08", ep.pub_date_local)
check("shownotes 去标签", html_to_text("<p>第一段</p><br/><div>第二段</div>") == "第一段\n第二段")

print("\n" + "=" * 46)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 46)
if FAIL == 0:
    print("\n生成的笔记预览：\n")
    print(text)
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if FAIL else 0)
