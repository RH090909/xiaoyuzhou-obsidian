"""本地语音转录兜底：下载音频 -> mlx-whisper 转文字。

只在拿不到官方文字稿时才用（没登录，或该单集没生成文稿）。
mlx-whisper 是 Apple Silicon 原生实现，1 小时音频大约 2-5 分钟。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import httpx

from .errors import PodError

DOWNLOAD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def asr_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def download_audio(
    url: str,
    dest_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """流式下载音频到临时文件。小宇宙音频 CDN 无防盗链，可直连。"""
    if not url:
        raise PodError("这一集没有可下载的音频地址。", "可能是付费单集或外链托管，试试官方文字稿路径。")

    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="xyz2ob_"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(url.split("?")[0]).suffix or ".m4a"
    dest = dest_dir / f"audio{suffix}"

    try:
        with httpx.stream(
            "GET", url, headers={"User-Agent": DOWNLOAD_UA}, timeout=120.0, follow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                raise PodError(f"音频下载失败（HTTP {resp.status_code}）。")
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 18):
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except httpx.HTTPError as exc:
        raise PodError(f"音频下载出错：{exc}", "检查网络后重试。") from exc

    if dest.stat().st_size < 1024:
        raise PodError("下载到的音频文件异常小，可能是付费/加密内容。")
    return dest


def transcribe(
    audio_path: Path,
    model: str = "mlx-community/whisper-large-v3-turbo",
    language: str = "zh",
) -> list[dict[str, Any]]:
    """本地转录，返回和官方文字稿同构的 segments。"""
    try:
        import mlx_whisper
    except ImportError as exc:
        raise PodError(
            "本地转录需要 mlx-whisper，但没装。",
            "在项目目录执行：uv pip install mlx-whisper（仅支持 Apple Silicon）。",
        ) from exc

    if not shutil.which("ffmpeg"):
        raise PodError(
            "本地转录需要 ffmpeg，但没装。",
            "执行：brew install ffmpeg",
        )

    print(f"  正在本地转录（模型 {model}，首次运行会下载模型，请耐心等）…", file=sys.stderr)
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        language=language,
        verbose=False,
    )

    segments: list[dict[str, Any]] = []
    for seg in result.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append({"startMs": int(float(seg.get("start") or 0) * 1000), "text": text})

    if not segments:
        text = (result.get("text") or "").strip()
        if text:
            segments = [{"startMs": 0, "text": text}]
    if not segments:
        raise PodError("本地转录没有得到任何文字。", "确认音频可正常播放。")
    return segments
