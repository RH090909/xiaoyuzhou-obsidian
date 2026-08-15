"""本地网页界面。

只监听 127.0.0.1，不对外暴露；因为要读写本机的 Obsidian vault 和登录 token，
所以必须跑在本机，不能部署到云端。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import asr, llm, pipeline, vault
from .config import PROJECT_ROOT, load_config
from .errors import PodError
from .xyzclient import XiaoyuzhouClient

STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_PATH = PROJECT_ROOT / ".env"

app = FastAPI(title="小宇宙 → Obsidian")

# job_id -> {"q": Queue, "result": dict|None, "error": str|None}
_JOBS: dict[str, dict[str, Any]] = {}
_OPTS: dict[str, Any] = {"vault": None, "model": None}


# ---------------- 配置读写 ----------------

def _write_env(updates: dict[str, str]) -> None:
    """就地更新 .env 里的键，保留注释和其他行。"""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(raw)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _cfg():
    return load_config(vault_path=_OPTS["vault"], model=_OPTS["model"])


# ---------------- 笔记读取（历史记录用） ----------------

def _parse_frontmatter(text: str) -> dict[str, Any]:
    """极简 YAML frontmatter 解析：只认顶层 `key: value` 和 `- item` 列表。

    笔记是本工具自己写的（见 render.py），格式可控，所以不引入 PyYAML。
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    data: dict[str, Any] = {}
    key_of_list: str | None = None
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        if raw.startswith(("  - ", "- ")) and key_of_list:
            data.setdefault(key_of_list, []).append(raw.split("-", 1)[1].strip().strip('"'))
            continue
        if ":" not in raw or raw.startswith(" "):
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:  # `tags:` 这种，后面跟列表
            key_of_list = key
            continue
        key_of_list = None
        data[key] = value.strip('"').replace('\\"', '"')
    return data


def _extract_one_liner(text: str) -> str:
    """取正文里 `> [!abstract] 一句话` 下面那一行。"""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "[!abstract]" in ln:
            for nxt in lines[i + 1 : i + 3]:
                body = nxt.lstrip(">").strip()
                if body:
                    return body
            break
    return ""


def _note_summary(path: Path) -> dict[str, Any]:
    head = path.read_text(encoding="utf-8")[:4000]
    fm = _parse_frontmatter(head)
    name = path.stem
    seq = name[:3] if name[:3].isdigit() else ""
    return {
        "path": str(path),
        "name": path.name,
        "seq": seq,
        "title": fm.get("title") or name,
        "podcast": fm.get("podcast", ""),
        "episode_title": fm.get("episode_title", ""),
        "published": fm.get("published", ""),
        "duration": fm.get("duration", ""),
        "created": fm.get("created", ""),
        "source": fm.get("source", ""),
        "eid": fm.get("eid", ""),
        "tags": [t for t in (fm.get("tags") or []) if t != "podcast"],
        "transcript_source": fm.get("transcript_source", ""),
        "one_liner": _extract_one_liner(head),
        "mtime": path.stat().st_mtime,
    }


@app.get("/api/history")
def history(limit: int = 8) -> JSONResponse:
    """最近生成的笔记，按序号倒序（序号就是生成顺序）。"""
    try:
        cfg = _cfg()
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message, "items": []})

    notes_dir = cfg.notes_dir
    if not notes_dir.exists():
        return JSONResponse({"ok": True, "items": [], "total": 0})

    files = [p for p in notes_dir.glob("*.md") if not p.name.startswith("_")]
    # 序号即生成顺序，比 mtime 稳（手动编辑笔记不会打乱历史）
    files.sort(key=lambda p: (p.name[:3].isdigit(), p.name[:3], p.stat().st_mtime), reverse=True)

    items: list[dict[str, Any]] = []
    for p in files[: max(1, min(limit, 50))]:
        try:
            items.append(_note_summary(p))
        except OSError:
            continue
    return JSONResponse({"ok": True, "items": items, "total": len(files)})


@app.get("/api/note")
def read_note(path: str) -> JSONResponse:
    """读一篇笔记的全文，给历史记录的预览用。"""
    try:
        cfg = _cfg()
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message})

    target = Path(path).expanduser().resolve()
    notes_dir = cfg.notes_dir.resolve()
    # 只允许读 notes_dir 里的 .md，避免这个本地接口变成任意文件读取
    if target.suffix != ".md" or notes_dir not in target.parents:
        return JSONResponse({"ok": False, "error": "只能读取笔记目录下的 Markdown 文件。"})
    if not target.exists():
        return JSONResponse({"ok": False, "error": "文件不存在，可能已被移动或删除。"})
    return JSONResponse({"ok": True, "markdown": target.read_text(encoding="utf-8")})


# ---------------- 接口 ----------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status() -> JSONResponse:
    import shutil

    try:
        cfg = _cfg()
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message, "hint": exc.hint})

    with XiaoyuzhouClient(cfg.token_path) as client:
        logged_in = client.creds.logged_in
        nickname = client.creds.nickname or ""

    notes_dir = cfg.notes_dir
    return JSONResponse(
        {
            "ok": True,
            "vault_path": str(cfg.vault_path),
            "vault_exists": cfg.vault_path.exists(),
            "notes_subdir": cfg.notes_subdir,
            "notes_dir": str(notes_dir),
            "note_count": len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0,
            "next_seq": vault.next_seq(notes_dir),
            "xyz_logged_in": logged_in,
            "xyz_nickname": nickname,
            "llm_configured": bool(cfg.llm_api_key),
            "llm_key_masked": (cfg.llm_api_key[:6] + "…") if cfg.llm_api_key else "",
            "llm_base_url": cfg.llm_base_url,
            "llm_model": cfg.llm_model,
            "asr_ready": asr.asr_available() and bool(shutil.which("ffmpeg")),
        }
    )


class Settings(BaseModel):
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    vault_path: str | None = None
    notes_subdir: str | None = None


@app.post("/api/settings")
def save_settings(body: Settings) -> JSONResponse:
    mapping = {
        "LLM_API_KEY": body.llm_api_key,
        "LLM_BASE_URL": body.llm_base_url,
        "LLM_MODEL": body.llm_model,
        "VAULT_PATH": body.vault_path,
        "NOTES_SUBDIR": body.notes_subdir,
    }
    updates = {k: v.strip() for k, v in mapping.items() if v is not None and v.strip()}
    if not updates:
        return JSONResponse({"ok": False, "error": "没有要保存的内容。"})
    _write_env(updates)
    return JSONResponse({"ok": True})


@app.post("/api/llm-test")
def llm_test() -> JSONResponse:
    try:
        cfg = _cfg()
        reply = llm.chat(cfg, "你是一个测试助手。", "只回复两个字：可用", max_tokens=20)
        return JSONResponse({"ok": True, "reply": reply.strip()[:40]})
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message, "hint": exc.hint})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)})


class Phone(BaseModel):
    phone: str


class Verify(BaseModel):
    phone: str
    code: str


@app.post("/api/login/code")
def send_code(body: Phone) -> JSONResponse:
    try:
        cfg = _cfg()
        with XiaoyuzhouClient(cfg.token_path) as client:
            client.send_sms_code(body.phone.strip())
        return JSONResponse({"ok": True})
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message, "hint": exc.hint})


@app.post("/api/login/verify")
def verify_code(body: Verify) -> JSONResponse:
    try:
        cfg = _cfg()
        with XiaoyuzhouClient(cfg.token_path) as client:
            who = client.login_with_sms(body.phone.strip(), body.code.strip())
        return JSONResponse({"ok": True, "who": who})
    except PodError as exc:
        return JSONResponse({"ok": False, "error": exc.message, "hint": exc.hint})


@app.post("/api/logout")
def logout() -> JSONResponse:
    cfg = _cfg()
    if cfg.token_path.exists():
        cfg.token_path.unlink()
    return JSONResponse({"ok": True})


@app.get("/api/clipboard")
def clipboard() -> JSONResponse:
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        return JSONResponse({"text": out.stdout.strip()})
    except Exception:  # noqa: BLE001
        return JSONResponse({"text": ""})


class OpenReq(BaseModel):
    path: str


@app.post("/api/open")
def open_in_obsidian(body: OpenReq) -> JSONResponse:
    p = Path(body.path)
    if not p.exists():
        return JSONResponse({"ok": False, "error": "文件不存在"})
    subprocess.run(["open", "-a", "Obsidian", str(p)], check=False)
    return JSONResponse({"ok": True})


class JobReq(BaseModel):
    url: str
    prefer: str = "auto"
    force: bool = False


@app.post("/api/jobs")
def create_job(body: JobReq) -> JSONResponse:
    job_id = uuid.uuid4().hex[:12]
    q: queue.Queue = queue.Queue()
    _JOBS[job_id] = {"q": q, "result": None, "error": None}

    def worker() -> None:
        try:
            cfg = _cfg()
            result = pipeline.run(
                cfg,
                body.url,
                prefer=body.prefer,
                force=body.force,
                write=True,
                on_log=lambda m: q.put({"type": "log", "message": m}),
            )
            _JOBS[job_id]["result"] = result
            q.put({"type": "done", "result": result})
        except PodError as exc:
            _JOBS[job_id]["error"] = exc.message
            q.put({"type": "error", "message": exc.message, "hint": exc.hint or ""})
        except Exception as exc:  # noqa: BLE001
            _JOBS[job_id]["error"] = str(exc)
            q.put({"type": "error", "message": f"意外错误：{exc}", "hint": ""})

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str) -> StreamingResponse:
    job = _JOBS.get(job_id)
    if not job:
        return StreamingResponse(iter([]), media_type="text/event-stream")

    def stream():
        q: queue.Queue = job["q"]
        while True:
            try:
                evt = q.get(timeout=300)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            if evt["type"] in ("done", "error"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    vault: str | None = None,
    model: str | None = None,
) -> int:
    import uvicorn

    _OPTS["vault"] = vault
    _OPTS["model"] = model
    url = f"http://{host}:{port}"
    print(f"\n  播客笔记界面已启动：{url}")
    print("  关掉这个窗口就会停止服务。\n")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
