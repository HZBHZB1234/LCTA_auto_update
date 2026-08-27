# 自动更新架构

## 边界

- `src/translateFunc`：从 LCTA 工具箱原样同步的翻译核心，不承载下载、版本或 GitHub Actions 逻辑。
- `src/auto_update`：本项目适配层，负责配置、网络、解压、幂等、版本、打包和运行编排。
- `src/main.py`：日志初始化和错误码入口。
- `.github/workflows/check.yml`：只负责准备 Python 环境、运行入口、发布 Release 和上传诊断文件。

## 数据流

`repository_dispatch` → status API → 官方 JP/KR/EN ZIP → 最新熟肉 Release 资产（cooked LLC ZIP）→ `TranslationPipeline` → `LLc-CN-LCTA` → ZIP/7Z → GitHub Release。

## 失败策略

- 网络、配置、解压、输入目录、完全无输出和打包错误会终止发布。
- 单文件翻译错误和 fallback 会记录到 `run-summary.json` 与 Release 说明，但只要存在有效输出仍继续发布。
- 自动重复 token 正常退出；手动重复 token 重建并覆盖原 tag 的 Release。
