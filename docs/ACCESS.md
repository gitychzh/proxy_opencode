# 访问指南（ACCESS）

> **凭据政策（2026-10-02 修订）**：本仓库为 **PUBLIC** 仓库。本文及仓库内任何文件**不得**出现真实 IP 之外的凭据（API key / AK / 令牌 / 密码 / Cookie）。
> 所有真实凭据只存放在**仓库外**的位置：设备侧配置文件（如 `/etc/egress-agent/*.json`）、`scripts_local/secrets.env`（已 gitignore）等。
> 本文只保留占位符与“去哪取”的指引，不记录任何凭据值。

## 1. 新栈入口（0.7.x）

端口注册表以 [`docs/ARCHITECTURE.md`](ARCHITECTURE.md#2-端口注册表统一口径) 为唯一来源。

- **客户端入口（IP 直连）**：`https://115.29.231.25:10002/v1`（IP 直连 + 自签 CA）
- **客户端入口（域名）**：`https://llm.223722.xyz:10002/v1`（LE 真证书，客户端零安装）
- **设备侧 egress-agent**：`10001`（opc2-eth 专用 `10002`）
- **被反代模型清单（排障专用）**：`https://115.29.231.25:10002/v1/models_cus?key=<KEY>`（仅展示上游裸 ID，聊天接口不接受裸 ID）

> 标准 `/v1/models` 只列公开别名 `deepseek-ai/deepseek-v4.1-flash`（严格 OpenAI 形状）。IP 入口需先信任自签 CA。

## 2. 网络底座

- **Tailscale tailnet**：各桶设备、ECS 互通，SSH 全走 Tailscale IP
- **公网入口**：IP 直连（移动网环境推荐）或域名入口（LE 证书）

## 3. 凭据与密钥去向（仅索引）

| 类别 | 存放位置 | 说明 |
|---|---|---|
| **网关 API Key（客户端）** | 环境变量/部署文件（仓库外） | `GATEWAY_API_KEYS` 等只写入设备/ECS 的 `.env` 或 systemd 环境，不入仓库。 |
| **LB 管理 Key** | ECS 环境（仓库外） | `ZEN_LB_API_KEY` 只进环境变量，LB fail-closed 未设置时拒绝请求。 |
| **ECS 管理员 Key** | ECS 环境（仓库外） | 用于 `/admin/*` 接口，仅限内网/Tailscale 调用。 |
| **ONT 凭据（光猫）** | 设备侧：`/etc/egress-agent/*.json`（600 权限） | 仅存放于各桶设备，**不提交至仓库**。详见 [`scripts_local/ont-credentials.md`](../scripts_local/ont-credentials.md)（仓库外、gitignored）。 |
| **SSH 密钥** | 各设备授权文件（仓库外） | 公钥授权，不在仓库记录私钥。 |
| **临时探测 Key** | 运行时临时生成 | 用后吊销，不落盘至仓库文件。 |

## 4. 远程命令纪律

- **Windows → ECS/opc2**：一律用 `base64` 编码命令后通过 SSH 执行：`echo $b64 | base64 -d | python3 -`（或等价方式），避免特殊字符转义问题。
- **adb 到 cc9**：同样必须全量 `base64`，不要直接传多行脚本。
- **Shell 安全**：远程批处理脚本避免在命令行拼接明文凭据。

## 5. 访问入口速览

- **新栈：** `https://115.29.231.25:10002/v1` 或 `https://llm.223722.xyz:10002/v1`
- **健康检查：** `GET /healthz`（无需鉴权）
- **模型目录：** `GET /v1/models`（仅公开别名）
- **被反代裸 ID 清单：** `GET /v1/models_cus`（需鉴权，排障专用）

更多拓扑、端口、三桶身份与出口见 [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)。运维操作、排障清单见 [`docs/OPERATIONS.md`](OPERATIONS.md)。