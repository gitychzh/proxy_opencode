#!/usr/bin/env bash
# proxy_opencode 节点一键部署（Linux 服务器 / Termux 通用）
#
# 用法：
#   bash deploy_node.sh [PORT]
#   PORT 默认 8791。
#
# 做什么：
#   1. 安装系统依赖（python3 + venv；Termux 下用 pkg）
#   2. 克隆/更新仓库到 $REPO_DIR（默认 ~/proxy_opencode）
#   3. 建 venv 并安装本包
#   4. 写 .env（PORT/HOST/GATEWAY_API_KEYS，凭据不进 git）
#   5. 写 run_node.sh 启动脚本
#
# 环境变量：
#   REPO_URL   仓库地址（默认 https://github.com/gitychzh/proxy_opencode.git）
#   REPO_DIR   安装目录（默认 ~/proxy_opencode）
#   GATEWAY_API_KEYS  网关 key（必填，多把逗号分隔）
#   HOST       监听地址（默认 0.0.0.0；仅本机用 127.0.0.1）
#
# systemd 部署见 scripts/proxy_opencode.service。

set -euo pipefail

PORT="${1:-8791}"
HOST="${HOST:-0.0.0.0}"
REPO_URL="${REPO_URL:-https://github.com/gitychzh/proxy_opencode.git}"
REPO_DIR="${REPO_DIR:-$HOME/proxy_opencode}"

echo "==> [1/5] 系统依赖"
if command -v pkg >/dev/null 2>&1; then
    pkg install -y python git
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip git
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 git
else
    echo "!! 未识别的包管理器，请自行确保 python3 + git 可用" >&2
fi

echo "==> [2/5] 仓库 → $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" fetch origin && git -C "$REPO_DIR" reset --hard origin/main
else
    git clone "$REPO_URL" "$REPO_DIR"
fi

echo "==> [3/5] venv + 安装"
cd "$REPO_DIR"
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .

echo "==> [4/5] 写 .env"
if [ -z "${GATEWAY_API_KEYS:-}" ]; then
    echo "!! GATEWAY_API_KEYS 未设置：非回环监听会被网关拒绝，先填再跑" >&2
    exit 1
fi
cat > "$REPO_DIR/.env" <<EOF
PORT=$PORT
HOST=$HOST
GATEWAY_API_KEYS=$GATEWAY_API_KEYS
EOF
chmod 600 "$REPO_DIR/.env"

echo "==> [5/5] 写 run_node.sh"
cat > "$REPO_DIR/run_node.sh" <<EOF
#!/usr/bin/env bash
set -a
. "$REPO_DIR/.env"
set +a
exec "$REPO_DIR/.venv/bin/python" -m proxy_opencode
EOF
chmod +x "$REPO_DIR/run_node.sh"

echo "==> 完成。前台试跑：  $REPO_DIR/run_node.sh"
echo "    验证：           curl -s http://127.0.0.1:$PORT/healthz"
echo "    systemd：        参考 scripts/proxy_opencode.service"
