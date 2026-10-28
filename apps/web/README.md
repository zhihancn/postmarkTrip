# apps/web —— 跑戳日志 Web 前端

Vite + React 19 + TypeScript（strict）。样式是手写 CSS（`src/styles/app.css`），不引 UI 框架。
路由 `react-router-dom` v7，数据 `@tanstack/react-query` v5 + `axios`，日期 `dayjs`。

## 开发

```bash
npm install
npm run dev        # http://localhost:5173，/api 代理到 http://localhost:9999
```

也可以把整个前端放进 compose 里跑（`apps/web` 就是那个 gateway 容器）。
开发时若后端跑在容器里，用 `http://localhost:9999` 作为代理目标即可。

## 构建与检查

```bash
npm run typecheck  # tsc --noEmit
npm run build      # vite build → dist/
npm run build:check # 先 typecheck 再 build
npm run test:pure  # 纯函数测试（零新依赖，见下）
```

### 纯函数测试（`test:pure`）

`src/features/cards/draft.ts` 与 `src/features/recognize/recognize.ts` 是全项目的
"翻译层"（界面输入 ⇄ 后端契约），写错了**不报错**，只会静默发出错的载荷。
它们被 tsc 单独编成自包含 JS，再由 Node 内置的 `node:test` 断言 ——
**不引 vitest**：本项目的开发环境网络受限，装不上就验证不了，
而把没验证过的测试脚手架写进 CI 比没有更糟。

`tools/check_web_static.py` 是一个**兜底**检查（括号平衡 + "命名的 import 必须
真的有对应 export"）。它**不是类型检查**，只在拿不到 tsc 的环境里用：

```bash
npm run check:static
```

### 不装 node 也能跑这三道检查

WSL 里目前**没有 node**（nvm / fnm / apt 都没有，只有 `uv`）。要么自己装，
要么用仓库根的 `_web_check.sh` —— 它在一次性 Node 容器里跑
`typecheck` / `test:pure` / `build`：

```bash
bash <repo>/_web_check.sh
```

三条设计（都是为了不违反本项目的"不许自装永久依赖"）：

1. 仓库以**只读**方式挂进容器（`-v apps/web:/w:ro`）
2. 源码 `cp` 进容器自己的文件系统 —— 于是 `node_modules` / `.test-build` / `dist`
   全在容器里，**退出即销毁**，仓库目录里不会出现它们
3. 容器以 `--user $(id -u):$(id -g)` 运行，不需要 sudo

⚠️ 写这类脚本时的两个坑，**都踩过**：`docker run … sh -lc '<多行脚本>'` 用的是
Alpine 的 busybox `sh`，**不支持 `${PIPESTATUS[0]}`** —— 报 bad substitution
之后脚本会断，**看起来却像"通过了"**；而换成 `bash -lc` 也不行，因为镜像默认
PATH 里没有 `bash`，docker 会把 `bash` 当成脚本参数
（`Cannot find module '/tmp/bash'`）。**结论：把脚本写成文件再挂进去执行。**

### ⚠️ 为什么 `build` 不跑 `tsc`

`npm run build` 只做 `vite build`，**刻意不 gate 在类型检查上**。
理由：镜像构建不该被一个类型错误整个卡死 —— 类型检查放在 `typecheck` 脚本与 CI 里跑，
发布镜像时只保证"能构建"。想两个都跑用 `npm run build:check`。

### ⚠️ 依赖锁文件

仓库里**目前没有 `package-lock.json`**（首次构建环境无法生成），所以 Dockerfile 用的是
`npm install` 而不是 `npm ci`。**第一次成功构建后，请把生成的 `package-lock.json` 提交，
并把 Dockerfile 里的 `npm install` 换成 `npm ci`**（可复现、更快）。

## 容器

默认走仓库根的 `docker-compose.yml`（`gateway` 服务）。**构建上下文是仓库根目录**，
不是 `apps/web`：

```bash
# 在仓库根目录执行
docker compose build gateway
```

多阶段：`node:22-alpine` 构建 → `nginx:alpine` 托管。

### ⚠️ 网关配置的归属（容易搞错）

| 拓扑 | 用的 nginx 配置 | 说明 |
| --- | --- | --- |
| **合并式（默认）** | **`infra/gateway/nginx.conf`** | 静态产物 + 网关同容器。`apps/web/Dockerfile` 会 COPY 它 |
| 分离式 | `infra/gateway/nginx.conf` | 由 `infra/gateway/Dockerfile` 构建纯网关，静态文件外部挂载 |
| 仅构建前端（可选） | `apps/web/nginx.conf` | 兜底。以 `apps/web` 为上下文单独构建时才会用到 |

`infra/gateway/nginx.conf` 是**唯一真源**：它在顶层定义了 WebSocket 的
`map $http_upgrade $connection_upgrade`，并提供 `/gateway-health` 端点
（`infra/scripts/doctor.sh` 用它区分"网关活着但后端挂了"与"整个容器没起来"）。
**要改网关行为，改那一份。**

`apps/web/nginx.conf` 是**参考配置**，只服务于"前端单独部署、网关由别处提供"
的场景（即 `infra/compose/gateway-standalone.yml` 那条路）。它同样把 `/api`
反代到 `http://api:8000`、有 SPA fallback 与 `/gateway-health`，但少了 WebSocket
`map` 与部分安全响应头。要用它构建前端镜像，需要把 `apps/web/Dockerfile` 里的
COPY 路径从 `apps/web/xxx` 改成 `xxx`、并把 nginx 配置那行指向 `nginx.conf`
（即 `context: apps/web`）。**日常部署不要走这条路。**

### 为什么镜像构建不跑 `nginx -t`

nginx 解析配置时会去解析 `proxy_pass http://api:8000` 里的主机名，而构建容器里
没有 `api` 这个 DNS 名，`nginx -t` 必然以 "host not found in upstream" 失败 ——
那是假阳性。语法校验放在运行时：`docker compose exec gateway nginx -t`。

## 目录

```
src/
├── main.tsx / App.tsx        入口、QueryClient、路由守卫
├── styles/app.css            全部样式（CSS 变量组织主题）
├── shared/
│   ├── api.ts                唯一的 API 客户端（组件里不要直接 import axios）
│   ├── types.ts              后端 schemas.py 的 TS 镜像
│   ├── vocab.ts              /api/vocab —— 前端不硬编码任何枚举
│   ├── format.ts             显示层格式化
│   ├── hooks.ts              防抖、点击外部
│   └── components/           通用组件（含三个增量补全选择器）
└── features/
    ├── auth/                 登录页、会话、改口令
    ├── cards/                列表、详情/编辑、上传、草稿模型
    ├── recognize/            扫描识别 → 确认 → 落库（`/recognize`）
    ├── dicts/                行政区划树、邮局、标签
    └── stats/                统计
```

### 两条硬约定

1. **枚举一律来自 `/api/vocab`**。戳种会持续扩充（没有官方完整分类），前端硬编码会导致
   "后端加了新取值，界面上选不到"。`labelOf()` 找不到时回落到原始 value，而不是显示空白。
2. **编辑走"草稿层"**（`features/cards/draft.ts`）：所有改动先落本地，一次保存时统一 diff
   提交（新增 / 更新 / 删除）。这样新建卡片时"卡片还不存在、邮戳没有 card_id"的问题
   自然解决，Ctrl+S 也才有意义。

### 识别确认页（`features/recognize/`）

三层分工，**落库只有一个入口**（spec A6 靠结构保证，不是靠自觉）：

| 步骤 | 端点 | 写库吗 |
| --- | --- | --- |
| 上传原始件 | `POST /api/assets` | 只写资产（L0，内容寻址去重） |
| 识别 | `POST /api/recognize?asset_id=…` | **不写**（后端有断言：该模块没有任何写操作） |
| 确认 | `POST /api/cards/confirm` | **唯一的写入点** |

界面按用户拍板的两条做：**默认折叠成摘要**（只有"**有值的可疑项**"自动展开 ——
空字段不算异常），**整页批量确认为主 + 「只确认这一张」为次**。

三条最容易改错的地方，都写在对应文件里：

* **颜色只按 `decision`，不按 `confidence`** —— 实测「福建南火」置信度 0.90 却是错的。
  判定由后端 `recognition.review.decide` 给出（它额外查了词表），**前端不许重算**。
* **`quad` / `circle` 都是工作尺度坐标**，叠层的 `viewBox` 就是 `work_size`，
  预览图必须是**等比缩放**的（`assetPreviewUrl` 走 `Image.thumbnail()`，是等比的）。
  第一版把 `verdict.circle`（卡局部坐标）画到整页上，7 张卡的圆全叠在左上角 ——
  那个错不报错，只是看起来"圆检测很乱"。
* **`machine_value` 必须用识别响应里的原值**，不是界面上改过的值。
  用错会把"人改的"也记成"机器读到的"，`correction` 这条长期标注资产就废了，而且不报错。

## 已知缺口（待后端补齐）

以下端点在写这份前端时尚未实现，前端已按契约接好，接口一上线即可用：

| 端点 | 用途 |
| --- | --- |
| `POST /api/assets`、`GET /api/assets/{id}/raw`、`/thumb` | **图片上传与显示**（缺了则上传和缩略图都不可用） |
| `GET /api/stats/overview` / `by-year` / `by-place` | 统计页 |
| `GET /api/export/cards.csv` | 导出 CSV |

另外两个契约上的小问题见交付说明：嵌套邮戳的 `tag_ids` 在 `POST /api/cards` 时会被后端丢弃；
缺少 `GET /api/post-offices/{id}` 导致无法按 id 反查邮局。
