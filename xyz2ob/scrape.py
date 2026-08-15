"""免登录抓取单集元信息：解析页面里的 __NEXT_DATA__。

小宇宙单集页是 Next.js 服务端渲染，元信息（标题/播客名/时长/音频地址/
transcriptMediaId）都完整嵌在 <script id="__NEXT_DATA__"> 里，无需登录。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from .errors import PodError

WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_EID_RE = re.compile(r"/episode/([0-9a-fA-F]{24})")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)
_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff）)】\]，,。]+")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Episode:
    eid: str
    title: str
    podcast: str
    author: str = ""
    pub_date: str = ""          # ISO8601 UTC
    duration_seconds: int = 0
    audio_url: str = ""
    media_id: str = ""          # 取官方文字稿要用
    shownotes_text: str = ""
    url: str = ""
    pay_type: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def duration_hms(self) -> str:
        s = int(self.duration_seconds or 0)
        h, m, sec = s // 3600, (s % 3600) // 60, s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    @property
    def pub_date_local(self) -> str:
        """UTC pubDate -> 北京时间 YYYY-MM-DD（用户理解的「发布日期」）。"""
        from datetime import datetime, timedelta, timezone

        if not self.pub_date:
            return ""
        try:
            dt = datetime.fromisoformat(self.pub_date.replace("Z", "+00:00"))
        except ValueError:
            return self.pub_date[:10]
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def extract_eid(user_input: str) -> str:
    """从各种输入里挖出 24 位 eid。

    支持：
    - 完整单集链接（可带 ?s=xxx 等参数）
    - App 分享文案（含【】、说明文字、链接混在一起）
    - xyzfm.link 短链（自动跟随重定向）
    - 直接给 24 位 eid
    """
    text = (user_input or "").strip()
    if not text:
        raise PodError("没有输入链接。", "用法：pod '<小宇宙分享链接>'")

    # 已经是裸 eid
    if re.fullmatch(r"[0-9a-fA-F]{24}", text):
        return text.lower()

    m = _EID_RE.search(text)
    if m:
        return m.group(1).lower()

    # 分享文案里可能是短链，跟随一次重定向
    url_match = _URL_RE.search(text)
    if url_match:
        url = url_match.group(0).rstrip(".,;，。、")
        try:
            with httpx.Client(follow_redirects=True, timeout=20.0) as client:
                resp = client.get(url, headers={"User-Agent": WEB_UA})
            final = str(resp.url)
        except httpx.HTTPError as exc:
            raise PodError(f"短链解析失败：{exc}", "确认网络正常，或直接粘贴完整单集链接。") from exc
        m = _EID_RE.search(final)
        if m:
            return m.group(1).lower()

    raise PodError(
        "没能从输入里识别出小宇宙单集 ID。",
        "请粘贴形如 https://www.xiaoyuzhoufm.com/episode/xxxxxxxx 的链接。",
    )


def html_to_text(html: str) -> str:
    """shownotes HTML -> 纯文本（保留换行，去掉标签）。"""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch_episode(eid: str, timeout: float = 30.0) -> Episode:
    """免登录拉取单集元信息。"""
    url = f"https://www.xiaoyuzhoufm.com/episode/{eid}"
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url, headers={"User-Agent": WEB_UA})
    except httpx.HTTPError as exc:
        raise PodError(f"打不开单集页面：{exc}", "检查网络连接后重试。") from exc

    if resp.status_code == 404:
        raise PodError(f"单集不存在或已下架（eid={eid}）。", "确认链接是否正确。")
    if resp.status_code != 200:
        raise PodError(f"单集页面返回 HTTP {resp.status_code}。", "稍后重试。")

    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        raise PodError(
            "页面结构变了，没找到 __NEXT_DATA__。",
            "小宇宙前端可能改版了，需要更新 scrape.py 的解析逻辑。",
        )
    try:
        data = json.loads(m.group(1))
        ep = data["props"]["pageProps"]["episode"]
    except (ValueError, KeyError) as exc:
        raise PodError(f"解析单集数据失败：{exc}", "小宇宙页面结构可能已变化。") from exc

    podcast = ep.get("podcast") or {}
    media = ep.get("media") or {}
    enclosure = ep.get("enclosure") or {}

    return Episode(
        eid=ep.get("eid") or eid,
        title=(ep.get("title") or "").strip(),
        podcast=(podcast.get("title") or "").strip(),
        author=(podcast.get("author") or "").strip(),
        pub_date=ep.get("pubDate") or "",
        duration_seconds=int(ep.get("duration") or 0),
        audio_url=(media.get("source") or {}).get("url") or enclosure.get("url") or "",
        media_id=(
            ep.get("transcriptMediaId")
            or (ep.get("transcript") or {}).get("mediaId")
            or media.get("id")
            or ep.get("mediaKey")
            or ""
        ),
        shownotes_text=html_to_text(ep.get("shownotes") or ""),
        url=url,
        pay_type=ep.get("payType") or "",
        raw=ep,
    )
