# migrations/

Alembic 迁移目录。由 `entrypoint.sh` 在每次 api 容器启动时执行 `alembic upgrade head`。

```
migrations/
├── env.py            # 运行环境：从 postmarktrip.config.settings 取数据库地址
├── script.py.mako    # 新迁移的模板
├── README.md         # 本文件
└── versions/         # 迁移脚本，按时间戳排序
```

## 手动执行

镜像里已经装好 CLI，直接进容器即可（**不要**在宿主机装 Python）：

```bash
make sh-api                  # 或 .\pmtrip.ps1 sh-api
# 容器内：
alembic history              # 看历史
alembic current              # 看当前版本
alembic upgrade head         # 升到最新
```

生成一个新的迁移（**注意**：生成后务必人工审阅，autogenerate 不是万能的）：

```bash
alembic revision --autogenerate -m "add xxx"
```

## 两条必须知道的约束

### 1. 数据库地址不在 alembic.ini 里

`alembic.ini` **刻意没有** `sqlalchemy.url`。地址由 `env.py` 从
`postmarktrip.config.settings.database_url` 读取，也就是 `.env` 里的
`DATABASE_URL`。改 `.env` 即可生效，不需要改任何版本控制内的文件。

### 2. pg_trgm 扩展必须先存在

`db/models.py` 里对 `name` / `title` / `note` / `inscription` 建了
`gin_trgm_ops` 索引（中文没有分词器，用 trigram 才搜得动）。这些索引要求
**pg_trgm 扩展已安装**。

扩展由 `infra/initdb/01-extensions.sql` 创建，而 PostgreSQL 官方镜像的
`/docker-entrypoint-initdb.d` **只在数据目录为空时执行一次**。

> ⚠️ 因此：如果 `data/db` 不是全新的（例如从旧备份恢复），而扩展又缺失，
> 迁移会在建索引时失败。补救：
> ```sql
> CREATE EXTENSION IF NOT EXISTS pg_trgm;
> ```
> `alembic upgrade head` 可以安全重跑 —— 失败的迁移不会写入版本表。

## 迁移不可逆

PostgreSQL 的 DDL 大部分可逆，但**数据丢失类操作不可逆**（删列、改类型收窄）。
升级前请务必 `make backup` / `.\pmtrip.ps1 backup`。
