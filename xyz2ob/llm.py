"""LLM 调用层：任何 OpenAI 兼容接口都能用（火山方舟 / DeepSeek / OpenAI / Ollama）。

长播客用 map-reduce：先分段提炼，再整合，避免超长上下文里细节被丢掉。
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import httpx

from . import prompts
from .config import Config
from .errors import PodError

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(raw: str) -> dict:
    """从模型输出里稳妥地挖出 JSON 对象。"""
    text = (raw or "").strip()
    if not text:
        raise PodError("模型返回了空内容。", "换个模型或重试一次。")

    # 1) 直接解析
    try:
        return json.loads(text)
    except ValueError:
        pass

    # 2) 剥掉 ``` 围栏
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            text = m.group(1).strip()

    # 3) 取第一个 { 到最后一个 } 之间的内容
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except ValueError:
            # 4) 容错：去掉行尾多余逗号
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except ValueError as exc:
                raise PodError(
                    f"模型输出不是合法 JSON：{exc}",
                    "重试一次；若反复失败，换一个指令跟随能力更强的模型。",
                ) from exc

    raise PodError(
        "模型输出里找不到 JSON。",
        f"输出开头是：{text[:200]}",
    )


def chat(cfg: Config, system: str, user: str, *, max_tokens: int = 8000) -> str:
    """调一次 chat completions，返回文本内容。"""
    cfg.require_llm()
    url = f"{cfg.llm_base_url}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg.llm_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=cfg.request_timeout,
        )
    except httpx.HTTPError as exc:
        raise PodError(f"调用大模型失败：{exc}", "检查 LLM_BASE_URL 和网络（是否需要代理）。") from exc

    if resp.status_code == 401:
        raise PodError("大模型接口鉴权失败（401）。", "检查 .env 里的 LLM_API_KEY。")
    if resp.status_code == 404:
        raise PodError(
            f"接口地址不对（404）：{url}",
            "LLM_BASE_URL 要填到 /v1 这一层，例如 https://ark.cn-beijing.volces.com/api/v3",
        )
    if resp.status_code != 200:
        raise PodError(
            f"大模型返回 HTTP {resp.status_code}：{resp.text[:300]}",
            "常见原因：模型名写错、账号没开通该模型、余额不足。",
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError) as exc:
        raise PodError(f"解析模型响应失败：{exc}", f"原始响应：{resp.text[:300]}") from exc


def _chunk_transcript(transcript: str, max_chars: int) -> list[str]:
    """按行切块，尽量在段落边界断开。"""
    lines = transcript.splitlines()
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def analyze(
    cfg: Config,
    meta_block: str,
    transcript: str,
    *,
    known_expressions: list[str] | None = None,
    verbose: bool = True,
    on_log=None,
) -> dict:
    """文字稿 -> 结构化笔记数据。短的单次搞定，长的走 map-reduce。"""

    def say(msg: str) -> None:
        if on_log is not None:
            on_log(msg)
        elif verbose:
            print(f"  {msg}", file=sys.stderr)

    if len(transcript) <= cfg.max_chars_single_pass:
        say(f"文字稿 {len(transcript)} 字，单次分析…")
        raw = chat(
            cfg,
            prompts.SINGLE_PASS_SYSTEM,
            prompts.build_single_pass_user(meta_block, transcript, known_expressions),
        )
        return _verify_expressions(_normalize(_extract_json(raw)), transcript, say)

    chunks = _chunk_transcript(transcript, cfg.max_chars_single_pass // 2)
    say(f"文字稿 {len(transcript)} 字，较长，分 {len(chunks)} 段提炼后整合…")

    partials: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        say(f"提炼第 {i}/{len(chunks)} 段…")
        raw = chat(
            cfg,
            prompts.MAP_SYSTEM,
            prompts.build_map_user(meta_block, chunk, i, len(chunks)),
            max_tokens=4000,
        )
        try:
            partials.append(_extract_json(raw))
        except PodError as exc:
            # 单段失败不该毁掉整次运行
            say(f"第 {i} 段提炼失败，已跳过：{exc.message}")

    if not partials:
        raise PodError("所有分段提炼都失败了。", "重试一次，或换一个更稳定的模型。")

    say("整合各段结果…")
    raw = chat(
        cfg,
        prompts.REDUCE_SYSTEM,
        prompts.build_reduce_user(
            meta_block,
            json.dumps(partials, ensure_ascii=False, indent=1),
            known_expressions,
        ),
        max_tokens=10000,
    )
    return _verify_expressions(_normalize(_extract_json(raw)), transcript, say)


def _plain(text: str) -> str:
    """抹掉时间戳、空白和标点，用来判断一句话是否真的出现在文字稿里。"""
    text = re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]", "", text or "")
    return re.sub(r"[\s，。、！？；：,.!?;:\"'“”‘’（）()\-—…]+", "", text).lower()


def _is_chinese_show(transcript: str) -> bool:
    """判断节目主语言是不是中文：中日韩字符占比过半就算。"""
    sample = transcript[:20000]
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in sample if ch.isalpha())
    return letters > 0 and cjk / letters > 0.5


def _verify_expressions(data: dict, transcript: str, say) -> dict:
    """兜底把不合规的表达剔掉。

    模型偶尔会把自己总结的词当成原话（原句和表达对不上）、收录语音转写出错的
    残句，或者在中文节目里塞英文口号。提示词已经写明规则，但模型不总听话，
    这里做最后一道校验：
    - 表达本身在文字稿里找不到 → 整条丢掉；只是原句对不上 → 清掉原句留表达
    - 中文节目里带拉丁字母的条目 → 丢掉（对应 _Rule.md「中文条目不许夹带英文」）
    """
    flat = _plain(transcript)
    cn_show = _is_chinese_show(transcript)
    kept: list[dict] = []
    dropped: list[str] = []
    for item in data.get("expressions") or []:
        raw_expr = item.get("expr", "")
        expr = _plain(raw_expr)
        if expr and expr not in flat:
            dropped.append(raw_expr)
            continue
        if cn_show and re.search(r"[A-Za-z]", raw_expr):
            dropped.append(raw_expr)
            continue
        if item.get("quote") and _plain(item["quote"]) not in flat:
            item["quote"] = ""
            item["timestamp"] = ""
        kept.append(item)
    if dropped:
        say(f"剔掉 {len(dropped)} 条不合规的表达：{'、'.join(dropped)}")
    data["expressions"] = kept
    return data


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if value in (None, "", {}):
        return []
    return [value]


CATEGORIES = ["科技与 AI", "商业与职场", "文化与思考", "语言与学习", "其他"]


def _pick_category(value: Any) -> str:
    """把模型给的分类归到 _Index.md 里已有的五个分类上。"""
    s = str(value or "").strip()
    if not s:
        return "其他"
    for cat in CATEGORIES:
        if cat == s:
            return cat
    # 容错：AI / ai / 科技 / 商业 / 职场 这类简写
    compact = s.replace(" ", "").lower()
    for cat in CATEGORIES:
        if cat.replace(" ", "").lower() in compact or compact in cat.replace(" ", "").lower():
            return cat
    if any(k in compact for k in ("ai", "科技", "技术", "互联网")):
        return "科技与 AI"
    if any(k in compact for k in ("商业", "职场", "创业", "管理", "增长")):
        return "商业与职场"
    if any(k in compact for k in ("文化", "思考", "哲学", "人文", "生活")):
        return "文化与思考"
    if any(k in compact for k in ("语言", "英语", "学习", "english")):
        return "语言与学习"
    return "其他"


def _normalize(data: dict) -> dict:
    """兜住模型的各种字段变体，保证下游渲染拿到统一结构。"""
    if not isinstance(data, dict):
        raise PodError("模型返回的不是 JSON 对象。")

    out: dict[str, Any] = {
        "cn_title": str(data.get("cn_title") or data.get("title") or "").strip(),
        "category": _pick_category(data.get("category")),
        "one_liner": str(data.get("one_liner") or data.get("summary") or "").strip(),
        "tags": [],
        "core_conclusions": [],
        "mindmap": data.get("mindmap") if isinstance(data.get("mindmap"), dict) else {},
        "thinking_frames": [],
        "key_points": [],
        "quotes": [],
        "expressions": [],
    }

    for tag in _as_list(data.get("tags")):
        t = str(tag).strip().lstrip("#").strip()
        if t and t not in out["tags"]:
            out["tags"].append(t)

    for item in _as_list(data.get("core_conclusions") or data.get("conclusions")):
        if isinstance(item, str):
            out["core_conclusions"].append({"point": item.strip(), "detail": ""})
        elif isinstance(item, dict):
            out["core_conclusions"].append(
                {
                    "point": str(item.get("point") or item.get("heading") or "").strip(),
                    "detail": str(item.get("detail") or item.get("body") or "").strip(),
                }
            )

    for item in _as_list(data.get("thinking_frames") or data.get("thinking_frame")):
        if isinstance(item, str):
            out["thinking_frames"].append(
                {"name": item.strip(), "how": "", "transfer": "", "timestamp": ""}
            )
        elif isinstance(item, dict):
            out["thinking_frames"].append(
                {
                    "name": str(item.get("name") or item.get("frame") or item.get("title") or "").strip(),
                    "how": str(item.get("how") or item.get("detail") or item.get("body") or "").strip(),
                    "transfer": str(
                        item.get("transfer") or item.get("apply") or item.get("usage") or ""
                    ).strip(),
                    "timestamp": str(item.get("timestamp") or "").strip(),
                }
            )

    for item in _as_list(data.get("key_points") or data.get("points")):
        if not isinstance(item, dict):
            continue
        out["key_points"].append(
            {
                "heading": str(item.get("heading") or item.get("title") or "").strip(),
                "timestamp": str(item.get("timestamp") or "").strip(),
                "body": str(item.get("body") or item.get("detail") or "").strip(),
            }
        )

    for item in _as_list(data.get("quotes")):
        if isinstance(item, str):
            out["quotes"].append({"text": item.strip(), "timestamp": "", "note": ""})
        elif isinstance(item, dict):
            out["quotes"].append(
                {
                    "text": str(item.get("text") or item.get("quote") or "").strip(),
                    "timestamp": str(item.get("timestamp") or "").strip(),
                    "note": str(item.get("note") or item.get("why") or "").strip(),
                }
            )

    for item in _as_list(data.get("expressions")):
        if not isinstance(item, dict):
            continue
        out["expressions"].append(
            {
                "expr": str(item.get("expr") or item.get("expression") or "").strip(),
                "lang": str(item.get("lang") or "").strip(),
                "meaning": str(item.get("meaning") or "").strip(),
                "usage": str(item.get("usage") or "").strip(),
                "quote": str(item.get("quote") or "").strip(),
                "timestamp": str(item.get("timestamp") or "").strip(),
            }
        )

    # 清掉空条目
    out["core_conclusions"] = [c for c in out["core_conclusions"] if c["point"]]
    out["thinking_frames"] = [f for f in out["thinking_frames"] if f["name"] or f["how"]]
    out["key_points"] = [k for k in out["key_points"] if k["heading"] or k["body"]]
    out["quotes"] = [q for q in out["quotes"] if q["text"]]
    out["expressions"] = [e for e in out["expressions"] if e["expr"]]

    # 00:00:00 基本都是模型定位不到时的敷衍值，宁可不给时间点也别给错的
    for group in ("thinking_frames", "key_points", "quotes", "expressions"):
        for item in out[group]:
            if item.get("timestamp") in ("00:00:00", "0:00:00", "00:00"):
                item["timestamp"] = ""
    return out
