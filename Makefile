# =============================================================================
# postmarkTrip · 运维入口（Linux / macOS / WSL）
#
# Windows 上通常没有 make —— 请用等价的 .\pmtrip.ps1。
#
#   make            （不带目标 = help）
#   make doctor
#   make up
#
# 所有目标都只是 docker compose 的薄封装。
# **宿主机不需要装 Python / Node / Go** —— 全部构建在容器里完成。
#
# ⚠️ 存储：数据库与 Redis 在**命名卷**里（postmarktrip-db / postmarktrip-redis），
#    扫描件在宿主目录 ./data/assets。想知道卷的真实路径：
#        docker volume inspect postmarktrip-db
#    （make doctor 也会打印）
# =============================================================================

SHELL := /bin/sh

COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
ENV_FILE    := .env

# 从 .env 取一个值，只用于提示信息。
# 刻意不 source 整个 .env：里面可能有中文注释或特殊字符，source 会把 shell 带崩。
getenv = $(shell grep -E '^$(1)=' $(ENV_FILE) 2>/dev/null | tail -1 | cut -d= -f2-)

APP_PORT := $(call getenv,APP_PORT)
ifeq ($(APP_PORT),)
APP_PORT := 9999
endif

WEB_DEV_PORT := $(call getenv,WEB_DEV_PORT)
ifeq ($(WEB_DEV_PORT),)
WEB_DEV_PORT := 5173
endif

BACKUP_KEEP_DAYS := $(call getenv,BACKUP_KEEP_DAYS)
ifeq ($(BACKUP_KEEP_DAYS),)
BACKUP_KEEP_DAYS := 30
endif

.DEFAULT_GOAL := help

.PHONY: help guard-env guard-frontend up down restart logs ps sh-api sh-db backup restore upgrade \
        doctor gen-contract test-contract dev build pull clean

# --------------------------------------------------------------------------- #
#  帮助
# --------------------------------------------------------------------------- #
help: ## 显示这份帮助
	@printf 'postmarkTrip —— 跑戳日志 · 运维入口\n\n'
	@printf '用法: make <目标>\n\n目标:\n'
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z][a-zA-Z0-9_-]*:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf '\nWindows 上请用 .\\pmtrip.ps1 <子命令>，两者等价。\n'
	@printf '首次使用：直接 make up —— 会自动生成 .env 并打印管理员口令。\n'

# --------------------------------------------------------------------------- #
#  前置检查
# --------------------------------------------------------------------------- #
guard-env:
	@if [ ! -f $(ENV_FILE) ]; then sh scripts/init-env.sh; fi
	@if [ ! -f $(ENV_FILE) ]; then \
		printf '\n错误：%s 生成失败\n\n' "$(ENV_FILE)"; \
		exit 1; \
	fi

# gateway 的默认镜像是 apps/web/Dockerfile（前端负责人维护）。
# 它不存在时 docker compose 只会丢一句 "failed to read dockerfile"，很难定位，
# 所以这里提前拦下来并给出两条现成的替代路径。
guard-frontend:
	@if [ ! -f apps/web/Dockerfile ]; then \
		printf '\n提示：apps/web/Dockerfile 尚不存在（前端镜像由前端负责人维护）。\n\n'; \
		printf '  默认拓扑里 gateway 用它构建，所以现在无法直接 up。两个替代方案：\n\n'; \
		printf '    1) 先用「分离式网关」把后端跑起来（不依赖前端产物）：\n'; \
		printf '       docker compose -f docker-compose.yml -f infra/compose/gateway-standalone.yml up -d\n\n'; \
		printf '    2) 开发模式（用 Vite dev server 代替静态产物，访问 5173）：\n'; \
		printf '       make dev\n\n'; \
		exit 1; \
	fi

# --------------------------------------------------------------------------- #
#  日常
# --------------------------------------------------------------------------- #
up: guard-env guard-frontend ## 启动全部服务（后台运行）
	$(COMPOSE) up -d
	@printf '\n启动完成。\n'
	@printf '  访问：http://localhost:%s\n' "$(APP_PORT)"
	@printf '  自检：make doctor\n'
	@printf '  日志：make logs\n'

down: guard-env ## 停止并移除容器（**保留数据**：卷与 ./data 都不动）
	$(COMPOSE) down --remove-orphans
	@printf '\n容器已移除。数据保留在命名卷与 ./data 目录中。\n'

restart: guard-env ## 重启全部服务（不重建镜像）
	$(COMPOSE) restart

logs: guard-env ## 跟随全部服务日志（最近 100 行）
	$(COMPOSE) logs -f --tail=100

ps: guard-env ## 查看容器状态与健康
	$(COMPOSE) ps

sh-api: guard-env ## 进入 api 容器的 shell
	$(COMPOSE) exec api sh

sh-db: guard-env ## 进入数据库交互式 psql
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

# --------------------------------------------------------------------------- #
#  备份 / 恢复
# --------------------------------------------------------------------------- #
backup: guard-env ## 备份：pg_dump 数据库 + 生成资产清单（不改动 .env）
	@mkdir -p backups/db backups/assets-manifest
	@ts=$$(date +%Y%m%d_%H%M%S); \
	 out="backups/db/$${ts}.dump"; \
	 printf '转储数据库（pg_dump）-> %s\n' "$$out"; \
	 if ! $(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --format=custom' > "$$out"; then \
	   printf '✗ 数据库转储失败（容器没起来？数据库没就绪？）\n'; \
	   rm -f "$$out"; \
	   exit 1; \
	 fi; \
	 if ! $(COMPOSE) exec -T db pg_restore --list < "$$out" > /dev/null 2>&1; then \
	   printf '✗ 转储文件无法解析（pg_restore --list 失败）—— 已删除，不要依赖它\n'; \
	   rm -f "$$out"; \
	   exit 1; \
	 fi; \
	 printf '✓ 已写入并校验（%s 字节）\n' "$$(wc -c < "$$out" | tr -d ' ')"; \
	 n=$$(find data/assets -type f 2>/dev/null | wc -l | tr -d ' '); \
	 man="backups/assets-manifest/$${ts}.txt"; \
	 { printf '# postmarkTrip 资产清单 %s\n' "$${ts}"; \
	   printf '# 恢复后请核对：文件数量与路径应与实际一致\n'; \
	   find data/assets -type f 2>/dev/null | sort; } > "$$man"; \
	 printf '✓ 资产清单 %s（%s 个文件）\n' "$$man" "$$n"; \
	 keep="$(BACKUP_KEEP_DAYS)"; \
	 if [ "$$keep" -gt 0 ] 2>/dev/null; then \
	   newest=$$(ls -1t backups/db/*.dump 2>/dev/null | head -1); \
	   find backups/db -name '*.dump' -type f -mtime +"$$keep" 2>/dev/null | while read -r f; do \
	     if [ "$$f" = "$$newest" ]; then continue; fi; \
	     rm -f "$$f"; printf '  已清理超过 %s 天的转储: %s\n' "$$keep" "$$f"; \
	   done; \
	 fi; \
	 printf '\n⚠️  .env 不在本次备份内（含密钥，刻意不写进 backups/）。\n'; \
	 printf '    请单独把它抄一份到安全的地方 —— 丢了 SECRET_KEY，所有人都要重新登录。\n'; \
	 printf '    数据库本身在命名卷 postmarktrip-db 里；备份走 pg_dump，**不要**直接拷贝卷目录。\n'

restore: guard-env ## 从转储恢复数据库（用法：make restore FILE=backups/db/xxx.dump）
	@if [ -z "$(FILE)" ]; then \
		printf '\n错误：必须指定要恢复的文件。\n\n'; \
		printf '  用法： make restore FILE=backups/db/20261008_030000.dump\n\n'; \
		printf '  可用的转储（最新在前）：\n'; \
		if ls -1t backups/db/*.dump >/dev/null 2>&1; then \
			ls -1t backups/db/*.dump | head -10 | sed 's/^/    /'; \
		else \
			printf '    （还没有任何转储，先跑 make backup）\n'; \
		fi; \
		printf '\n'; \
		exit 1; \
	fi
	@if [ ! -f "$(FILE)" ]; then printf '\n错误：找不到文件 %s\n\n' "$(FILE)"; exit 1; fi
	@printf '\n⚠️  这会**覆盖当前数据库的全部内容**。\n'; \
	 printf '    转储文件：%s\n' "$(FILE)"; \
	 printf '    扫描件目录 ./data/assets 不会被改动 —— 恢复后用资产清单核对。\n\n'; \
	 printf '    确认请输入 y：'; \
	 read -r ans; \
	 if [ "$$ans" != "y" ]; then printf '\n已取消。\n'; exit 1; fi
	@printf '\n① 恢复前的安全转储\n'
	@ts=$$(date +%Y%m%d_%H%M%S); \
	 safety="backups/db/pre-restore_$${ts}.dump"; \
	 mkdir -p backups/db; \
	 if $(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --format=custom' > "$$safety" \
	    && $(COMPOSE) exec -T db pg_restore --list < "$$safety" > /dev/null 2>&1; then \
	   printf '✓ %s（%s 字节）\n' "$$safety" "$$(wc -c < "$$safety" | tr -d ' ')"; \
	 else \
	   rm -f "$$safety"; \
	   printf '\n警告：安全转储失败 —— 数据库可能没起来或已经不可用。\n'; \
	   printf '      继续恢复就**没有退路**了：pg_restore --clean 会先删表，\n'; \
	   printf '      万一这份转储也有问题，恢复前的数据将无法找回。\n\n'; \
	   printf '      确认仍要继续请输入 y：'; \
	   read -r ans2; \
	   if [ "$$ans2" != "y" ]; then printf '\n已取消。\n'; exit 1; fi; \
	 fi
	@printf '\n② 停掉 api 与 worker（恢复期间不要写库）\n'
	@$(COMPOSE) stop api worker || true
	@printf '③ 恢复\n'
	$(COMPOSE) exec -T db sh -c 'pg_restore -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --clean --if-exists --no-owner' < $(FILE)
	@printf '④ 重新启动\n'
	@$(COMPOSE) start api worker
	@printf '\n完成。恢复前的状态已存为 backups/db/pre-restore_*.dump\n'
	@printf '请执行 make doctor 确认。\n'

# --------------------------------------------------------------------------- #
#  升级 / 自检
# --------------------------------------------------------------------------- #
upgrade: guard-env guard-frontend ## 升级：备份 → 重建镜像 → 重启 → 重载网关
	@printf '\n① 升级前强制备份\n'
	@$(MAKE) --no-print-directory backup
	@printf '\n② 重建镜像（--pull 会顺带拉取更新的基础镜像）\n'
	$(COMPOSE) build --pull
	@printf '\n③ 重启（数据库迁移由 api 容器启动时自动执行）\n'
	$(COMPOSE) up -d
	@printf '\n④ 重载 gateway\n'
	@printf '   （api 容器被重建后 IP 会变，而 nginx 在启动时只解析一次上游名）\n'
	@$(COMPOSE) exec -T gateway nginx -s reload || printf '   gateway 未运行或尚未就绪，跳过重载\n'
	@printf '\n⑤ 看迁移结果：make logs    然后：make doctor\n'

doctor: ## 自检：Docker、.env、示例值、存储、容器、HTTP 端点、端口、备份
	@sh infra/scripts/doctor.sh

# --------------------------------------------------------------------------- #
#  契约层（v1.0 起逐渐有实质内容）
# --------------------------------------------------------------------------- #
gen-contract: guard-env ## 校验 contract/*.schema.json（类型生成 v1.0 接入）
	@sh infra/scripts/check-contract.sh

test-contract: ## 契约测试（黄金图像回归）—— v1.0 接入
	@sh infra/scripts/test-contract.sh

# --------------------------------------------------------------------------- #
#  构建
# --------------------------------------------------------------------------- #
dev: guard-env ## 开发模式：后端热重载（前端 HMR 见提示）
	$(COMPOSE_DEV) up -d
	@printf '\n开发模式已启动（后端热重载）。\n'
	@printf '  应用（经网关）：http://localhost:%s\n' "$(APP_PORT)"
	@printf '  后端改代码：容器内 uvicorn 自动重载，无需重启\n'
	@printf '\n  前端 HMR 请在**宿主机**另开一个终端：\n'
	@printf '      cd apps/web && npm install && npm run dev   ->  http://localhost:%s\n' "$(WEB_DEV_PORT)"
	@printf '  （代理目标默认跟随 .env 的 APP_PORT=%s；若 vite 进了容器就设 VITE_PROXY_TARGET=http://gateway）\n' "$(APP_PORT)"

build: guard-env guard-frontend ## 构建/重建镜像
	$(COMPOSE) build

pull: guard-env ## 拉取基础镜像（应用镜像由 build 生成）
	$(COMPOSE) pull --ignore-pull-failures || true

clean: guard-env ## 停止并移除容器与网络（**绝不删除数据卷**）
	@printf '停止并移除容器与网络 …\n'
	$(COMPOSE) down --remove-orphans
	@printf '\n已清理。以下内容**未被触碰**：\n'
	@printf '  命名卷 postmarktrip-db / postmarktrip-redis（数据库与队列）\n'
	@printf '  ./data/assets  ./inbox  ./public-data  ./models  ./backups\n'
	@printf '\n⚠️  绝不要用 `docker compose down -v` —— 那会删掉数据库卷。\n'
	@printf '    真要清空重建，请先 make backup，再手工 docker volume rm。\n'
