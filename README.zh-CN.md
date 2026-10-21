# intelbrew

[English](README.md) · **简体中文**

> 让 Intel Mac 上的 `brew`、`node`、`npm`、`npx` 继续可用——当你的 macOS 比 Homebrew
> 最后一个还发 Intel 预编译包的 macOS 更新时，自动把安装切换到**官方发布的二进制包**，
> 并在落地时校验完整性。

Homebrew 4/5+ 只给 Apple Silicon 和最新的 Intel macOS 出预编译 bottle。在老一点的 Intel
Mac 上（比如 2015–2018 款 MacBook Pro 跑 Ventura 13），几乎每个 `brew install` 都会
**退回源码编译**：一个工具几十分钟到几小时，还经常编不过。intelbrew 不 fork Homebrew、
也不需要编译器：它为每个包规划一条**二进制路线**，校验字节，再用不破坏原有命令的方式装好。

```
brew install node      →  Node.js 官方 darwin-x64 构建   （已签名、SHA256 校验）
ibrew install ripgrep  →  上游 GitHub Release 资产       （SHA256 校验）
ibrew install uv       →  Astral 官方 macOS 构建         （SHA256 校验）
ibrew install ffmpeg   →  conda-forge osx-64 包          （SHA256 校验）
```

---

## 1. 问题（实测，不是估计）

`ibrew census` 会去 Homebrew API 查“这台机器到底有多少配方有可用的 bottle”：

```console
$ ibrew census
macOS 13.7.8 x86_64: bottle tag 'ventura'
  native bottles   1234 / 7358  (16.8%)
  would compile    6124
```

也就是说 Ventura x86_64 上 **约 83% 的 `brew install` 会走编译**。热门包同样如此：
`node`、`python@3.12/3.13`、`ffmpeg`、`go`、`git`、`postgresql`、`redis`、`imagemagick`、
`nginx`、`rust` 都没有 `ventura` bottle；只有少数已停止维护的版本化配方（`node@18`、
`icu4c@76`）还留着。

还有两件更麻烦的事，本项目都处理了：

* `brew` 用**自己的平台标签**决定“有没有 bottle”，而这个标签永远不会指向已经停止发版的
  平台——所以**没有任何参数**能把 bottle 找回来。
* `ghcr.io` 上确实还存在一些老 bottle，但直接 `tar xzf` 解开会得到坏安装，因为缺 Homebrew
  的 **relocation 元数据**（`.brew/` 里的安装记录）。

## 2. 它做什么

| 命令 | 行为 |
|---|---|
| `ibrew doctor` | 校验机器、Homebrew、网络、镜像、代理，并逐条验证内置 recipe（真的下载、解包、运行、回写验证信息） |
| `ibrew census` | 这台机器上多少配方是二进制、多少会编译 |
| `ibrew plan <包>…` | 试运行：用哪个源、哪个版本、怎么校验、契约类型 |
| `ibrew install <包>…` | 按计划安装；**永不编译**；校验摘要 |
| `ibrew node install/use <大版本>` | 官方 Node.js 构建（多镜像赛跑）+ `node`/`npm`/`npx` 垫片 |
| `ibrew add <命令> <url> --sha256 …` | 把已验证的压缩包（或用 `--raw` 指定裸二进制）变成 recipe，并立即验证 |
| `ibrew why <名字>` | 说清这个命令/包在这台机器上能从哪来：原生 bottle、recipe/上游二进制，还是 npm（以及 `npx` 是否要编译） |
| `ibrew setup --apply` | 一条命令装好：Node 运行时 + 垫片 + `brew` 垫片 + shell 配置 |
| `ibrew status` / `ibrew env` / `ibrew verify` | 查看状态、shell 集成、单独校验文件 |

`brew` 垫片保留 Homebrew 的接口，**只改写** Homebrew 本来会编译的那部分安装：

```console
$ brew install node            # 实际走 ~/.intelbrew/brew-bin/brew
==> node: Homebrew has no ventura bottle — using node-dist (same-contract, verified)
✓ node v24.22.0 — digest-only: sha256 [prefix]
```

`brew list`、`brew info`、cask、tap、`--build-from-source` 以及其他所有子命令都原样透传给
真正的 `brew`。

## 3. 安装

```bash
curl -fsSL https://raw.githubusercontent.com/Adminote/ventura-x86-recipe/main/install.sh | sh
# 或在源码目录里：
./install.sh --prefix ~/.local

# 安装脚本会把 PATH 写进你的 shell 配置；当前这个终端要手动加载一次
source ~/.zshrc            # zsh；bash 用 ~/.bash_profile
ibrew doctor               # 装任何东西之前先看这个
ibrew setup --apply        # Node 运行时、node/npm/npx 垫片、brew 垫片
```

如果装完提示 `ibrew: command not found`，说明 PATH 已经写进配置文件、但这个终端还没读它——
`source` 一下或开个新终端：

```bash
grep -n intelbrew ~/.zshrc ~/.zprofile   # 看块写到哪个文件了
ibrew setup --apply                      # 重新写入，并打印具体文件
ibrew setup --uninstall-profile          # 再删掉
```

要求：Intel macOS 11+（Apple Silicon 也能用，只是没那么必要）、`python3` 3.9+（系统自带即可）；
Homebrew 只在你想用 `brew` 垫片时才需要。

## 4. 它怎么决定从哪拿二进制

对每个包按顺序尝试，命中即停：

1. **内置 recipe** —— `registry/<名字>.json`，由 `ibrew doctor` 在本机验证过。
2. **本平台的 Homebrew bottle** —— 取 `≤ 本机 macOS` 的最近标签（白名单：
   `golden_gate`、`big_sur`、`monterey`、`ventura`、`sonoma`、`sequoia`、`tahoe`、`*_linux`）。
   **比本机新的标签一律拒绝**，绝不猜测。
3. **上游直供** —— 工具自己的官方构建（`node-dist`、`github-release`、`adoptium-jdk`、
   `pypi`、`conda-forge`、`npm`）。
4. **拒绝** —— 并打印对应的 playbook 页面，告诉你怎样自己加一个源。

每个计划都带两个互不混淆的属性：

* **契约（contract）** —— `same-contract`（接口与配方一致）或 `npm`/`shortcut`
  （目的相同但接口不同，例如 `uv` 能装 Python，但它不是 `python`）。
* **可信度（confidence）** —— `verified`（recipe 或有文档的上游映射）、`probable`
  （在更老 macOS 上构建的 bottle）、`heuristic`（猜的仓库，需要人工复核）。

校验分三级，如实报告：`unverified`（默认拒绝）、`digest-only`（只有摘要）、
`verified`（摘要 **且** GPG/Apple 签名）。

## 5. 进度与“是否卡住”

每个命令都会说明自己在干什么。耗时步骤（配方索引、多镜像赛跑、Homebrew API 查询、recipe 验证）
会先打印一行开始信息，在终端里显示带秒数的转圈，结束时打印 `✓ … 4.2s`：

```console
$ ibrew plan jq git
==> resolving 2 formula(s) for macOS 13.7.8  [0.2s]
==> looking up jq in the Homebrew API (formulae.brew.sh) …
  ✓ looking up jq in the Homebrew API (formulae.brew.sh)  3.4s
```

不会再出现“几分钟毫无输出、不知道是慢还是死了”的情况。百分比是**实测值**，不是估算：

| 场景 | 显示 |
|---|---|
| 任何下载（含多镜像赛跑） | `uv-x86_64-apple-darwin.tar.gz  42.3%  7.3/17.2 MB  0.5 MB/s` |
| `ibrew install a b c` | `[2/3 · 33.3%] b 1.8.2 via registry` |
| `ibrew doctor` | `[4/6 · 50.0%] network`，以及 `28.6% probing nodejs.org (3/7)` |
| recipe 验证 | 每个 recipe 一步：`[4/6 · 50.0%] validate recipe jq` |

`IBREW_NO_PROGRESS=1` 关闭转圈；管道/CI 里每 5 秒打印一行百分比而不是转圈。

## 6. 边界——它**不做**什么

* **不是 Homebrew 的 fork，也不是一个包宇宙。** 它只做“路由到官方上游构建 + Homebrew
  bottle”，不搭建私有 bottle 服务器。
* **不会复活只发 Linux 或已停更的软件。** 上游没有 Intel macOS 二进制时，它明确拒绝，
  绝不偷偷编译。
* **不改写别的工具的行为。** `brew install python@3.12` 只有在存在 recipe 时才会被改写；
  `pip install numpy` 仍然由 pip 自己决定用不用 wheel——那是 pip 的职责。
* **不支持 `.pkg`/`.dmg` 安装器**（需要交互式安装和全局写入）、不支持 macOS GUI 应用
  （用 `brew --cask`）、默认不支持无摘要的下载（除非你设 `IBREW_ALLOW_UNVERIFIED=1`）。
* **不保证每个 bottle 都能跑。** Sonoma 上构建的 bottle 可能用到 Ventura 没有的符号；
  `ibrew install` 会检查 Mach-O 的部署目标并给出警告。
* **不修复坏网络。** 它自带镜像列表、GitHub 代理和并行赛跑，但如果任何镜像都不可达，
  它也帮不上忙（先跑 `ibrew doctor`）。

完整边界见 [docs/LIMITATIONS.zh-CN.md](docs/LIMITATIONS.zh-CN.md)。

## 7. 覆盖矩阵

完整矩阵（源 × 契约 × 校验 × 是否实测）见 [docs/SOURCES.zh-CN.md](docs/SOURCES.zh-CN.md)。速览：

| 场景 | 支持 | 方式 |
|---|---|---|
| Intel macOS 12–15 上的 Node/npm/npx | ✅ 已实测 | nodejs.org / TUNA / npmmirror 官方构建，多镜像赛跑 |
| 单文件 CLI 工具（`ripgrep`、`fd`、`gh`、`uv`…） | ✅ 已实测 | 官方 GitHub Release 资产 + GitHub 资产摘要里的 `sha256` |
| 只发裸二进制、没有压缩包的工具（`jq`） | ✅ 已实测 | `"fmt": "raw"` recipe：资产本身就是可执行文件 |
| Java（`openjdk`） | ✅ | Eclipse Temurin x64 构建 |
| 依赖 Homebrew 库的工具（`ffmpeg`、`imagemagick`） | ⚠️ 部分 | conda-forge `osx-64` 包（独立运行时，不与 brew 链接） |
| Python（`python@3.x`） | ⚠️ 部分 | 上游独立构建，不等于替换 brew 配方 |
| macOS GUI 应用 | ❌ | 用 `brew install --cask` |
| 上游没有 Intel 二进制的任何东西 | ❌ 设计如此 | 拒绝，并打印 playbook |

## 8. 仓库结构

```
src/intelbrew/        工具本体（只用标准库，零依赖）
  sources.py          二进制从哪来（每个源一个函数）
  planner.py          配方 → 有序步骤、契约、可信度
  installer.py        下载 → 校验 → 解包 → 安装 → 链接
  brewshim.py         `brew` 改写器（永远调用真正的 brew）
  nodemgr.py          官方 Node 运行时 + node/npm/npx 垫片
  archive.py          安全解包（zip 路径穿越、符号链接逃逸、硬链接检查）
  verify.py           SHA256 / GPG / codesign 报告
  registry.py         recipe 注册表
  doctor.py           “在这台机器上真的能跑吗？”
registry/             内置 recipe（JSON，需经本机验证）
docs/                 方法论、源、边界、recipe 规范、FAQ
tests/                离线单元测试（不需要网络）
```

## 9. 开发

```bash
python3 -m pytest tests/ -q          # 离线，不用网络
PYTHONPATH=src python3 -m intelbrew.cli doctor --no-apply
```

最有价值的贡献是 **recipe**：每个配方一个已验证的 JSON 文件，证据由
`ibrew doctor <配方>` 生成，见 [docs/RECIPES.zh-CN.md](docs/RECIPES.zh-CN.md)。

## 10. 状态

| 模块 | 状态 |
|---|---|
| Node 运行时、垫片、多镜像赛跑 | 可用，已端到端跑通 |
| GitHub Release / 官方发行版路线 | 可用，已端到端跑通 |
| recipe、doctor、census、plan | 可用 |
| Homebrew bottle 路线（ghcr、relocation） | 已实现；速度取决于你到 `ghcr.io` 的线路 |
| conda-forge 路线 | 已实现，部分验证 |
| GPG 校验 | 已实现，需要 `gpg` 和 keyring |

## 许可证

MIT，见 [LICENSE](LICENSE)。与 Homebrew、Node.js Foundation 及其安装的任何项目均无隶属关系。
