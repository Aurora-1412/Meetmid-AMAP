# 生产静态资源路由修复设计

## 问题

本地入口 `app_v2.py` 使用 Flask。Flask 默认把项目的 `static/` 目录暴露在
`/static/*`，所以 `static/index.html` 中的三个脚本引用可以正常加载。

生产入口 `app/main.py` 使用 FastAPI，并把 `static/` 目录直接挂载在 URL 根路径
`/`。因此浏览器请求 `/static/midpoint-state.js` 时，生产后端会错误查找
`static/static/midpoint-state.js`。线上首先报出 `MidpointState is not defined`，
主脚本在调用地图初始化前中断。

## 修复

在生产 FastAPI 应用中增加独立的 `/static` 挂载，并确保它注册在兜底根路径
`/` 之前。保留现有根路径挂载继续提供首页，因此：

- 本地 Flask 入口继续使用 `/static/*`；
- 生产 FastAPI 入口也能使用相同的 `/static/*`；
- 前端文件和业务逻辑无需分叉或内联。

## 测试

先添加会在当前代码上失败的回归测试，验证：

1. 生产入口声明 `/static` 静态资源挂载；
2. `/static` 挂载位于根路径 `/` 挂载之前；
3. 页面引用的三个 JavaScript 文件都存在于发布目录。

修复后运行全部 Python 与 Node 测试。随后重新生成包含 `.env` 的生产 ZIP，
从 ZIP 解压后实际启动 `app.main:app`，验证首页、三个 `/static/*.js` 请求、
`/api/config` 和浏览器地图初始化。

## 发布范围

代码修改仅涉及生产静态资源路由及其回归测试。发布包继续输出到
`dist/midpoint-recommender.zip`，包含当前 `.env`，排除 `.env.example`、
测试、虚拟环境、缓存和 Git 数据。
