"""小宇宙官方文字稿客户端。

小宇宙 App 内的「文稿」是官方 AI 转录的结果，质量和分句都远好于本地转录，
而且带毫秒级时间戳。走这条路需要一次性短信登录拿 token（存本地，不上传）。

协议要点（实测）：
- 登录：POST podcaster-api.xiaoyuzhoufm.com/v1/auth/send-code + /v1/auth/login-with-sms
  token 在响应头 x-jike-access-token / x-jike-refresh-token 里
- 文字稿地址：POST api.xiaoyuzhoufm.com/v1/episode-transcript/get {eid, mediaId}
- 文字稿 CDN 有 User-Agent 白名单，必须用官方安卓 UA
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .errors import PodError

API_BASE = "https://api.xiaoyuzhoufm.com"
PODCASTER_BASE = "https://podcaster-api.xiaoyuzhoufm.com"
APP_UA = "Xiaoyuzhou/2.99.1(android 28)"


def _app_headers(access_token: str | None = None, device_id: str | None = None) -> dict[str, str]:
    """伪装官方安卓客户端。文字稿 CDN 只认这套 UA。"""
    local_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0800"
    headers = {
        "Host": "api.xiaoyuzhoufm.com",
        "os": "android",
        "os-version": "28",
        "manufacturer": "Xiaomi",
        "model": "MI 6",
        "resolution": "1080x1920",
        "market": "xiaomi",
        "applicationid": "app.podcast.cosmos",
        "app-version": "2.99.1",
        "app-buildno": "1362",
        "webviewversion": "138.0.7204.179",
        "User-Agent": APP_UA,
        "app-permissions": "100100",
        "wificonnected": "false",
        "timezone": "Asia/Shanghai",
        "local-time": local_time,
        "content-type": "application/json;charset=utf-8",
    }
    if access_token:
        headers["x-jike-access-token"] = access_token
    if device_id:
        headers["x-jike-device-id"] = device_id
    return headers


def _web_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://podcaster.xiaoyuzhoufm.com",
        "referer": "https://podcaster.xiaoyuzhoufm.com/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    }


def format_ts(ms: int | float) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


@dataclass
class Credentials:
    access_token: str | None = None
    refresh_token: str | None = None
    device_id: str | None = None
    nickname: str | None = None
    saved_at: float = field(default_factory=time.time)

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.saved_at = time.time()
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    def clear(self, path: Path) -> None:
        self.access_token = None
        self.refresh_token = None
        if path.exists():
            path.unlink()

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)


class XiaoyuzhouClient:
    def __init__(self, token_path: Path, timeout: float = 30.0) -> None:
        self.token_path = token_path
        self.creds = Credentials.load(token_path)
        if not self.creds.device_id:
            self.creds.device_id = str(uuid.uuid4())
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "XiaoyuzhouClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 登录 ----------

    def send_sms_code(self, phone: str, area_code: str = "+86") -> None:
        resp = self._http.post(
            f"{PODCASTER_BASE}/v1/auth/send-code",
            headers=_web_headers(),
            json={"mobilePhoneNumber": phone, "areaCode": area_code},
        )
        if resp.status_code != 200:
            raise PodError(
                f"验证码发送失败（HTTP {resp.status_code}）：{resp.text[:150]}",
                "确认手机号格式，或稍等一会儿再试（有频率限制）。",
            )

    def login_with_sms(self, phone: str, code: str, area_code: str = "+86") -> str:
        resp = self._http.post(
            f"{PODCASTER_BASE}/v1/auth/login-with-sms",
            headers=_web_headers(),
            json={
                "areaCode": area_code,
                "verifyCode": code,
                "mobilePhoneNumber": phone,
            },
        )
        if resp.status_code != 200:
            raise PodError(
                f"登录失败（HTTP {resp.status_code}）：{resp.text[:150]}",
                "验证码可能输错或已过期，重新执行 pod login。",
            )
        access = resp.headers.get("x-jike-access-token")
        refresh = resp.headers.get("x-jike-refresh-token")
        if not access:
            raise PodError("登录成功但没拿到 token。", "小宇宙接口可能有变化。")

        user = {}
        try:
            user = resp.json().get("data", {}).get("user", {}) or {}
        except ValueError:
            pass

        self.creds.access_token = access
        self.creds.refresh_token = refresh
        self.creds.nickname = user.get("nickname")
        self.creds.save(self.token_path)
        return self.creds.nickname or phone

    def _refresh(self) -> bool:
        if not self.creds.refresh_token:
            return False
        headers = _app_headers(device_id=self.creds.device_id)
        headers["x-jike-refresh-token"] = self.creds.refresh_token
        try:
            resp = self._http.post(f"{API_BASE}/app_auth_tokens.refresh", headers=headers)
        except httpx.HTTPError:
            return False
        if 400 <= resp.status_code < 500:
            self.creds.clear(self.token_path)
            return False
        if resp.status_code != 200:
            return False
        body: dict = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        new_access = resp.headers.get("x-jike-access-token") or body.get("x-jike-access-token")
        if not new_access:
            return False
        self.creds.access_token = new_access
        new_refresh = resp.headers.get("x-jike-refresh-token") or body.get("x-jike-refresh-token")
        if new_refresh:
            self.creds.refresh_token = new_refresh
        self.creds.save(self.token_path)
        return True

    def _post(self, path: str, payload: dict) -> dict:
        if not self.creds.access_token:
            raise PodError("还没登录小宇宙。", "执行 pod login 完成一次短信登录。")

        def do() -> httpx.Response:
            return self._http.post(
                f"{API_BASE}{path}",
                headers=_app_headers(self.creds.access_token, self.creds.device_id),
                json=payload,
            )

        resp = do()
        if resp.status_code == 401:
            if not self._refresh():
                raise PodError("登录已过期。", "重新执行 pod login。")
            resp = do()
        if resp.status_code != 200:
            raise PodError(f"{path} 返回 HTTP {resp.status_code}：{resp.text[:150]}")
        return resp.json()

    # ---------- 文字稿 ----------

    def get_transcript_url(self, eid: str, media_id: str) -> str | None:
        data = self._post("/v1/episode-transcript/get", {"eid": eid, "mediaId": media_id})
        inner = data.get("data") or {}
        if isinstance(inner.get("data"), dict):
            inner = inner["data"]
        return inner.get("transcriptUrl")

    def fetch_segments(self, eid: str, media_id: str) -> list[dict[str, Any]]:
        """返回 [{'startMs': int, 'text': str}, ...]；没有官方文稿则返回 []。"""
        url = self.get_transcript_url(eid, media_id)
        if not url:
            return []
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": APP_UA},
                timeout=60.0,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise PodError(f"文字稿下载失败：{exc}", "稍后重试。") from exc
        if resp.status_code == 403:
            raise PodError(
                "文字稿 CDN 拒绝访问（403）。",
                "小宇宙的 UA 白名单可能更新了，需要调整 xyzclient.py 里的 APP_UA。",
            )
        if resp.status_code != 200:
            raise PodError(f"文字稿 CDN 返回 HTTP {resp.status_code}。")
        try:
            data = resp.json()
        except ValueError as exc:
            raise PodError(f"文字稿不是合法 JSON：{exc}") from exc
        if not isinstance(data, list):
            return []

        segments: list[dict[str, Any]] = []
        for seg in data:
            if not isinstance(seg, dict):
                continue
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            segments.append({"startMs": int(seg.get("startMs") or 0), "text": text})
        return segments
