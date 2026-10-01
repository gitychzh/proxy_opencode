"""Guard: no real credential may ever be committed (see SECURITY.md).

This repo is PUBLIC. 2026-10-02 一轮排查发现 `docs/ACCESS.md` 等文件把云账号
AK、Cloudflare 全权 Key、root 密码、网关 key 以明文提交进了公开仓库——文件
删除无法消除历史，只能轮换凭据。本测试是防回归闸门：它扫描**全部 git 跟踪
文件**，命中已知凭据形态即失败。

设计约束：
  * 只用**结构性**正则（前缀 + 字符类），绝不把真实凭据值写进本文件——
    否则守卫自己就成了泄露点。
  * 正则源码本身不会自我命中（`cfk_[` 后跟 `[`，不满足 `[A-Za-z0-9]{25,}`）。
  * 只扫跟踪文件，因此本地 `.env` / `keys.json` / `scripts_local/` 不会误报。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (name, pattern). Structural shapes only — no literal secret values.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 阿里云 AccessKey ID (LTAI + 12~20 位字母数字)
    ("alibaba-accesskey-id", re.compile(r"\bLTAI[0-9A-Za-z]{12,20}\b")),
    # Cloudflare Global API Key / scoped token / legacy tunnel token
    ("cloudflare-global-key", re.compile(r"\bcfk_[A-Za-z0-9]{25,}\b")),
    ("cloudflare-api-token", re.compile(r"\bcfut_[A-Za-z0-9]{25,}\b")),
    # 本项目历史网关 key 形态
    ("legacy-gateway-key", re.compile(r"\bapi_[a-z]{3,}\d{8,}\b")),
    ("lb-static-key", re.compile(r"\bgw-lb-[A-Za-z0-9_-]{16,}\b")),
    # 私钥块
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

# 命中行若含这些标记，视为占位符/示例，放行。
ALLOW_MARKERS = (
    "dummy",
    "placeholder",
    "CHANGE-ME",
    "change-me",
    "example",
    "REDACTED",
    "redacted",
)

# 体积大且确认为纯文本资产，跳过（仍会被结构性正则检查，但避免无谓读取）。
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl")


def _tracked_files() -> list[Path]:
    """Git-tracked files; falls back to a directory walk when git is absent."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        skip_dirs = {
            ".git", ".venv", ".venv312", "__pycache__", ".pytest_cache",
            ".ruff_cache", ".mypy_cache", "node_modules", "logs", "scripts_local",
        }
        files: list[Path] = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            files.append(path)
        return files
    return [REPO_ROOT / name for name in out.decode().split("\0") if name]


def _scan() -> list[str]:
    hits: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in ALLOW_MARKERS):
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{lineno}: {name}")
    return hits


def test_no_credentials_committed() -> None:
    hits = _scan()
    if hits:
        pytest.fail(
            "检测到疑似真实凭据入库（本仓库为 PUBLIC，禁止提交凭据）：\n  "
            + "\n  ".join(hits)
            + "\n请把真实值移到仓库外（scripts_local/secrets.env），"
            "文档改用占位符，并轮换已泄露的凭据。"
        )
