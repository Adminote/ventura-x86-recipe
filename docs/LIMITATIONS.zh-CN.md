# 边界——支持什么、不支持什么

[English](LIMITATIONS.md) · **简体中文**

这份清单是刻意的。每一行都描述你会**实际观察到**的行为，而不是愿望。
如果某个情况这里没写，就默认不支持，直到 `ibrew doctor` 证明它可以。

## 1. 设计上不支持

| 情况 | 行为 | 原因 |
|---|---|---|
| 源码编译 | `ibrew install` 永不编译；只有**你**给 `brew` 垫片加 `--build-from-source` 时才编译 | 编译本身就是问题，不能当兜底 |
| `.pkg` / `.dmg` / `.msi` 载荷 | 拒绝 | 安装器需要交互和全局写入，且不幂等 |
| macOS GUI 应用 | 拒绝 | 用 `brew install --cask`；App 不是 prefix 可安装的 |
| 没有摘要的未签名下载 | 默认拒绝（除非 `IBREW_ALLOW_UNVERIFIED=1`） | 无法验证的字节不算安装 |
| 任意 brew 名字 → npm | 拒绝 | npm 上有抢注占位包（`uvicorn@0.0.1-security`） |
| 只支持 Linux 的项目 | 拒绝，并指向 playbook | 不存在 macOS 二进制 |
| 替换 Homebrew 的依赖图 | 不尝试 | 依赖跟着包一起来：bottle 自带依赖，上游构建是自包含的 |
| 私有/闭源二进制 | 不自动解析 | 可以用 `ibrew add` 自己加 recipe |

## 2. 支持但有前提

| 情况 | 前提/代价 |
|---|---|
| 为**更老** macOS 构建的 bottle | 可能用到本机没有的符号；`ibrew install` 会读 Mach-O `minos` 并告警，但无法证明运行时正确。状态是 `probable`，不是 `verified` |
| 为**更新** macOS 构建的 bottle | 直接拒绝——这是唯一“猜一定错”的情况 |
| `python@3.x` | 走上游**独立** Python 构建。`pyenv`/`uv` 可以互操作；但 brew 的 `python3` 软链和 site-packages **不会**被重建 |
| `ffmpeg`、`imagemagick`、`redis`… | conda-forge 构建使用自带库集合，**不**链接 Homebrew 的 `libvpx`/`openssl`/`jpeg`。这是特性（不编译、不炸依赖），也是限制（不等价于 brew 配方） |
| x64 构建要求更新 macOS 的 Node 大版本 | `ibrew node` 会读下载构建的 `minos`，要的版本比本机新就丢弃并试下一个较老版本；上限按机器实测，不靠假设 |
| Node 18 + npm 11 | npm ≥ 11 声明 `engines.node >= 20`；垫片在系统有可用 npm 时回退到系统 npm，而不是掩盖冲突 |
| GPG 校验 | 需要装 `gpg` 且有 keyring；没有时报告写 `digest-only`，绝不写 `verified` |
| `codesign` 检查 | 只能证明**谁签的**，不能证明代码安全；多数开源工具是 ad-hoc 签名 |
| Apple Silicon | 支持但基本没必要（Homebrew 本来就发 bottle）；工具会自动优先 arm64 资产 |
| `brew update` 之后重新安装 | 某个配方可能又有了原生 bottle（或失去了）；重跑 `ibrew census`——决策永远从实时数据重算，不会缓存成政策 |

## 3. 任何工具都做不到的事

* **完全没有可用线路的网络。** 多镜像赛跑只挑最快的可用镜像，不能凭空造一条。`ibrew doctor`
  会告诉你哪些端点死了。
* **已经停止发布 Intel 二进制的项目。** 没有东西能变出二进制；诚实的选项只有：用老版本、
  用容器、或者自己编译。
* **内核/驱动级软件**（kext、系统扩展、虚拟化驱动）——由 SIP 和公证决定，不由包管理器决定。
* **与 Homebrew 构建逐字节一致。** 编译参数、OpenSSL、路径都不同。要可复现，就锁死 recipe
  里的 `sha256`。

## 4. 安全模型和它的洞

* 摘要证明**你拿到的字节就是源发布的字节**，不证明发布者善良。
* 对 `nodejs.org`、`github.com`、`ghcr.io`、`pypi.org` 的 TLS 是信任锚。一个同时拿下镜像和
  元数据端点的攻击者会赢——所以只要可能，元数据和产物会从**不同主机**获取。
* `IBREW_ALLOW_UNVERIFIED=1` 是应急开关；它会大声打印 `unverified`，并且拒绝把 `verified`
  写进 recipe。
* GitHub 代理（`gh-proxy.com`、`ghfast.top`）是第三方，能看到你的请求路径。它们改不了字节
  （摘要会失败），但能看到你下载**什么**。用 `IBREW_GITHUB_MIRRORS=`（空值）可以关闭。
* 压缩包用加固过的解包器，但恶意**二进制**依然会运行。验证收窄了“可能是谁写的”，
  不是沙箱。

## 5. 已知粗糙处（公开跟踪，不隐藏）

* 某些国内线路下 `ghcr.io` 很慢；所以 Homebrew bottle 路线是最慢的一条，排在选择顺序最后。
* conda-forge 路线只用 `current_repodata.json`（体积小）。只出现在完整 repodata 里的包
  不会被解析到。
* pypi zipapp 路线依赖项目发布 `.app`/wheel；否则回退到 GitHub releases。
* 冷缓存下 `ibrew doctor` 可能要一分钟（配方索引约 30 MB）。
* recipe 覆盖刻意保持很小：错的 recipe 比没有 recipe 更糟。
