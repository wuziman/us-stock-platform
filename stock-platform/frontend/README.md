# 量化交易平台前端 (Vite + React + TypeScript)

基于 React 18、TypeScript、Ant Design 5 与 ECharts 按需构建的 A 股与美股量化分析前端。

## 常用脚本

在 `stock-platform/frontend` 目录下可执行：

- `npm run dev`：启动 Vite 本地开发服务器（默认端口 `http://localhost:3000`，自动代理 `/api` 至 `http://localhost:8000`）
- `npm run build`：执行 `tsc && vite build` 类型检查与生产环境打包
- `npm run lint`：运行 ESLint 代码检查
- `npm run test`：运行 Vitest 单元测试
- `npm run preview`：预览生产构建产物

## 设计规范

UI 颜色 Token 与图表规范请查阅同目录下的 `DESIGN.md` 与 `src/theme/tokens.ts`。
