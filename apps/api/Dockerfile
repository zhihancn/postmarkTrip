# syntax=docker/dockerfile:1.7
# =============================================================================
# postmarkTrip · API / Worker 镜像
#
# 构建上下文是**仓库根目录**（因为要拷 contract/）：
#
#     docker build -f apps/api/Dockerfile -t postmarktrip/api:dev .
#
# compose 中的对应写法：
#
#     build:
#       context: .
#       dockerfile: apps/api/Dockerfile
#
# 同一镜像服务两个角色：
#   * api    —— uvicorn + FastAPI（默认）
#   * worker —— ARQ 任务队列（compose 里 command: ["worker"]）
#
# [recognition] extra（OpenCV / numpy / rapidocr）由 **build arg** 控制，
# 默认关闭：
#
#     docker compose build --build-arg INSTALL_RECOGNITION=1 api
#
# 为什么默认关而不是默认开：
#   * v0.1 的录入/上传/导出完全不碰识别，装上它白胖几百 MB
#   * 识别依赖是**纯增量**的：没装的时候 providers 探测会如实报 cv_ready=false，
#     而不是让镜像"看起来准备好了"
# 为什么用 build arg 而不是两个 Dockerfile：两个文件必然漂移，
#   而且 compose 里要维护两套 build 配置。
# =============================================================================

FROM python:3.12-slim AS runtime

#: 1 = 安装 [recognition] extra。见文件头。
ARG INSTALL_RECOGNITION=0

#: 宿主用户的 gid。绑定挂载的宿主目录（data/assets、inbox）会设成这个组 +
#: 组写 + setgid，于是**宿主用户与容器用户都能写**，而宿主目录的属主不变。
#:
#: 为什么需要这个：以前 entrypoint 直接对宿主目录 `chown pmtrip:pmtrip`，
#: 把属主改成 10001，**宿主用户因此失去写权限** —— 坏掉的是 `make backup`
#: 的资产清单、宿主侧 rsync 备份、以及手工往 assets/inbox 放文件。
#: 容器自己能写并不代表这样没问题：宿主目录是用户的东西。
#:
#: 默认 0 = 不建组、只加组写权限（在宿主 gid 不可知时的安全退化）。
#: compose 会把它默认成宿主登录用户的 gid（见 docker-compose.yml）。
ARG HOST_GID=0

# --------------------------------------------------------------------------- #
#  基础环境
# --------------------------------------------------------------------------- #
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=Asia/Shanghai \
    APP_HOME=/app/apps/api

# --------------------------------------------------------------------------- #
#  系统依赖
#
#  * gosu   —— entrypoint 以 root 修正绑定挂载目录的属主后**立即降权**到 pmtrip。
#              应用进程本身始终以非 root 运行。
#  * tzdata —— python:3.12-slim 不自带时区库，没有它 TZ=Asia/Shanghai 不生效。
#
# **不装** libpq-dev / gcc / build-essential：
#   psycopg[binary] 自带 libpq，argon2-cffi 与 pillow 都提供 manylinux wheel，
#   整个依赖树无需编译。少这一层就让镜像小几百 MB。
# --------------------------------------------------------------------------- #
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    echo "${TZ}" > /etc/timezone

# --------------------------------------------------------------------------- #
#  非 root 运行用户（uid/gid 固定为 10001，便于宿主侧对齐）
#
#  HOST_GID 有效时额外建一个同名组并把 pmtrip **加入**它（不改 pmtrip 的主组）。
#  这样绑定挂载目录设成宿主组后，容器用户凭附加组身份就能写。
#  注：附加组成员关系由**进程**的组列表决定，而 gosu 保留了进程的附加组，
#  所以 `gosu pmtrip` 之后依然生效。
# --------------------------------------------------------------------------- #
RUN set -eux; \
    groupadd --gid 10001 pmtrip; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin pmtrip; \
    if [ "$HOST_GID" != "0" ] && [ "$HOST_GID" != "10001" ]; then \
        groupadd --gid "$HOST_GID" hostuser; \
        usermod --append --groups hostuser pmtrip; \
        echo "pmtrip 已加入宿主组 gid=$HOST_GID"; \
    else \
        echo "未提供 HOST_GID（或与 pmtrip 相同），跳过宿主组"; \
    fi

WORKDIR /app

# --------------------------------------------------------------------------- #
#  依赖层 —— 只要 pyproject.toml 不变就命中缓存
#
#  这里造的 README.md / __init__.py 是**构建期占位**，不写回仓库：
#   pyproject.toml 声明了 readme = "README.md"，但 apps/api/README.md 由
#  API 作者维护、当前尚未创建；缺它会让 hatchling 在元数据阶段直接报错。
#  占位只在构建层的临时副本里，随后会被真实的 src/ 覆盖。
#
#  ⚠️ INSTALL_RECOGNITION=1 时分**两步**装，这是必需的，不是风格问题：
#
#     rapidocr-onnxruntime 的元数据声明 `opencv-python`（非 headless）。
#     若让 pip 去解析 rapidocr 的依赖，它会同时装上 opencv-python 与
#     opencv-python-headless，而**非 headless 的那个生效** —— 它需要 X11，
#     在 python:3.12-slim 里 import cv2 直接报
#
#         ImportError: libxcb.so.1: cannot open shared object file
#
#     结果：识别栈完全不可用（cv2 与 rapidocr 都 import 失败），
#     而 `pip list` 看起来一切正常。实测撞到过一次。
#
#     第 1 步装 `[recognition]`：这个 extra 现在**包含** rapidocr 本体，
#     所以 pip 会去解析它的元数据，**opencv-python（非 headless）会被拖进来**
#     —— 两个包都提供 cv2，非 headless 的那个赢。
#     第 2 步用 `--no-deps` 装 rapidocr 本体，保留是为了版本可控。
#     第 3 步**强制回到 headless**：卸掉 opencv-python，再用
#     --force-reinstall --no-deps 装回 opencv-python-headless。
#     这一步不能省 —— 省掉就是 libxcb.so.1 那个错。
#
#     代价：rapidocr 将来新增的依赖不会自动跟上。
#     这条由 tests/test_recognition_deps.py 守着 —— 它把 rapidocr 自己声明的
#     依赖与 extra 的声明对账，缺哪个就报哪个名字。
# --------------------------------------------------------------------------- #
COPY apps/api/pyproject.toml ./apps/api/pyproject.toml
RUN set -eux; \
    if [ ! -f apps/api/README.md ]; then : > apps/api/README.md; fi; \
    mkdir -p apps/api/src/postmarktrip; \
    if [ ! -f apps/api/src/postmarktrip/__init__.py ]; then \
        : > apps/api/src/postmarktrip/__init__.py; \
    fi; \
    python -m pip install --upgrade pip; \
    if [ "$INSTALL_RECOGNITION" = "1" ]; then \
        echo "第1步：基础依赖 + [recognition]（headless opencv + rapidocr 的传递依赖）"; \
        pip install -e './apps/api[recognition]'; \
        echo "第2步：--no-deps 装 rapidocr 本体（固定版本，不让 pip 重复解析依赖）"; \
        pip install --no-deps rapidocr-onnxruntime; \
        echo "第3步：卸掉非 headless 的 opencv-python，强制回到 headless"; \
        pip uninstall -y opencv-python || true; \
        pip install --force-reinstall --no-deps 'opencv-python-headless>=4.10'; \
        echo "自检：cv2 与 rapidocr 必须真的能 import"; \
        python -c "import cv2, numpy, onnxruntime, rapidocr_onnxruntime; \
                   print('识别栈 OK | cv2', cv2.__version__, '| numpy', numpy.__version__)"; \
    else \
        echo "跳过 [recognition] extra（INSTALL_RECOGNITION=0）"; \
        pip install -e ./apps/api; \
    fi

# --------------------------------------------------------------------------- #
#  源码与运行时资产
#
#  用 editable 安装（上一步的 -e），因此把真实 src/ 覆盖进来即生效 ——
#  这也是 docker-compose.dev.yml 能直接挂载 src/ 做热重载的前提。
# --------------------------------------------------------------------------- #
COPY apps/api/src            ./apps/api/src
COPY apps/api/migrations     ./apps/api/migrations
COPY apps/api/alembic.ini    ./apps/api/alembic.ini
COPY apps/api/entrypoint.sh  ./apps/api/entrypoint.sh

# contract/ 是三方共享的契约层（场景图 JSON Schema）。API 侧在 v1.1 起会读取它，
# 现在就拷进来，路径与仓库布局保持一致：/app/contract。
COPY contract ./contract

# --------------------------------------------------------------------------- #
#  运行时目录（无绑定挂载时也能正常启动）
# --------------------------------------------------------------------------- #
RUN set -eux; \
    mkdir -p /data/assets /inbox /public-data /models /backups; \
    sed -i 's/\r$//' apps/api/entrypoint.sh; \
    chmod +x apps/api/entrypoint.sh; \
    chown -R pmtrip:pmtrip /app /data /inbox /public-data /models /backups

# --------------------------------------------------------------------------- #
#  入口
#
#  PID 1 是 entrypoint 脚本（root，仅用于对齐挂载目录属主后降权）；
#  **应用进程以 pmtrip(10001) 运行**。
#
#  刻意**不写镜像级 HEALTHCHECK**：worker 与 api 共用本镜像，而 worker 不提供
#  HTTP 端点。健康检查按角色在 compose 里分别定义。
# --------------------------------------------------------------------------- #
ENTRYPOINT ["/app/apps/api/entrypoint.sh"]
CMD ["api"]

# 文档性声明：uvicorn 在容器内监听 8000，对外由 gateway(nginx) 暴露。
EXPOSE 8000
