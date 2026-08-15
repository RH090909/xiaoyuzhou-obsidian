"""命令行入口。

    pod <小宇宙链接>        抓取 -> 转文字 -> AI 提炼 -> 写进 Obsidian
    pod login              一次性短信登录（拿官方文字稿，强烈推荐）
    pod doctor             体检：配置、登录态、vault、转录环境
    pod transcript <链接>   只导出文字稿，不调 AI
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import asr, llm, pipeline, scrape, vault
from .config import STATE_DIR, load_config
from .errors import PodError
from .xyzclient import XiaoyuzhouClient


def _info(msg: str) -> None:
    print(msg, file=sys.stderr)


def _read_clipboard() -> str:
    try:
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cmd_run(args: argparse.Namespace) -> int:
    raw_input_str = args.url or _read_clipboard()
    if not raw_input_str:
        raise PodError("没有输入链接，剪贴板也是空的。", "用法：pod '<小宇宙分享链接>'")

    cfg = load_config(vault_path=args.vault, model=args.model)

    result = pipeline.run(
        cfg,
        raw_input_str,
        prefer=args.prefer,
        transcript_file=args.transcript_file,
        force=args.force,
        write=not args.no_write,
        on_log=lambda m: _info(f"  {m}"),
    )

    if args.save_transcript and result.get("transcript"):
        tpath = Path(args.save_transcript).expanduser()
        tpath.write_text(result["transcript"], encoding="utf-8")
        _info(f"  文字稿已存到 {tpath}")

    if result["status"] == "skipped":
        _info("  想重新生成加 --force。")
        print(result["note_path"])
        return 0

    if args.no_write:
        print(result["markdown"])
        return 0

    _info(f"✓ 已写入：{result['note_path']}")
    if args.open:
        subprocess.run(["open", "-a", "Obsidian", result["note_path"]], check=False)
    print(result["note_path"])
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    phone = args.phone or input("小宇宙账号手机号（不含 +86）：").strip()
    if not phone:
        raise PodError("手机号不能为空。")
    cfg = load_config(vault_path=args.vault)
    with XiaoyuzhouClient(cfg.token_path) as client:
        client.send_sms_code(phone)
        _info(f"验证码已发送到 {phone}")
        code = (args.code or input("收到的验证码：")).strip()
        if not code:
            raise PodError("验证码不能为空。")
        who = client.login_with_sms(phone, code)
    _info(f"✓ 登录成功：{who}")
    _info(f"  token 存在本地 {cfg.token_path}（权限 600，不会上传）")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = load_config(vault_path=args.vault)
    if cfg.token_path.exists():
        cfg.token_path.unlink()
        _info("✓ 已删除本地登录 token。")
    else:
        _info("本来就没有登录 token。")
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    raw_input_str = args.url or _read_clipboard()
    cfg = load_config(vault_path=args.vault)
    eid = scrape.extract_eid(raw_input_str)
    episode = scrape.fetch_episode(eid)
    _info(f"→ 《{episode.podcast}》{episode.title}")
    segments, source = pipeline.get_transcript(
        cfg, episode, prefer=args.prefer, on_log=lambda m: _info(f"  {m}")
    )
    _info(f"→ 来源：{source}")
    print(pipeline.segments_to_text(segments, with_ts=not args.plain))
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """启动本地网页界面。"""
    from .web import serve

    return serve(host=args.host, port=args.port, open_browser=not args.no_browser,
                 vault=args.vault, model=args.model)


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== xyz2ob 体检 ===\n")
    ok = True

    try:
        cfg = load_config(vault_path=args.vault, model=args.model)
    except PodError as exc:
        print(f"[×] 配置：{exc.message}\n    → {exc.hint}")
        return 1

    print(f"[✓] 配置文件已加载")
    print(f"    vault：{cfg.vault_path}")
    if cfg.vault_path.exists():
        print(f"[✓] vault 存在")
    else:
        print(f"[×] vault 不存在，检查 .env 里的 VAULT_PATH")
        ok = False
    print(f"    笔记目录：{cfg.notes_dir}")
    if cfg.notes_dir.exists():
        n = len(list(cfg.notes_dir.glob("*.md")))
        print(f"[✓] 笔记目录存在，已有 {n} 个 md，下一个序号 {vault.next_seq(cfg.notes_dir):03d}")
    else:
        print(f"[!] 笔记目录还不存在，首次运行会自动创建")

    # 小宇宙登录态
    with XiaoyuzhouClient(cfg.token_path) as client:
        if client.creds.logged_in:
            print(f"[✓] 小宇宙已登录（{client.creds.nickname or '账号'}）→ 可用官方文字稿")
        else:
            print("[!] 小宇宙未登录 → 只能本地转录。建议执行：pod login")

    # LLM
    if cfg.llm_api_key:
        print(f"[✓] LLM Key 已配置（{cfg.llm_api_key[:6]}…）")
        print(f"    接口：{cfg.llm_base_url}")
        print(f"    模型：{cfg.llm_model}")
        if not args.skip_llm:
            print("    正在试调一次…")
            try:
                reply = llm.chat(cfg, "你是一个测试助手。", "只回复两个字：可用", max_tokens=20)
                print(f"[✓] LLM 连通，返回：{reply.strip()[:30]}")
            except PodError as exc:
                print(f"[×] LLM 调用失败：{exc.message}\n    → {exc.hint or ''}")
                ok = False
    else:
        print("[×] 没有配置 LLM_API_KEY，无法生成笔记")
        ok = False

    # 本地转录环境
    import shutil

    if asr.asr_available():
        print("[✓] mlx-whisper 已安装（本地转录兜底可用）")
    else:
        print("[!] mlx-whisper 未安装（没有官方文稿的单集会失败）")
    if shutil.which("ffmpeg"):
        print("[✓] ffmpeg 已安装")
    else:
        print("[!] ffmpeg 未安装（本地转录需要：brew install ffmpeg）")

    print(f"\n状态目录：{STATE_DIR}")
    print("\n" + ("体检通过，可以直接用了。" if ok else "有问题需要先处理（上面标 × 的项）。"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pod",
        description="小宇宙播客链接 -> AI 结构化笔记 -> Obsidian",
    )
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--vault", help="覆盖 vault 路径")
        sp.add_argument("--model", help="覆盖 LLM 模型名")

    # 默认命令（pod <url>）
    run = sub.add_parser("run", help="完整流程（默认）")
    run.add_argument("url", nargs="?", help="小宇宙链接；省略则读剪贴板")
    add_common(run)
    run.add_argument(
        "--prefer",
        choices=["auto", "official", "asr"],
        default="auto",
        help="文字稿来源偏好，默认 auto（先官方后本地）",
    )
    run.add_argument("--official", dest="prefer", action="store_const", const="official",
                     help="只用官方文字稿")
    run.add_argument("--asr", dest="prefer", action="store_const", const="asr",
                     help="强制本地转录")
    run.add_argument("--transcript-file", help="直接用本地文字稿文件，跳过抓取")
    run.add_argument("--save-transcript", help="把文字稿另存到指定路径")
    run.add_argument("--no-write", action="store_true", help="只打印笔记，不写入 vault")
    run.add_argument("--force", action="store_true", help="已记录过也重新生成")
    run.add_argument("--open", action="store_true", help="生成后用 Obsidian 打开")
    run.set_defaults(func=cmd_run)

    login = sub.add_parser("login", help="小宇宙短信登录（拿官方文字稿）")
    login.add_argument("--phone")
    login.add_argument("--code")
    add_common(login)
    login.set_defaults(func=cmd_login)

    logout = sub.add_parser("logout", help="删除本地登录 token")
    add_common(logout)
    logout.set_defaults(func=cmd_logout)

    tr = sub.add_parser("transcript", help="只导出文字稿")
    tr.add_argument("url", nargs="?")
    add_common(tr)
    tr.add_argument("--prefer", choices=["auto", "official", "asr"], default="auto")
    tr.add_argument("--plain", action="store_true", help="不带时间戳")
    tr.set_defaults(func=cmd_transcript)

    ui = sub.add_parser("ui", help="启动本地网页界面（推荐，不用记命令）")
    add_common(ui)
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ui.set_defaults(func=cmd_ui)

    doc = sub.add_parser("doctor", help="体检配置和环境")
    add_common(doc)
    doc.add_argument("--skip-llm", action="store_true", help="不试调 LLM")
    doc.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"run", "login", "logout", "transcript", "doctor", "ui", "-h", "--help"}
    # 允许 `pod <url>` 省略 run 子命令
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv.insert(0, "run")
    elif not argv:
        argv = ["run"]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except PodError as exc:
        print(f"\n✗ {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  → {exc.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
