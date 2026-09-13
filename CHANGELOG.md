# 更新日志

本文件记录「听间 · 本地播客书架」的变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] — 2026-09-12

第一个独立发布版本。播客系统从桌面 `30min` 学习系统的内部模块中抽出，整理成可以独立运行、独立发布的仓库。

### 新增

- **订阅自动更新**（`app/podcast_feeds.py`）
  - 后台调度器按设定间隔（5 分钟 – 24 小时，默认 30 分钟）轮询所有订阅源。
  - 按单集 ID 求差集，只报新增；首次巡检把现有目录作为基线，不会把历史全报成「新」。
  - 单个订阅源失败只记录错误，不影响其它源；状态落在 `data/index/feed-state.json`。
  - 新增接口：`GET /api/updates`、`GET|POST /api/subscription-settings`、`POST /api/updates/refresh`、`POST /api/updates/seen`。
- **AI 自主发现**（`app/podcast_discover.py`）
  - 用免费的 Apple Podcasts 检索接口按兴趣找候选，多关键词合并去重。
  - 配置了模型就由模型挑选并给理由；没配置则用透明的本地规则排序。
  - 自动关注：写订阅登记表、同步 RSS 目录、落盘封面，复用既有归档代码路径。
  - 取消关注把节目文件夹移动到 `data/archive/`，不删除任何文件。
  - 新增接口：`GET /api/discover/interests`、`GET|POST /api/discover/settings`、`GET /api/discover/search`、`GET /api/discover/health`、`POST /api/discover/recommend`、`POST /api/discover/auto`、`POST /api/discover/subscribe`、`POST /api/discover/unfollow`。
- **自带密钥的模型客户端**（`app/podcast_llm.py`）
  - 支持 DeepSeek 与任意 OpenAI 兼容接口。
  - 密钥来源优先级：环境变量 → 本机加密文件；**仓库中不含任何密钥**。
  - 失败有明确的中文提示（密钥无效、余额不足、限流、响应截断）。
- **加法式界面层**（`web/extras.js`、`web/extras.css`）
  - 侧栏新增「更新与发现」入口与未读数角标，顶栏新增「新节目」入口。
  - 面板、样式、事件监听全部自建，原有 DOM / CSS / JS 未被改写。
  - `index.html` 只增加了一行 `<script src="/extras.js" defer>`。
- **路径统一**（`app/paths.py`）：所有目录与文件位置收敛到一处，并支持用环境变量指向本地的转写模型与解释器。

### 变更

- 系统从「寄生在更大的学习工作区里」改为独立仓库，路径不再依赖上层目录结构。
- `podcast_server.py` 的启动逻辑抽成 `serve(port)`，供 `run.py` 复用。
- `podcast_translate.py` 的配置路径改为在调用时解析，便于测试时替换。

### 移除

- 不再打包任何个人数据：音频、字幕、学习数据库、API 密钥全部排除并写入 `.gitignore`。

### 保留

- 原有全部功能与前端表现不变：书架 / 专注收听、目录更新、下载校验、片段标记、笔记、字幕导入导出、生词本、翻译设置。
- 原有 31 个测试全部保留并通过。

### 测试

- 测试总数 31 → 55，全部离线，不依赖网络、GPU 或密钥。
- 新增 `tests/test_podcast_feeds.py`：新单集比对、首次基线、巡检容错、设置校验。
- 新增 `tests/test_podcast_discover.py`：检索归一化、排序权重、自动关注、取消关注归档、密钥不落盘、模型接口错误处理。
