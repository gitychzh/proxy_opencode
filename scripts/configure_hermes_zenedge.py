#!/usr/bin/env python3
"""为 ECS hermes 生成/校准 proxy_opencode (zenedge) 四免费模型的 provider 配置。

背景（2026-11-16 实测，详见 docs/MODEL_PROFILE.md §11）：

* 上游免费层对**单次请求**另有一道「估算器门」，与模型自身窗口无关：
  其估算器固定按 ``about = round(chars * 0.339)`` 计（与内容语言/类型无关，
  自然中文/英文/纯符号串同一比率），``about > 32000`` 即 400
  ``context_length_exceeded``。换算式：**约 94.4K chars 就是硬顶**。
* 模型自身窗口远大于此（big-pickle 实测 368K+、minimax 实测 1,000,000 可过），
  但常规文本永远先撞估算器门。只有用 4-byte 罕见字符（真实 4.1 tok/char vs
  估算 0.339）把真实 tokens 藏在 94K chars 内才测得到 384K——**那是测量技巧，
  不是可用容量**。
* hermes 内部用 ``4 chars/token`` 估算（``agent/model_metadata.CHARS_PER_TOKEN``），
  与上游估算口径不同，且 ``context_length < 64000`` 会被 ``agent_init`` 直接拒绝。
  因此 ``context_length`` 只能填 64000（硬下限），实际压缩靠 provider-overflow 兜底。

本脚本只做两件可复现的事：写 provider/overrides 块 + 校验。

用法::

    python3 scripts/configure_hermes_zenedge.py --config ~/.hermes/config.yaml [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 实测事实（2026-11-16，ECS 115.29.231.25:10002，usage.prompt_tokens 回读）
# ---------------------------------------------------------------------------
MODELS: dict[str, dict[str, object]] = {
    # id: 能力与窗口。context_window 用 hermes 硬下限 64000（见模块 docstring）。
    "big-pickle": {
        "ctx": 64000,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "family": "big-pickle",
    },
    "deepseek-v4.1-flash-free": {
        "ctx": 64000,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "family": "deepseek-flash",
    },
    "glm-5.3-flash-free": {
        "ctx": 64000,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "family": "glm-flash",
    },
    "minimax-m3.1-flash-free": {
        "ctx": 64000,
        "tools": True,
        "vision": False,
        "reasoning": True,
        "family": "minimax-flash",
    },
}

PROVIDER_KEY = "zenedge"
PROVIDER_NAME = "OpenCode Zen free (via proxy_opencode :10002)"
BASE_URL = "http://127.0.0.1:10002/v1"
DEFAULT_MODEL = "big-pickle"
KEY_ENV = "HERMES_CUSTOM_ZENEDGE_API_KEY"

PROVIDER_COMMENT = """    # context_length 取 hermes 硬下限 64000（更低会 ValueError）。
    # 免费层真实约束是上游估算器门：
    #   about = round(chars x 0.339) <= 32000，即约 94.4K chars 硬顶，
    #   与内容语言无关，远早于模型窗口与 0.75 压缩阈值。
    # 故实际靠 provider-overflow 兜底压缩（日志 trigger_source=overflow 可见）。
    # 模型窗口实测：big-pickle 368K+、minimax 1M 可过（MODEL_PROFILE §11）。
"""


def _provider_block() -> str:
    lines = [
        f"  {PROVIDER_KEY}:",
        f"    name: {PROVIDER_NAME}",
        f"    base_url: {BASE_URL}",
        f"    api_key: ${{{KEY_ENV}}}",
        "    api_mode: chat_completions",
        f"    model: {DEFAULT_MODEL}",
        "    discover_models: false",
        "    catalog_provider: zai",
    ]
    lines.append(PROVIDER_COMMENT.rstrip("\n"))
    lines.append("    models:")
    for mid, meta in MODELS.items():
        lines.append(f"      {mid}:")
        lines.append(f"        context_length: {meta['ctx']}")
    lines.append(f"    context_length: {MODELS[DEFAULT_MODEL]['ctx']}")
    lines.append(f"    key_env: {KEY_ENV}")
    return "\n".join(lines) + "\n"


def _overrides_block() -> str:
    lines = [f"  {PROVIDER_KEY}:"]
    for mid, meta in MODELS.items():
        lines += [
            f"    {mid}:",
            f"      context_window: {meta['ctx']}",
            f"      supports_tools: {'true' if meta['tools'] else 'false'}",
            f"      supports_vision: {'true' if meta['vision'] else 'false'}",
            f"      supports_reasoning: {'true' if meta['reasoning'] else 'false'}",
            f"      model_family: {meta['family']}",
        ]
    return "\n".join(lines) + "\n"


def _splice(text: str, key: str, block: str, *, stop_key: str) -> str:
    """Replace ``key:`` .. ``stop_key:`` section with ``block``."""
    start = text.find(key)
    if start < 0:
        raise SystemExit(f"anchor {key!r} not found in config")
    end = text.find(stop_key, start)
    if end < 0:
        raise SystemExit(f"stop anchor {stop_key!r} not found after {key!r}")
    return text[:start] + block + text[end:]


def apply(config: Path, *, dry_run: bool) -> int:
    src = config.read_text(encoding="utf-8")

    # 1) providers.<zenedge> 整块（到 model_overrides 之前）
    src = _splice(
        src,
        f"  {PROVIDER_KEY}:\n    name:",
        _provider_block(),
        stop_key="model_overrides:",
    )

    # 2) model_overrides.<zenedge>（到 dashboard 之前）
    ov_start = src.index("model_overrides:\n")
    marker = f"  {PROVIDER_KEY}:\n"
    ov_zenedge = src.find(marker, ov_start)
    if ov_zenedge < 0:
        raise SystemExit("model_overrides has no zenedge section to patch")
    dash = src.index("dashboard:\n", ov_zenedge)
    src = src[:ov_zenedge] + _overrides_block() + src[dash:]

    if dry_run:
        print(src)
        return 0

    backup = config.with_suffix(config.suffix + ".bak-zenedge")
    shutil.copy2(config, backup)
    config.write_text(src, encoding="utf-8")
    print(f"patched {config} (backup: {backup})")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="~/.hermes/config.yaml", help="hermes config path")
    ap.add_argument("--dry-run", action="store_true", help="print result, do not write")
    args = ap.parse_args(argv)
    path = Path(args.config).expanduser()
    if not path.exists():
        print(f"config not found: {path}", file=sys.stderr)
        return 2
    return apply(path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
