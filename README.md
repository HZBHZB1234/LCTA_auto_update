# LCTA 自动汉化发布工具

LCTA（Localize-Compile-Translate-Automate）是一个基于 GitHub Actions 的自动汉化发布脚本，用于自动获取、翻译并发布汉化文件，助力本地化工作流程自动化。  

[旧版README](https://github.com/HZBHZB1234/LCTA_auto_update/blob/main/docs/README_origin.md)  [Englisg ver](https://github.com/HZBHZB1234/LCTA_auto_update/blob/main/docs/README_en.md)

## ✨ 功能特点

- 🔄 自动从 [都市零协会汉化组](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany) 同步最新汉化资源与原文
- 🛠️ 自动生成汉化文件，支持版本管理与发布
- 🚀 基于 GitHub Actions 实现全流程自动化，无需手动干预
- 📦 自动发布至 Releases，提供稳定下载渠道

## 📥 获取汉化文件

您可以直接在 [**最新 Release**](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/releases/latest) 页面下载最新版本的汉化文件。

## 🛠️ 使用方式

本项目完全自动化运行，无需用户手动配置或执行脚本。每一小时，GitHub Actions 将自动触发流程，检查是否存在新更新。如有，生成新的汉化文件并发布至 Release。

## 🌟 相关项目

- [都市零协会汉化组](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany)：都市零协会的汉化资源与原文。  
- [LCTA工具箱](https://github.com/HZBHZB1234/LCTA-Limbus-company-transfer-auto)：以翻译为主的边狱公司工具箱。包含启动器，汉化包管理，汉化包自动更新，目录迁移，缓存数据清理等多种功能，可自动评估并进行汉化更新(包括此项目)

## 📄 许可证说明

- 本项目源代码及脚本遵循 **[MIT 协议](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/blob/main/LICENSE)**。
- 所有通过本工具发布的汉化文件，其内容源自 [都市零协会汉化组](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany)，遵循 **[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)** 协议进行分发。

## 🌍 支持更多语言（二次开发）

LCTA 设计上支持为多种语言构建自动翻译流程。目前优先实现了中文汉化的自动化，但框架本身具备扩展性。

如果您希望为您的语言创建自动翻译流程，欢迎：

- 📌 提交 [Issue](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/issues) 说明需求
- 🔧 提交 Pull Request 实现对应语言的支持
- 💬 与作者联系，讨论协作可能性  

在其他语言的具体实现中，可以部分参考 [LCTA工具箱](https://github.com/HZBHZB1234/LCTA-Limbus-company-transfer-auto) 的汉化部分实现。

## 📁 项目结构

```
LCTA_auto_update/
├── .github/workflows/   # GitHub Actions 自动化流程
├── docs/                # 文档
├── src/                 # 项目脚本代码
├── .gitignore           # Git 忽略文件
├── requirements.txt     # 项目依赖包
└── README.md            # 项目说明文档
```
