# 0.7.x 网关镜像（proxy_opencode）——唯一部署点：ECS 容器。
#
# 构建上下文 = 仓库根目录：
#   docker build -f deploy/ecs/Dockerfile -t zen-gw:0.7 .
# 或直接 docker compose -f deploy/ecs/compose.yml up -d --build
#
# 运行期只访问：上游 Zen API（经设备 egress-agent）与设备 agent 的
# /healthz；没有任何云端构建/遥测调用。依赖装在本镜像内（不锁云端）。
FROM python:3.12-slim

# bun 传输栈（2026-10-24 传输指纹门）：免费层上游按 TLS/HTTP 栈指纹判定，
# httpx（OpenSSL/HTTP1.1）的请求一律被伪装成 429 配额额满；bun（boringssl/
# HTTP2）——官方客户端同栈——同字节重放得 200。网关把 TLS/H2 跳交给 bun
# （zen_bun/forward.ts 子进程，经每桶 agent 隧道出站，SSE 直通）。预装到
# /usr/local/bin/bun；下载走 npmmirror（构建期一次性行为，运行期零外网依赖）。
# 国内构建走镜像源（默认清华 TUNA）；可用 --build-arg 覆盖。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG BUN_VERSION=1.4.4
ARG BUN_URL=https://registry.npmmirror.com/-/binary/bun/bun-v${BUN_VERSION}/bun-linux-x64.zip
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=10002 \
    ZEN_BUN_TRANSPORT=1 \
    ZEN_BUN_BIN=/usr/local/bin/bun \
    ZEN_BUN_SCRIPT=/app/zen_bun/forward.ts

WORKDIR /app

RUN python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/bun.zip')" "${BUN_URL}" \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/bun.zip').extractall('/tmp')" \
    && mv /tmp/bun-linux-x64/bun /usr/local/bin/bun \
    && chmod 0555 /usr/local/bin/bun \
    && rm -rf /tmp/bun.zip /tmp/bun-linux-x64

# 先只装包本体；pyproject 声明的依赖随 index 拉取。README.md 是 pyproject
# 的 readme 字段，必须一起拷入，否则构建报错。
COPY pyproject.toml README.md ./
COPY proxy_opencode ./proxy_opencode
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10002 gw

USER gw

# 容器内监听全部接口；compose 只把端口发布到宿主机 loopback，由 caddy 反代。
EXPOSE 10002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:10002/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", \
     "--factory", "proxy_opencode.app:create_app_from_env", \
     "--host", "0.0.0.0", "--port", "10002"]
