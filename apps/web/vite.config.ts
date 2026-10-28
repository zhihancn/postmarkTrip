import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * dev server 把 /api 代理到后端（docker compose 暴露的 9999 端口）。
 * 生产由 nginx 反代到 http://api:8000，因此前端全部使用相对路径 /api/...
 * ——同一份代码在两种环境下都不用改 baseURL。
 *
 * `VITE_PROXY_TARGET` 允许覆盖代理目标。为什么需要它：`localhost:9999` 只在
 * **vite 跑在宿主机上**时成立。一旦 vite 进了容器，容器内的 localhost 指向容器
 * 自己，代理必然 ECONNREFUSED。此时把该变量设为服务名即可（如 `http://gateway`）。
 *
 * 用 `loadEnv` 而不是 `process.env`：后者需要 @types/node，而本项目刻意不引入它
 * （vite 配置在 Node 里执行，但不需要把 Node 类型带进前端工程）。
 */
export default defineConfig(({ mode }) => {
  // 端口从**仓库根目录**的 .env 读 —— 整个项目只有一个 .env（.env.example 是模板），
  // 没有理由让前端再维护一份。已实测：'../../' 从 apps/web 解析到仓库根。
  //
  // 这样做的意义：.env 里的 APP_PORT / WEB_DEV_PORT 变成**真的生效**，
  // 而不是只有 pmtrip.ps1 / Makefile 在打印提示时用一下。原先 port 写死 5173，
  // 于是改了 .env 里的 WEB_DEV_PORT 什么也不会发生 —— 典型的"配置名不副实"。
  const repoEnv = loadEnv(mode, '../../', '');

  // VITE_* 仍按 Vite 惯例从 apps/web 下读（便于用 apps/web/.env.local 做本地覆盖）
  const webEnv = loadEnv(mode, '.', 'VITE_');

  const devPort = Number(repoEnv.WEB_DEV_PORT) || 5173;
  const apiPort = Number(repoEnv.APP_PORT) || 9999;

  return {
    plugins: [react()],
    server: {
      port: devPort,
      // strictPort：端口被占用时**明确报错**，而不是静默换一个相邻端口。
      // 否则 `pmtrip.ps1 dev` / `make dev` 打印的 URL 会与实际监听的端口不一致，
      // 而"提示在撒谎"是最费时间的一类问题。
      strictPort: true,
      host: true,
      proxy: {
        '/api': {
          // VITE_PROXY_TARGET 可覆盖：vite 一旦进了容器，容器内的 localhost
          // 指向容器自己，代理必然 ECONNREFUSED，此时要改成服务名。
          // 默认跟随 .env 的 APP_PORT，所以只改端口不需要动这里。
          target: webEnv.VITE_PROXY_TARGET || `http://localhost:${apiPort}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
  };
});
