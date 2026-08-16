"""流水线核心：链接 -> 文字稿 -> AI 提炼 -> 写入 Obsidian。

CLI 和网页界面都调这里，保证两边行为完全一致。
进度通过 on_log 回调往外抛，调用方决定是打到终端还是推给浏览器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import asr, llm, scrape, vault
from .config import Config
from .errors import PodError
from .prompts import build_meta_block
from .render import render_note, safe_filename
from .xyzclient import XiaoyuzhouClient, format_ts

LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def segments_to_text(segments: list[dict], with_ts: bool = True) -> str:
    if with_ts:
        return "\n".join(f"[{format_ts(s['startMs'])}] {s['text']}" for s in segments)
    return "\n".join(s["text"] for s in segments)


def get_transcript(
    cfg: Config,
    episode: scrape.Episode,
    *,
    prefer: str = "auto",
    transcript_file: str | None = None,
    on_log: LogFn = _noop,
) -> tuple[list[dict], str]:
    """返回 (segments, 来源标记)。prefer: auto | official | asr"""
    if transcript_file:
        path = Path(transcript_file).expanduser()
        if not path.exists():
            raise PodError(f"文字稿文件不存在：{path}")
        text = path.read_text(encoding="utf-8")
        segments = [
            {"startMs": 0, "text": ln.strip()} for ln in text.splitlines() if ln.strip()
        ]
        if not segments:
            raise PodError(f"文字稿文件是空的：{path}")
        on_log(f"使用本地文字稿文件：{path.name}")
        return segments, "manual"

    if prefer in ("auto", "official"):
        with XiaoyuzhouClient(cfg.token_path) as client:
            if client.creds.logged_in:
                if not episode.media_id:
                    on_log("这一集没有 mediaId，跳过官方文字稿。")
                else:
                    on_log("正在获取小宇宙官方文字稿…")
                    try:
                        segments = client.fetch_segments(episode.eid, episode.media_id)
                    except PodError as exc:
                        if prefer == "official":
                            raise
                        on_log(f"官方文字稿获取失败（{exc.message}），改用本地转录。")
                        segments = []
                    if segments:
                        on_log(f"拿到官方文字稿，共 {len(segments)} 段。")
                        return segments, "official"
                    on_log("这一集还没有官方文稿。")
            elif prefer == "official":
                raise PodError("指定了「只用官方文稿」但还没登录小宇宙。", "先完成小宇宙登录。")
            else:
                on_log("未登录小宇宙，跳过官方文字稿（建议登录，速度和质量都更好）。")

    if prefer == "official":
        raise PodError(
            "这一集拿不到官方文字稿。",
            "改用本地转录（需要 mlx-whisper + ffmpeg），或换一集试试。",
        )

    if not asr.asr_available():
        raise PodError(
            "拿不到官方文字稿，本地转录环境也没装好。",
            "二选一：① 完成小宇宙登录用官方文稿（推荐，秒出）"
            "② 安装本地转录：uv pip install mlx-whisper && brew install ffmpeg",
        )

    on_log("正在下载音频…")
    last = [-1]

    def progress(done: int, total: int) -> None:
        if not total:
            return
        pct = int(done * 100 / total)
        if pct >= last[0] + 10:
            last[0] = pct
            on_log(f"音频下载 {pct}%")

    audio = asr.download_audio(episode.audio_url, on_progress=progress)
    on_log("正在本地转录（首次运行会先下载模型，请耐心等）…")
    segments = asr.transcribe(audio, model=cfg.asr_model)
    on_log(f"本地转录完成，共 {len(segments)} 段。")
    return segments, "asr"


def run(
    cfg: Config,
    raw_input_str: str,
    *,
    prefer: str = "auto",
    transcript_file: str | None = None,
    force: bool = False,
    write: bool = True,
    on_log: LogFn = _noop,
) -> dict:
    """跑完整流程。返回结果字典，含 note_path / markdown / episode 信息。"""
    if write:
        cfg.require_vault()

    eid = scrape.extract_eid(raw_input_str)
    on_log(f"识别到单集 ID：{eid}")

    episode = scrape.fetch_episode(eid)
    on_log(f"《{episode.podcast}》{episode.title}（{episode.duration_hms}）")

    if episode.pay_type and episode.pay_type not in ("FREE", "", "PAY_TYPE_FREE"):
        on_log(f"注意：这是付费单集（payType={episode.pay_type}），可能拿不到内容。")

    existing = vault.find_existing(cfg.notes_dir, eid) if write else None
    if existing and not force:
        on_log(f"这一集已经记过了：{existing.name}")
        return {
            "status": "skipped",
            "note_path": str(existing),
            "note_name": existing.name,
            "episode": _ep_dict(episode),
            "markdown": existing.read_text(encoding="utf-8"),
        }

    segments, source = get_transcript(
        cfg, episode, prefer=prefer, transcript_file=transcript_file, on_log=on_log
    )
    transcript = segments_to_text(segments)

    on_log("AI 提炼中，长播客会分段处理，请稍等…")
    meta_block = build_meta_block(
        title=episode.title,
        podcast=episode.podcast,
        duration=episode.duration_hms,
        shownotes=episode.shownotes_text,
    )
    # --force 重跑同一集时，这一集自己的旧笔记不算「历史积累」——
    # 否则模型会被要求避开本集最好的那几条表达，越重跑越差。
    known = vault.collect_known_expressions(
        cfg.vault_path,
        cfg.notes_dir,
        exclude=existing if force else None,
        extra_dirs=cfg.extra_scan_dirs,
    )
    if known:
        on_log(f"已积累 {len(known)} 条表达，本次会避开重复。")

    data = llm.analyze(
        cfg,
        meta_block,
        transcript,
        known_expressions=known,
        verbose=False,
        on_log=on_log,
    )

    cn_title = safe_filename(data.get("cn_title") or episode.title)
    seq = (
        int(existing.name[:3])
        if (existing and existing.name[:3].isdigit())
        else vault.next_seq(cfg.notes_dir)
    )
    markdown = render_note(
        episode=episode,
        data=data,
        transcript_source=source,
        seq=seq,
        cn_title=cn_title,
    )

    result = {
        "status": "ok",
        "markdown": markdown,
        "episode": _ep_dict(episode),
        "transcript_source": source,
        "transcript": transcript,
        "category": data.get("category", "其他"),
        "one_liner": data.get("one_liner", ""),
        "cn_title": cn_title,
        "seq": seq,
    }

    if not write:
        result["note_path"] = ""
        result["note_name"] = f"{seq:03d} {cn_title}.md"
        return result

    note_path = vault.write_note(
        cfg.notes_dir,
        seq,
        cn_title,
        markdown,
        overwrite_path=existing if (existing and force) else None,
    )
    vault.update_index(cfg.notes_dir, note_path, data.get("one_liner", ""), result["category"])
    on_log(f"已写入：{note_path.name}")
    on_log(f"索引分类：{result['category']}")

    result["note_path"] = str(note_path)
    result["note_name"] = note_path.name
    return result


def _ep_dict(episode: scrape.Episode) -> dict:
    return {
        "eid": episode.eid,
        "title": episode.title,
        "podcast": episode.podcast,
        "duration": episode.duration_hms,
        "published": episode.pub_date_local,
        "url": episode.url,
    }
