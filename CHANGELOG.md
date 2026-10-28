# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增
- **GitHub 构建镜像**：`.github/workflows/release.yml`，打 `v*` tag 自动构建并推送
  `ghcr.io/<owner>/postmarktrip-api`（含识别栈）与 `-gateway`；用户 `docker compose pull` 即可，无需本地 build
- `PMTRIP_API_REPO` / `PMTRIP_GATEWAY_REPO` / `PMTRIP_TAG`：镜像来源可配，留空仍是本地构建
- `COLNET_ENABLED`：以图搜票的显式开关（默认关）；`/api/health` 暴露 `stamp_identify.enabled`，
  未启用时「识别邮票」按钮置灰并说明原因
- **零配置启动**：`scripts/init-env.sh`（Linux/macOS）与 `pmtrip.ps1 up`（Windows）在缺少 `.env` 时自动生成随机 `SECRET_KEY`、数据库口令与管理员口令，并打印一次凭据
- **bootstrap 兜底**：`ADMIN_PASSWORD` 为空时自动生成口令并打印（此前直接退出，表现为「容器起来了但登录永远 401」）
- `infra/scripts/check-leaks.py` + 独立 CI job「泄漏扫描」：真实 API Key、带用户名的绝对路径、`.env` 是否曾进入历史
- `SECURITY.md`；README 新增「数据边界」一节
- `.env.example` 记录可选变量 `PMTRIP_PGDATA`

### 变更
- **数据库默认回到命名卷** `postmarktrip-db`；需要绑定挂载的用 `.env` 的 `PMTRIP_PGDATA`（此前 compose 里硬编码开发机路径，任何人 clone 后都起不来）
- `.gitignore` 忽略 `docker-compose.override.yml` / `docker-compose.a5.yml`（本机专用编排）
- 文档中的真实管理员口令与个人绝对路径统一替换为占位符（`<ADMIN_PASSWORD>` / `<you>` / `<repo>`）

### 修复
- 不再跟踪 `docker-compose.override.yml` / `docker-compose.a5.yml` —— 二者会被 compose **自动加载**，导致陌生人的 `up` 失败
- 移除 `docs/frontDemo/_check/` 叠加诊断图（含真实扫描件）

### 计划中
- **v0.1** — 手工录入与管理（零识别）。数据模型、CRUD、图片上传、筛选搜索、统计、CSV 导出、单用户登录。
- **v1.0** — 采集（投递目录 + 上传）、单卡切分、戳型判别（日戳/风景戳）、识别预填与校对队列、场景图契约、个人画廊基础。
- **v1.1** — 电子模板渲染与模板编辑器、邮戳卡绘制、画廊完整版（个人 + 公共戳库）、多卡混排、去污渍、盖戳评分、Windows 扫描 Agent、公共数据包。
- **v1.2** — 邮戳生成器（日戳参数化渲染 + 风景戳 AI 增强）、以图搜戳、服务端渲染器、在线同步。
- **v2.0** — 移动端（拍照上传，不做扫描）。

---

<!-- 版本发布时在下方追加，格式：

## [0.1.0] - YYYY-MM-DD

### 新增
- ...

### 变更
- ...

### 修复
- ...

-->
