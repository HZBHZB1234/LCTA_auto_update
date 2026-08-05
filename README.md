# LCTA Auto Update

LCTA Auto Update 是 `LCTA-Limbus-company-transfer-auto` 翻译模块的 GitHub Actions 封装。项目在官方生肉更新后自动下载 JP、KR、EN 文本，结合都市零协会最新正式 Release 中的熟肉，补全翻译并发布 `LLc-CN-LCTA` 汉化包。

## 更新流程

1. voidfissure 在源更新后发送 `repository_dispatch` 事件 `voidfissure_update`。
2. 工作流请求 `https://limbus-api.voidfissure.de/api/status`，读取 `latest_token.token`。
3. 使用 token 下载三个官方生肉 ZIP：
   - `localize_jp.zip`
   - `localize_kr.zip`
   - `localize_en.zip`
4. 获取 `LocalizeLimbusCompany/LocalizeLimbusCompany` 最新正式 Release 的源码快照，以 `LLC_zh-CN` 作为熟肉输入。
5. 运行从 LCTA 工具箱同步的新版 `TranslationPipeline`。
6. 生成 ZIP/7Z、运行摘要和 Release 说明，并发布或覆盖对应 Release。

工作流不再包含定时轮询。仍可通过 GitHub Actions 页面手动执行；手动执行遇到相同 raw token 时会重建并覆盖原 Release。

## Release 版本

tag 完全保留旧版日期加次数规则：

- 格式为 `YYYYMMDDNN`，日期使用北京时间。
- 当天首次发布使用 `01`。
- 当天后续发布在上一个正式 Release tag 的序号上加一。
- 跨日重新从 `01` 开始。
- 手动重建相同 raw token 时复用原 tag，不产生新版本。

`LLc-CN-LCTA/Info/version.json` 会记录版本、raw token、生肉创建时间、熟肉 Release 和生成时间。相同 token 的自动触发通过 Release 说明中的隐藏元数据去重。

## 配置

主要配置位于 `src/config.yaml`：

- `sources`：status API、生肉下载模板、语言和熟肉仓库。
- `network`：超时、重试和 GitHub token 环境变量名。
- `translation`：翻译器、模型、API key 环境变量、并发和提示格式。
- `features`：翻译管线各功能、幂等检查、回退、调试和 dump 开关。
- `publishing`：ZIP、7Z、输出目录、资产前缀和诊断 artifact 开关。

敏感值必须通过环境变量提供，默认使用：

- `DEEPSEEK`：翻译 API key。
- `LCTA_FETCHER`：可选的 GitHub API token；未配置时使用 Actions 的 `GITHUB_TOKEN`。

## Webhook 对接

外部服务需要调用 GitHub repository dispatch API：

```http
POST /repos/HZBHZB1234/LCTA_auto_update/dispatches
Authorization: Bearer <GitHub token>
Accept: application/vnd.github+json
Content-Type: application/json

{"event_type":"voidfissure_update"}
```

dispatch payload 中不需要携带 raw token。脚本始终重新请求 status API，并以其响应作为权威版本。

## 自托管状态服务

仓库自带常驻状态服务脚本，可替代 voidfissure 的状态 API 与 dispatch 触发，实现完全自托管：

```powershell
# 1. 复制默认配置并填写 github.token（config.yaml 已加入 .gitignore，不会提交）
copy tools\default_config.yaml tools\config.yaml

# 2. 启动（默认读取 tools\config.yaml；可用环境变量 LCTA_STATUS_CONFIG 指定其他路径）
python tools\status_server.py
```

服务会按 `schedule` 配置的更新窗口运行（默认每周四北京时间 10:00–13:00，每 15 分钟遍历一次）：每次遍历先执行 steamcmd 更新游戏，再扫描 `resources.assets`（路径在 `asset` 配置项，为空则按 `steamcmd.install_dir` → 默认 Steam 路径解析）提取最新 CDN token，并通过 `GET /api/status` 提供与 voidfissure 相同格式的响应；发现新 token 且文件稳定后自动调用 `repository_dispatch` 触发工作流，已触发过的 token 记录在 `state` 文件，重启不会重复触发。窗口外不运行，但可通过 `POST /api/check` 手动触发一次完整遍历（steamcmd + 扫描 + dispatch），返回 202。

主要配置项：

- `asset`：resources.assets 路径，为空按 `steamcmd.install_dir` → 默认 Steam 安装路径解析。
- `server`：监听地址与端口。
- `polling`：新 token 等待 mtime 稳定的秒数（避免 steamcmd 半写入时误触发）。
- `schedule`：更新窗口——`update_dow`（0=周一，默认 3=周四）、`start_hour`/`end_hour`（北京时间，默认 10–13）、`interval`（窗口内遍历间隔，默认 900 秒）；`enabled: false` 则始终按 `interval` 遍历。
- `steamcmd`：`path`（可执行文件路径，为空则不执行）、`app_id`（默认 1973530）、`install_dir`（为空用 steamcmd 默认安装位置，否则 `+force_install_dir`）、`login`（默认 anonymous）、`timeout`。
- `github`：目标仓库、event_type 与 token（token 只填写在本地 config.yaml）。
- `state`：已 dispatch 的 token 记录文件。
- `dispatch`：发现新 token 时是否自动调用 repository_dispatch；为 true 而 token 为空时启动报错。
- `verify`：提取到新 token 时是否在线校验，并在响应中加入 `hash` 字段。

服务就绪后把 `src/config.yaml` 的 `sources.status_url` 改为 `http://<host>:<port>/api/status` 即可让工作流改用本服务。

## 本地验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
python src/main.py
```

直接运行 `src/main.py` 会访问真实数据源并调用翻译 API。仅检查配置和模块时应运行测试，不要执行完整更新。

## 上游同步

`src/translateFunc` 保持与 `LCTA-Limbus-company-transfer-auto/translateFunc` 相同的目录和实现，自动更新适配全部位于 `src/auto_update`。后续更新翻译模块时应优先整体同步该目录，并运行 `tests/upstream` 中移植的上游测试，避免在翻译核心内部加入项目专用逻辑。
