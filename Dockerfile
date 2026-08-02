# 对齐参考项目 mcp_caiyun_weather 的 uv 配方
FROM ghcr.io/astral-sh/uv:latest

ENV PYTHONUNBUFFERED=1

# 项目拷贝
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY skills /app/skills
COPY run.py /app/run.py
WORKDIR /app

# 用 uv 在虚拟环境安装（镜像内 /app/.venv）
# 关键：editable(-e) 安装 —— 源码经 compose 的 ./src:/app/src 绑定挂载后，
# 容器外改 NAS 共享盘源码、docker compose restart 即生效，无需重建镜像。
RUN uv venv && uv pip install --python /app/.venv/bin/python -e .

# ⚠️ uv 的 -e 安装在某些情况下会把包【复制】进 site-packages 而非真正可编辑链接，
# 导致容器外改 /app/src（bind mount）不生效。这里强制把 site-packages 里的包目录
# 替换为指向 /app/src/autoflow_gateway 的符号链接，确保 /app/src 是唯一真相源：
# 改 NAS 共享盘源码 + docker compose restart 即生效。
RUN SP=$(/app/.venv/bin/python -c "import sysconfig; print(sysconfig.get_path('purelib'))") \
    && rm -rf "$SP/autoflow_gateway" \
    && ln -s /app/src/autoflow_gateway "$SP/autoflow_gateway"

# 共享态/备份持久卷挂载点
ENV AUTOFLLOW_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

# 默认启动 MCP(/mcp) + WebUI(/) 同端口 :8000（控制面用于配连接 / 建 agent 身份码）
CMD ["/app/.venv/bin/python", "run.py", "serve"]
