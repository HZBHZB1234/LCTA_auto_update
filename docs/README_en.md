# LCTA Auto Localization Release Tool

LCTA (Localize-Compile-Translate-Automate) is an automated localization and release script based on GitHub Actions, designed to automatically fetch, translate, and release localized files, helping automate the localization workflow.  

[Legacy README](https://github.com/HZBHZB1234/LCTA_auto_update/blob/main/docs/README_origin.md)

This doc was translated by Deepseek. There might be some mistakes.

## ✨ Features

- 🔄 Automatically syncs the latest localization resources and source text from [Urban Zero Association Localization Group](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany)
- 🛠️ Automatically generates localized files with version management and release support
- 🚀 Fully automated workflow based on GitHub Actions, requiring no manual intervention
- 📦 Automatically publishes to Releases, providing a stable download channel

## 📥 Download Localized Files

You can directly download the latest version of the localized files from the [**Latest Release**](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/releases/latest) page.

## 🛠️ Usage

This project runs completely automatically and requires no manual configuration or script execution from the user. GitHub Actions will trigger the workflow every hour to check for new updates. If updates exist, new localized files are generated and published to the Release.

## 🌟 Related Projects

- [Urban Zero Association Localization Group](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany): Localization resources and source text for Urban Zero Association.  
- [LCTA Toolbox](https://github.com/HZBHZB1234/LCTA-Limbus-company-transfer-auto): A Limbus Company toolbox focused on translation. Includes a launcher, localization package management, auto-updates for localization packages, directory migration, cache data cleaning, and more. Can automatically assess and perform localization updates (including this project).

## 📄 License Information

- The source code and scripts of this project follow the **[MIT License](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/blob/main/LICENSE)**.
- All localized files released through this tool originate from the [Urban Zero Association Localization Group](https://github.com/LocalizeLimbusCompany/LocalizeLimbusCompany) and are distributed under the **[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans)** license.

## 🌍 Support for More Languages (Secondary Development)

LCTA is designed to support automated translation workflows for multiple languages. Currently, automation for Chinese localization is prioritized, but the framework itself is extensible.

If you wish to create an automated translation process for your language, you are welcome to:

- 📌 Submit an [Issue](https://github.com/LocalizeLimbusCompany/LCTA_auto_update/issues) describing your needs
- 🔧 Submit a Pull Request to implement support for the corresponding language
- 💬 Contact the author to discuss collaboration possibilities  

For specific implementations in other languages, you can refer in part to the localization implementation in the [LCTA Toolbox](https://github.com/HZBHZB1234/LCTA-Limbus-company-transfer-auto).

## 📁 Project Structure

```
LCTA_auto_update/
├── .github/workflows/   # GitHub Actions automation workflows
├── docs/                # Documentation
├── src/                 # Project script code
├── .gitignore           # Git ignore file
├── requirements.txt     # Project dependencies
└── README.md            # Project documentation
```