#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# AutoFlow 网关镜像构建 + 安全重建（方案 C 自更新镜像固化）
#
# 用途：在 NAS 部署机（含 docker-compose.yml / Dockerfile 的仓库根）重建网关镜像并重建容器。
# 关键保障：
#   1. 重建前把【当前运行容器】冻结为回退点（autoflow-autoflow_gateway:backstop-<ts>），
#      万一新镜像启动失败可一键回滚，不影响线上。
#   2. 仅 build 成功后才 recreate；build 失败直接退出，旧容器不受影响。
#   3. recreate 后做 HTTP 健康检查 + git/ssh 固化校验；不达标打印回滚命令。
#
# 已知坑（已写入 Dockerfile，勿回退）：
#   · FROM 必须用 ghcr.io/astral-sh/uv:python3.12-bookworm-slim（含 shell+apt）。
#     `uv:latest` 已漂移到无 /bin/sh 的 distroless，会使 `RUN apt-get` 全失败。
#   · Dockerfile 须 COPY README.md（pyproject 的 readme 字段要求，否则 uv install 失败）。
#   · pyproject 须钉 mcp<2（代码用 v1 的 mcp.server.fastmcp API，mcp 2.x 会崩启动）。
#
# 用法：bash scripts/build_image.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
SERVICE=autoflow_gateway
IMAGE=autoflow-autoflow_gateway

echo "==> 项目目录: $PROJECT_DIR"

command -v docker >/dev/null 2>&1 || { echo "❌ 缺少 docker"; exit 1; }
docker compose config --services 2>/dev/null | grep -q "$SERVICE" || { echo "❌ compose 未找到服务 $SERVICE"; exit 1; }

# 1) 冻结当前运行态为回退点
TS="$(date +%Y%m%d-%H%M%S)"
BACKSTOP="${IMAGE}:backstop-${TS}"
if docker ps -q -f "name=${SERVICE}" | grep -q .; then
  echo "==> 冻结当前容器为回退点: $BACKSTOP"
  docker commit "$SERVICE" "$BACKSTOP"
else
  echo "==> 当前无运行容器，跳过冻结"
fi

# 2) 构建新镜像（仅 build，不自动 recreate）
echo "==> docker compose build $SERVICE"
if ! docker compose build "$SERVICE"; then
  echo "❌ 构建失败；旧容器仍在运行，无需回滚。"
  exit 1
fi

# 3) 重建容器
echo "==> docker compose up -d --force-recreate $SERVICE"
docker compose up -d --force-recreate "$SERVICE"

# 4) 健康检查（最多等 60s）
echo "==> 等待启动并健康检查 (http://localhost:8000/)..."
code="000"
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/ 2>/dev/null || true)"
  [ "$code" = "200" ] && break
  sleep 2
done
if [ "$code" != "200" ]; then
  echo "❌ 网关未就绪 (HTTP ${code})。回退："
  echo "    docker tag $BACKSTOP $IMAGE:latest && docker compose up -d --force-recreate $SERVICE"
  exit 1
fi
echo "✓ 网关就绪 HTTP 200"

# 5) 验证 git/ssh 已固化进镜像（recreate 后不丢）
gitv="$(docker exec "$SERVICE" git --version 2>/dev/null || true)"
sshV="$(docker exec "$SERVICE" ssh -V 2>&1 | head -1 || true)"
echo "✓ git: ${gitv:-缺失} | ssh: ${sshV:-缺失}"
if [ -n "$gitv" ] && [ -n "$sshV" ]; then
  echo "✓ git/ssh 已固化进镜像（docker compose up -d 重建不丢）"
else
  echo "⚠️ git/ssh 未固化，请检查 Dockerfile 是否含 openssh-client"
fi

echo "==> 完成。回退点: $BACKSTOP（确认无误后可 docker rmi 清理）"
