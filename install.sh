#!/usr/bin/env bash
#
# AutoFlow 一键安装脚本（Docker）
# ============================================================
# 用法（默认从 GitHub 拉取源码并在本机构建镜像）：
#   curl -fsSL https://raw.githubusercontent.com/lidicn/AutoFlow/main/install.sh | sh
#
# 环境变量 / 参数：
#   REPO=lidicn/AutoFlow   仓库（fork 时改这里）
#   BRANCH=main                    分支
#   INSTALL_DIR=/opt/autoflow      安装目录（macOS 默认 ~/autoflow）
#   sh install.sh --install-docker 本机没装 Docker 时顺带安装（仅 Linux）
#   sh install.sh --update         拉最新代码并重建镜像（保留 data/ 与 .env）
#   sh install.sh -d <dir>         指定安装目录
# ============================================================
set -euo pipefail

REPO="${REPO:-lidicn/AutoFlow}"
BRANCH="${BRANCH:-main}"
if [ "$(uname)" = "Darwin" ]; then
  DEFAULT_DIR="$HOME/autoflow"
else
  DEFAULT_DIR="/opt/autoflow"
fi
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_DIR}"
UPDATE=0
INSTALL_DOCKER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE=1 ;;
    --install-docker) INSTALL_DOCKER=1 ;;
    -d|--dir) INSTALL_DIR="$2"; shift ;;
    -b|--branch) BRANCH="$2"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
  shift
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── 颜色（非 TTY 时关闭）──
if [ -t 1 ]; then C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[34m'; C_N=$'\033[0m'; else C_R=""; C_G=""; C_Y=""; C_B=""; C_N=""; fi
info(){ printf "${C_B}==>${C_N} %s\n" "$*"; }
ok(){ printf "${C_G}✓${C_N} %s\n" "$*"; }
warn(){ printf "${C_Y}!${C_N} %s\n" "$*"; }
err(){ printf "${C_R}✗ %s${C_N}\n" "$*" >&2; }
need(){ command -v "$1" >/dev/null 2>&1 || { err "缺少依赖: $1"; exit 1; }; }

info "AutoFlow 一键安装  (REPO=$REPO  BRANCH=$BRANCH  DIR=$INSTALL_DIR)"

# ── 1. 依赖 ──
need curl
need tar

if ! command -v docker >/dev/null 2>&1; then
  if [ "$INSTALL_DOCKER" = "1" ]; then
    if [ "$(uname)" = "Darwin" ]; then
      err "macOS 请安装 Docker Desktop：https://docs.docker.com/desktop/install/"; exit 1
    fi
    [ "$(id -u)" = "0" ] || { err "安装 Docker 需要 root，请用 sudo 重新运行"; exit 1; }
    info "未检测到 Docker，开始安装（get.docker.com）…"
    curl -fsSL https://get.docker.com -o "$TMP/get-docker.sh"
    sh "$TMP/get-docker.sh"
    command -v docker >/dev/null 2>&1 || { err "Docker 安装失败，请手动安装：https://docs.docker.com/get-docker/"; exit 1; }
    command -v systemctl >/dev/null 2>&1 && systemctl start docker 2>/dev/null || true
  else
    err "未检测到 Docker。请先安装（https://docs.docker.com/get-docker/），或加 --install-docker 让脚本代装（Linux）。"
    exit 1
  fi
fi

docker compose version >/dev/null 2>&1 || { err "缺少 docker compose 插件（v2）。请安装 Docker Compose v2 后重试。"; exit 1; }
docker info >/dev/null 2>&1 || { err "Docker 守护进程不可达。Linux: sudo systemctl start docker；macOS: 打开 Docker Desktop。"; exit 1; }
ok "Docker 就绪"

# ── 2. 目录 ──
if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
  if [ "$UPDATE" = "1" ]; then
    info "更新模式：保留 data/ 与 .env，拉取最新代码重建镜像"
  else
    err "目标目录已存在 AutoFlow：$INSTALL_DIR — 为防止误覆盖已停止。请用 --update，或换 INSTALL_DIR=-d <其他目录>。"
    exit 1
  fi
else
  mkdir -p "$INSTALL_DIR"
fi

# ── 3. 拉取构建上下文（tarball，无需 git）──
mkdir -p "$TMP/repo"
TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
info "下载仓库快照：$TARBALL_URL"
if ! curl -fsSL "$TARBALL_URL" -o "$TMP/autoflow.tar.gz"; then
  err "下载失败。请确认 REPO/BRANCH 正确且能访问 GitHub。"
  exit 1
fi
ok "下载完成"
tar xzf "$TMP/autoflow.tar.gz" -C "$TMP/repo" --strip-components=1
# 并入安装目录；data/ 与 .env 不在 tarball 中，不会被覆盖
cp -a "$TMP/repo/." "$INSTALL_DIR/"
ok "代码已就绪：$INSTALL_DIR"

# ── 4. .env ──
if [ ! -f "$INSTALL_DIR/.env" ] && [ -f "$INSTALL_DIR/.env.example" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  info ".env 已生成（连接建议在 WebUI「连接配置」里填，更安全）"
fi

# ── 5. 启动 ──
cd "$INSTALL_DIR"
info "构建并启动容器（首次会拉取 uv 基础镜像并用 uv 装包，请稍候）…"
docker compose up -d --build
ok "容器已启动"

# ── 6. 健康检查（仅提示，不阻断）──
info "等待 WebUI 就绪（:8000）…"
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://localhost:8000/" >/dev/null 2>&1; then
    ok "WebUI 已响应"
    break
  fi
  sleep 2
done

echo
echo "────────────────────────────────────────────────"
ok "AutoFlow 已安装并运行"
echo
echo "  控制面（WebUI）：  http://<本机IP>:8000"
echo "    1) 设置 → 连接配置：填写 HA / Node-RED 地址与令牌"
echo "    2) Agents 面板：给 agent 生成身份码（连 MCP 必备）"
echo
echo "  持久化数据：  $INSTALL_DIR/data"
echo "  配置文件：    $INSTALL_DIR/.env"
echo
echo "  日志：  docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
echo "  停止：  docker compose -f $INSTALL_DIR/docker-compose.yml down"
echo "  更新：  sh install.sh --update"
echo "────────────────────────────────────────────────"
