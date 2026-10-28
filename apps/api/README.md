# apps/api —— 后端服务

Python 3.12 + FastAPI + SQLAlchemy 2.x + Alembic + PostgreSQL 16。

---

## 目录

```
apps/api/
├── pyproject.toml          依赖与工具配置（ruff / mypy / pytest）
├── Dockerfile              多阶段构建，非 root 运行
├── entrypoint.sh           等库 → 迁移 → 初始化 → 启动
├── alembic.ini
├── migrations/             Alembic
└── src/postmarktrip/
    ├── main.py             应用入口（挂载 /api 前缀）
    ├── config.py           全部配置来自环境变量（见 docs/ok/05 §四）
    ├── security.py         口令哈希（Argon2）与会话（签名 cookie）
    ├── bootstrap.py        首次启动：建管理员、写预置标签（幂等）
    ├── api/                路由层（薄）
    │   ├── deps.py         依赖：DbDep / StoreDep / CurrentUser
    │   ├── routes_auth.py  登录、登出、改口令、健康检查
    │   ├── routes_dict.py  行政区划树、邮局、标签、词表
    │   ├── routes_cards.py 卡片 / 邮戳 / 邮票
    │   ├── routes_assets.py 上传、原图、缩略图
    │   └── routes_stats.py 统计与 CSV 导出
    ├── domain/             领域层
    │   ├── vocab.py        ★ 词表的**唯一归属地**
    │   ├── schemas.py      请求/响应模型（Pydantic v2）
    │   └── tagging.py      多态标签关联
    ├── db/
    │   ├── base.py         DeclarativeBase + 通用 Mixin
    │   ├── models.py       全部表定义
    │   ├── ids.py          UUID v7
    │   └── session.py      引擎与会话
    ├── storage/            ★ 内容寻址存储（L0 原始件的守门人）
    ├── ingest/             采集（v1.0：投递目录监听）
    ├── recognition/        识别管线（v1.0）
    ├── providers/          识别 Provider 抽象（v1.0）
    └── worker/             ARQ 异步任务（v0.1 无任务，拓扑已就位）
```

---

## 三条贯穿全局的设计约定

### 1. 枚举只有一个归属地：`domain/vocab.py`

**不要在数据库里用 PG 原生 enum，也不要在别处硬编码取值。**

邮戳种类明确需要持续扩充——中国不存在覆盖全部戳种的统一官方分类标准
（见 `docs/ok/03` §1.3）。用原生 enum 意味着每加一个取值都要写一次迁移。
现在是文本列 + 集中词表 + API 层校验；前端所有下拉取值来自 `GET /api/vocab`。

### 2. 三层数据模型，界限不能糊

| 层 | 是什么 | 落在哪 |
| --- | --- | --- |
| **L0** | 原始扫描件 / 照片 —— **不可变、内容寻址** | `storage/`（只提供写一次的方法） |
| **L2** | 机器的一次"看法" —— 字段值 + 置信度 + 模型版本 | `observation` 表（可多版本并存） |
| **L3** | 你确认后的正式记录 + 修正差异 | `card` / `postmark` / `stamp` + `correction` 表 |

`observation` 与 `correction` 在 v0.1 就建表、暂不写入——它们定义了 L2/L3 的边界，
事后加会牵动大量代码。

**`correction` 是一等公民，不是日志。** 每条"机器说是 X、人改成 Y"都是标注数据，
积累一年后可以据此微调专属于本机的识别模型。

### 3. `inscription`（戳面原文）与 `place_id`（结构化地点）必须分开

戳上刻的是"北京 中关村""西藏 珠峰"——**这些不是行政区**。
原文是**证据**（原样录入、不做规范化、必填）；结构化地点是**索引**（可空）。

强行让两者合一，这些戳就无处安放，只能造假数据或放弃记录。

---

## 本地开发

```bash
# 后端服务 + 数据库 + Redis（推荐，见仓库根的 Makefile / pmtrip.ps1）
make dev

# 只跑后端（需要可达的数据库）
cd apps/api
pip install -e ".[dev]"
uvicorn postmarktrip.main:app --reload --port 8000
```

前端开发服务器跑在宿主机（`apps/web`），它把 `/api` 代理到 `localhost:9999`。

## 测试与检查

```bash
pytest                      # 单元测试
ruff check src tests        # lint
ruff format src tests       # format
mypy src                    # 类型检查（strict）
```

`tests/test_ids.py` 与 `tests/test_storage.py` 覆盖了不依赖外部服务的两块核心逻辑：
UUID v7 的性质，以及内容寻址存储的去重 / 不可变 / 越界防御。
其中一条测试断言的是 **API 形状**——存储层不得出现修改或删除原始件的方法。
