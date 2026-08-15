"""LLM 提示词。

设计原则：
1. 让模型只输出 JSON（结构化数据），思维导图的 mermaid 语法由本地代码生成 ——
   模型写 mermaid 极易踩括号/缩进的坑，交给代码更稳。
2. 时间戳必须来自文字稿里已有的 [hh:mm:ss] 标记，禁止自己编。
3. 「金句摘录」和「优秀表达提炼」要分工明确，避免两节内容重复。
"""

from __future__ import annotations

OUTPUT_SCHEMA = """{
  "cn_title": "中文核心主旨，用作文件名，15 字以内，概括这期讲了什么。不要用冒号、斜杠、引号、书名号",
  "category": "从这五个里选一个最贴切的：科技与 AI / 商业与职场 / 文化与思考 / 语言与学习 / 其他",
  "tags": ["3-5 个主题标签，不含 # 和空格"],
  "one_liner": "一句话说清这期最重要的东西，40 字以内",
  "core_conclusions": [
    {"point": "结论的标题式表述，15 字以内", "detail": "1-3 句展开，带上具体数据、案例或前提条件"}
  ],
  "mindmap": {
    "root": "中心主题，8 字以内",
    "children": [
      {"text": "一级分支，10 字以内", "children": [{"text": "二级分支，10 字以内"}]}
    ]
  },
  "thinking_frames": [
    {"name": "这套思考模式的名字，自己命名，8 字以内", "how": "他们具体是怎么用这套思路去想这个问题的，2-4 句，要带上本期里的实际例子", "transfer": "我在什么场合能复用它，1-2 句，落到具体动作", "timestamp": "hh:mm:ss"}
  ],
  "key_points": [
    {"heading": "话题小标题", "timestamp": "hh:mm:ss", "body": "这个话题的完整展开，2-5 句。保留嘉宾原话里的关键数据、人名、书名、案例。可以用 markdown 的加粗和列表"}
  ],
  "quotes": [
    {"text": "原话摘录，尽量逐字，不要改写", "timestamp": "hh:mm:ss", "note": "入选理由，一句话"}
  ],
  "expressions": [
    {"expr": "值得学习的表达本身", "lang": "中 或 英", "meaning": "中文节目写这句话在讨论中起什么作用；英文节目写中文释义", "usage": "什么场合用 + 一个自己造的例句（中文条目要造开会/讨论场景的例句）", "quote": "播客中的原句片段", "timestamp": "hh:mm:ss"}
  ]
}"""

_COMMON_RULES = """
硬性要求：
- 只输出一个 JSON 对象，不要任何解释文字，不要包在 ```json 代码块里。
- 所有时间戳必须是文字稿中真实出现过的 [hh:mm:ss] 标记（取该内容所在位置最近的那个）。如果某条实在定位不到，把 timestamp 设为空字符串 ""，绝对不要编造。
- 不要输出「本期播客讲了…」这类空话，直接给内容。
- 中文正文用中文标点。JSON 字符串内部不要出现真实换行，需要换行时用 \\n。
- 广告口播、开场寒暄、结尾致谢等与主题无关的内容直接忽略，不要写进笔记。

各字段的具体要求：
- core_conclusions：3-5 条。是「听完这期你该记住的判断」，不是章节目录。每条要能独立成立。
- mindmap：一级分支 3-5 个，每个下挂 2-4 个二级分支。节点文字要短、是名词性短语。禁止在节点文字里使用圆括号、方括号、花括号、引号、冒号、竖线、破折号。
- thinking_frames：2-4 条。记的是**他们怎么想**，不是他们想出了什么（结论归 core_conclusions，别重复）。
  可捕捉的思考动作：把问题重新问成了什么样子（换问法）／先分哪几层、按什么维度切／拿什么当好坏的尺子、什么条件下结论不成立／用哪个熟悉领域的模型来类比／怎么自我反驳、找反例、检查前提。
  name 要具体、能被复述，像「先问它何时失效」「把抽象的降到可观察」这种；禁止「深度思考」「系统思维」这类空壳名词。
  transfer 要落到我（互联网运营）的真实场合：写方案、评审、跟老板汇报、跟算法/产品对齐。凑不出 2 条就少给，绝不硬编。
- key_points：按内容话题分 3-6 段，按播客推进顺序排列。要有信息密度：具体数字、人名、书名、案例、方法步骤都要留住。
- quotes：4-6 条最有传播力的金句，逐字摘录。和 key_points 不要重复表述。
- expressions：5-8 条，是「语言层面」值得积累的东西，跟 quotes 的分工是：quotes 记观点，expressions 记说法。
  先判断节目主语言，按下面对应标准挑，lang 字段如实标「中」或「英」：

  【中文节目 · lang = 中】目标是提升中文口头表达能力，尤其是两人讨论、观点碰撞时能直接搬去用的说法。
  优先收：把对方观点先接住再反驳的说法／限定范围不把话说满的说法／追问逼近真问题的说法／让步但不丢立场的说法／把跑偏讨论拉回来的说法／给结论收口的说法；
  以及把模糊感觉说清楚的精准比喻、有画面感的动词搭配、换个话题也能套用的句式框架。
  硬性排除：① 条目里**不许夹带英文单词**（leverage、align、insight、AI 这类中英混说一律不收）；
  ② 不收新名词、概念术语的科普解释（如「隐性知识」「飞轮效应」这种，属于知识不属于表达）；
  ③ 不收日常用不到的书面语、生僻词、文言腔；④ 不收大白话和无特色的常用词。
  meaning 写这句话在讨论中起什么作用（是让步、是限定、是追问还是收口），usage 给一句自己造的开会/讨论场景例句。

  【英文节目 · lang = 英】只收日常高频、地道、职场能直接用的短语和句式；
  不收冷门俚语、过于书面的措辞、专业术语、只在特定语境成立的一次性说法。meaning 给中文释义，usage 给用法说明 + 一个自己造的例句。

  【中英混合节目】两类分别按上面标准收；即使节目里中英混说，lang 为「中」的条目本身也不许出现英文。

  【三条硬门槛，任一不满足就整条丢掉，宁可只给 3 条也不要凑数】
  ① 至少一半的中文条目必须是**讨论话术类**（接住对方再反驳／限定范围／追问／让步不失立场／把跑偏讨论拉回来／给结论收口），
     纯粹是"好词好句"的最多占一半。
  ② quote 必须是文字稿里**逐字存在、并且真的包含这条表达本身**的句子。找不到对得上的原句，这条就不要收。
  ③ 读起来不通顺、有重复字、明显是语音转文字出错的说法一律不收（比如「狗肉也也得卖」「这个是跑开的」这种残句）。
  另外：术语和概念名一律不收（如「定价锚」「涌现能力」「隐性知识」「AI 感」），哪怕它很精妙 —— 那属于知识，不属于表达；
  中文节目里冒出来的英文单词或口号（如 go do it jump）也不收。
"""

SINGLE_PASS_SYSTEM = f"""你是一位擅长做知识提炼的中文播客编辑，服务对象是一位互联网行业的运营。
你的任务：读完这期播客的完整文字稿，产出一份高信息密度的结构化笔记数据。

笔记会存进对方的 Obsidian 知识库长期复看，所以宁可具体、不要笼统：
能留住的数据、案例、人名、书名、方法论名称都要留住，不要压缩成抽象概括。

按下面的 JSON 结构输出：

{OUTPUT_SCHEMA}
{_COMMON_RULES}"""


MAP_SYSTEM = """你是一位中文播客编辑。下面是一期长播客文字稿的其中一段（不是全部）。
请只针对这一段做提炼，输出 JSON：

{
  "summary": "这一段讲了什么，3-6 句，保留具体数据、人名、案例",
  "points": [{"heading": "小标题", "timestamp": "hh:mm:ss", "body": "2-4 句展开"}],
  "quotes": [{"text": "原话逐字摘录", "timestamp": "hh:mm:ss", "note": "为什么值得记"}],
  "thinking_frames": [{"name": "他们的思考动作，自己命名，8 字以内", "how": "这段里他们怎么用它想问题的", "transfer": "能迁移到什么场合", "timestamp": "hh:mm:ss"}],
  "expressions": [{"expr": "值得学的表达", "lang": "中 或 英", "meaning": "中文条目写它在讨论里起什么作用；英文条目写中文释义", "usage": "什么场合用 + 例句", "quote": "原句片段", "timestamp": "hh:mm:ss"}]
}

要求：
- 只输出 JSON，不要解释，不要代码块包裹。
- 时间戳必须来自文字稿里真实出现的 [hh:mm:ss] 标记，定位不到就填 ""。
- quotes 这一段最多挑 4 条最有冲击力的，expressions 最多 4 条，thinking_frames 最多 2 条。
- thinking_frames 记「怎么想」（换问法、拆解维度、判断标准、类比、自我反驳），不记结论；这一段没有明显思考路径就给空数组。
- expressions：中文条目不许夹带英文单词，不收新名词/术语解释，重点收讨论、观点碰撞时能直接用的口语说法；英文条目只收日常高频、职场可用的表达。
  quote 必须是这一段里逐字存在、且真的包含这条表达的句子；读不通、有重复字、明显转写出错的说法直接不收。
- 如果这一段是开场寒暄、广告口播、结尾致谢等无实质内容，points / quotes / thinking_frames / expressions 可以给空数组，summary 里说明是什么内容即可。"""


REDUCE_SYSTEM = f"""你是一位擅长做知识提炼的中文播客编辑，服务对象是一位互联网行业的运营。
这期播客很长，已经被分段提炼过了。下面给你的是各段的提炼结果（按时间顺序）。
请把它们整合成一份完整、连贯、不重复的结构化笔记数据。

整合要求：
- 去重和合并：同一件事在多段里出现过，合并成一条，别重复。
- 提炼升维：core_conclusions 要跨段总结出真正的判断，不是把各段小标题抄一遍。
- key_points 控制在 4-8 条，把碎片合并成有主干的叙述，按播客推进顺序。
- quotes 从各段候选里挑最好的 5-10 条，保持原话不改写。
- thinking_frames 从各段候选里合并去重，留 2-4 条最有迁移价值的；同一种思考动作在多段出现就合成一条，用跨段的例子把它讲实。
- expressions 从各段候选里挑最好的 5-8 条，去掉平庸的、以及不符合下面中英文标准的。
- 时间戳沿用各段提炼里已有的，不要自己改动或编造。

按下面的 JSON 结构输出：

{OUTPUT_SCHEMA}
{_COMMON_RULES}"""


def build_single_pass_user(
    meta_block: str, transcript: str, known_expressions: list[str] | None = None
) -> str:
    return f"""{meta_block}
{build_dedup_block(known_expressions)}
以下是完整文字稿（含时间戳）：

<transcript>
{transcript}
</transcript>

请按系统指令输出 JSON。"""


def build_map_user(meta_block: str, chunk: str, idx: int, total: int) -> str:
    return f"""{meta_block}

以下是文字稿的第 {idx}/{total} 段：

<transcript_chunk>
{chunk}
</transcript_chunk>

请按系统指令输出这一段的提炼 JSON。"""


def build_reduce_user(
    meta_block: str, partials: str, known_expressions: list[str] | None = None
) -> str:
    return f"""{meta_block}
{build_dedup_block(known_expressions)}
以下是各段的提炼结果：

<partials>
{partials}
</partials>

请整合成最终的完整 JSON。"""


def build_dedup_block(known_expressions: list[str] | None) -> str:
    """把知识库里已积累过的表达喂给模型，避免重复收录（对应 _Rule.md 第 4 节查重要求）。"""
    if not known_expressions:
        return "\n"
    listed = "、".join(known_expressions[:150])
    return f"""
以下表达在知识库里**已经积累过了**，expressions 里不要再收录这些（同义的变体也算重复）：
{listed}
"""


def build_meta_block(*, title: str, podcast: str, duration: str, shownotes: str) -> str:
    block = f"""播客节目：{podcast}
单集标题：{title}
时长：{duration}"""
    if shownotes:
        trimmed = shownotes[:1500]
        block += f"\n\n单集简介（shownotes，可用于理解背景和确认人名书名的正确写法）：\n{trimmed}"
    return block
