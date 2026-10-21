# 来源与覆盖矩阵

[English](SOURCES.md) · **简体中文**

intelbrew 获取二进制的全部途径、各自的保证，以及是否真的跑过。
“已实测”= 在 Intel Mac（macOS 13.7 Ventura）上端到端跑通，不是“应该能用”。

## 来源表

| 来源（`Candidate.source`） | 用于 | 契约 | 完整性 | 来源证明 | 状态 |
|---|---|---|---|---|---|
| `registry` | 有内置 recipe 的任何东西 | 一致 | sha256（recipe 内） | 可选 GPG/codesign | 已实测（`doctor`） |
| `brew-bottle` | 该配方有本平台或更老 macOS 的 bottle | 一致 | Homebrew bottle 摘要 | Homebrew / ghcr TLS | 已实现；速度取决于到 ghcr.io 的线路 |
| `node-dist` | `node`、`node@18/20/22/24` | 一致 | 来自 `SHASUMS256.txt` 的 sha256 | 已校验 Apple 签名 `HX7739G8FX` | **已实测** |
| `github-release`（裸资产） | 直接发无扩展名二进制的工具（jq、部分 Go CLI） | 一致 | 来自 `asset.digest` 的 sha256 | 无 | **已实测**（`jq` 1.8.2） |
| `github-release` | 单文件 CLI（uv、ripgrep、fd、jq、gh…） | 一致 | 来自 `asset.digest` 的 sha256 | 无（仅摘要） | **已实测** |
| `adoptium-jdk` | `openjdk`、`openjdk@21` | 一致 | Adoptium API 的 sha256 | Temurin 构建 | 已实现 |
| `pypi-zipapp` / `pypi-wheel` | Python CLI（poetry、pre-commit…） | CLI 一致 | PyPI JSON 的 sha256 | 无 | 已实现 |
| `conda-forge` | ffmpeg、imagemagick、redis、sqlite… | 运行时不同 | repodata 的 sha256 | conda-forge | 已实现，部分验证 |
| `npm-global` | 仅白名单内的 JS 工具 | npm CLI | registry 的 sha1 | 无 | **已实测** |

## 载荷格式（压缩包与裸二进制）

| 格式 | 支持 | 说明 |
|---|---|---|
| `.tar.gz` / `.tgz` | ✅ | 通用 |
| `.tar.xz` / `.txz` | ✅ | 标准库 `lzma` |
| `.tar.bz2` | ✅ | 标准库 |
| `.tar.zst` | ✅ | Python ≥ 3.14 原生，否则需 `zstandard` 包 |
| **裸二进制**（无压缩包） | ✅ | 例如 `jq-macos-amd64`；recipe 里写 `"fmt": "raw"` 和命令名 |
| `.zip`（含 zip64） | ✅ | 依据 unix 权限位还原符号链接 |
| `.pkg` / `.dmg` | ❌ | 是安装器不是压缩包，明确拒绝 |
| `.7z` / `.rar` | ❌ | 明确拒绝 |

## 平台矩阵

| 主机 | `ibrew install` 上游路线 | `brew` 垫片 | 托管 `node` | 说明 |
|---|---|---|---|---|
| Intel macOS 12–15 | ✅ | ✅ | ✅ | 主要目标 |
| Intel macOS 10.16–11 | ✅ | ⚠️ 仅老 bottle | ⚠️ 最高 Node 18 系 | 上游可能已停发旧构建 |
| Apple Silicon macOS 14–26 | ✅（arm64 资产） | ✅（通常没必要，bottle 齐全） | ✅ | 可用，但 Homebrew 本来就发 bottle |
| macOS 26（Tahoe）Intel | ⚠️ | ⚠️ | ⚠️ | 上游 Intel 构建越来越少，`ibrew census` 会显示真相 |
| Linux x86_64 / arm64 | ⚠️ | ⚠️ | ✅ Node 镜像逻辑可用 | 只有 brew 垫片/Node 部分有意义 |

## Node.js 上限（来自上游，不是 intelbrew 的决定）

`ibrew node` 会选择**官方 `darwin-x64` 包声明的 macOS 下限**与本机兼容的最新版本。
最后一次在 Ventura 13.7 x86_64 上验证时：

| Node 大版本 | 有 darwin-x64 包 | 能在 Ventura 13 跑 |
|---|---|---|
| 18 | ✅（已 EOL） | ✅ |
| 20 | ✅ | ✅ |
| 22 | ✅ | ✅ |
| 24 | ✅ | ✅（`minos 13.5`） |
| 26 | ✅ | ✅（`minos 13.5`，2026-10-21 实测） |

这张表是**实测**的，不是假设：`ensure_runtime` 会读取刚下载构建的 Mach-O 部署目标
（`otool -l … minos`），如果要的 macOS 比本机新就丢弃它、再试下一个较老的版本。
`ibrew doctor` 会在每台机器上打印同样的结论（`node.ceiling`），不需要你自己猜哪个大版本
“应该”能装。（本文档早期版本曾写“Node 25+ 需要 macOS 15”——实际的 `v26.12.0-darwin-x64`
声明 `minos 13.5`，在 Ventura 上可运行。所以这个门槛是**量出来**的，不是写死的。）

## 来源永远改变不了的东西

* **装的是哪些字节** —— 镜像可互换的唯一理由是摘要。
* **契约** —— `npm` 或 `conda-forge` 安装会在 `ibrew plan` 里如实标注，永远不会被报告成
  `same-contract`。
* **Homebrew 的记账** —— 只有 `brew install` 会写进 `Cellar`，其他一律进
  `~/.intelbrew/root`。
