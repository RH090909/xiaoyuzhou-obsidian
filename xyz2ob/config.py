"""配置加载：.env + 环境变量。

配置优先级：命令行参数 > 环境变量 > 项目根目录 .env > 内置默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import PodError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 状态目录：存放小宇宙登录 token
STATE_DIR = Path(
    os.environ.get("XYZ2OB_STATE_DIR") or str(Path.home() / ".xyz2ob")
)

DEFAULT_VAULT_SUBDIR = "03 KNOWLEDGE/Podcasts"


def _load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析，不引入额外依赖。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        data[key.strip()] = value
    return data


@dataclass
class Config:
    vault_path: Path
    notes_subdir: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    asr_model: str
    max_chars_single_pass: int
    request_timeout: float
    # 除笔记目录外，还要扫哪些目录做表达查重（相对 vault 根目录，逗号分隔）
    extra_scan_dirs: tuple[str, ...] = ()

    @property
    def notes_dir(self) -> Path:
        return self.vault_path / self.notes_subdir

    @property
    def token_path(self) -> Path:
        return STATE_DIR / "xiaoyuzhou_token.json"

    def require_llm(self) -> None:
        if not self.llm_api_key:
            raise PodError(
                "缺少大模型 API Key，无法生成笔记。",
                "在项目根目录的 .env 里填写 LLM_API_KEY（参考 .env.example）。",
            )

    def require_vault(self) -> None:
        if not self.vault_path.exists():
            raise PodError(
                f"Obsidian vault 路径不存在：{self.vault_path}",
                "在 .env 里把 VAULT_PATH 改成你的 vault 绝对路径。",
            )


def load_config(
    *,
    vault_path: str | None = None,
    model: str | None = None,
    env_file: Path | None = None,
) -> Config:
    file_env = _load_dotenv(env_file or (PROJECT_ROOT / ".env"))

    def pick(key: str, default: str = "") -> str:
        # 环境变量优先于 .env，方便临时覆盖
        return os.environ.get(key) or file_env.get(key) or default

    raw_vault = vault_path or pick("VAULT_PATH")
    if not raw_vault:
        raise PodError(
            "没有配置 Obsidian vault 路径。",
            "复制 .env.example 为 .env，填写 VAULT_PATH。",
        )

    return Config(
        vault_path=Path(raw_vault).expanduser(),
        notes_subdir=pick("NOTES_SUBDIR", DEFAULT_VAULT_SUBDIR),
        llm_base_url=pick("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        llm_api_key=pick("LLM_API_KEY"),
        llm_model=model or pick("LLM_MODEL", "doubao-seed-1-6-250615"),
        asr_model=pick("ASR_MODEL", "mlx-community/whisper-large-v3-turbo"),
        max_chars_single_pass=int(pick("MAX_CHARS_SINGLE_PASS", "20000")),
        request_timeout=float(pick("REQUEST_TIMEOUT", "300")),
        extra_scan_dirs=tuple(
            d.strip() for d in pick("EXTRA_SCAN_DIRS").split(",") if d.strip()
        ),
    )
