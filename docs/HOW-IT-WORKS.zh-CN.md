# 工作原理（方法论）

[English](HOW-IT-WORKS.md) · **简体中文**

这份文档是可复用的部分：它能用在**任何** Intel Mac 和任何 macOS 版本上，而不只是开发
它的那台机器。下面的机制都在 `src/intelbrew/` 里实现；数据来自 `ibrew census` 和
`ibrew doctor`。

---

## 1. 先用数据确定“最后一个还发 Intel 二进制的 macOS”

不要硬编码“Ventura 已经没人管了”，要问数据：

```bash
# 某个配方“现在”到底发布哪些 bottle 标签
curl -s https://formulae.brew.sh/api/formula/node.json |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d["bottle"]["stable"]["files"]))'
# → ['arm64_golden_gate', 'arm64_linux', 'arm64_sequoia', 'arm64_tahoe', ..., 'x86_64_linux']
```

对标签列表套用这几条规则：

| 现象 | 含义 |
|---|---|
| 所有配方都没有 `<我的macOS>` 标签 | Homebrew 已停止为该版本发版 ⇒ 全部走编译 |
| 只有 `arm64_<x>` 标签 | 只支持 Apple Silicon |
| 只剩 `x86_64_linux` | Intel macOS 已完全不在支持范围 |
| 最新 Intel 标签是 `sonoma`，而你在 `ventura` | 可以尝试“落后一个版本”的 bottle（见 §3） |

这套逻辑在 `src/intelbrew/brew.py`：`TAG_MACOS`（标签 ↔ macOS 大版本）、
`INTEL_SUPPORTED_MAX_MACOS`、`tag_for(platform)`。Homebrew 改政策时只改这张表，别改别处。

**还要检查 Homebrew 自己的版本。** Homebrew 会把新系统映射到 `arm64_*`，而很新的 macOS
可能根本不被旧版 Homebrew 认识，此时
`brew ruby -e 'puts Homebrew::SimulateSystem.current_tag'` 的结果会和你的预期不一致。
`ibrew doctor` 会比对两者并告警。

## 2. 把每个配方分成三类：二进制 / 大概能用 / 只能编译

`ibrew census` 产出三个桶。单个配方的判定是：

```
tag = tag_for(本机)
if tag in bottle_tags(配方):            → native        （不动 brew install）
elif 存在 macos_major <= 本机的标签:      → older-bottle  （改写路由、告警、启动校验）
else:                                  → compile-only  （找上游源，否则拒绝）
```

**绝不能**路由到比本机更新的 bottle——它会链接本机不存在的符号。这就是“probable”和
“想当然”的区别。

## 3. 老 bottle：真正会坏在哪

bottle = 可重定位的 prefix + Homebrew 的记账文件：

```
node/23.11.0/
  .brew/node.rb                 ← 构建时使用的配方
  INSTALL_RECEIPT.json          ← prefix、tab、依赖
  bin/node include/ lib/ share/
```

Homebrew 依赖 `ghcr.io` 上紧邻 layer 的 OCI **config blob**（`sh.brew.bottle.digest`、
`org.opencontainers.image.version`、prefix 信息）做重定位，再按 bottle 的校验清单逐个
校验文件。两个后果：

* 只 `tar xzf bottle.tar.gz` 会得到**坏的安装**（没有 receipt ⇒ `brew list` 看不到，
  没有重定位 ⇒ `.pc`/`.dylib` 里的路径错）。
* 安装“为别的 macOS 构建”的 bottle **不是**改个标签就行：Homebrew 会拿自己的
  `SimulateSystem` 标签比对，不匹配就拒绝。

正确做法（`installer.install_bottle` 就是这么做的）：拉 OCI index → 找到目标 macOS 的
darwin 变体 → 下载 layer **和** config blob → 校验 layer 的 sha256 → 写回成本地 bottle
（`.incomplete` + `.manifest.json`）→ `brew install <该文件>`。之后的重定位、链接、receipt、
caveats 全由 Homebrew 自己做。**如果到 `ghcr.io` 的线路很慢（从国内经常如此），这条路
很慢但正确**；`ibrew install` 优先走上游构建，正是为了绕开它。

**启动校验。** 接受老 bottle 之前先读 Mach-O 的部署目标：

```bash
otool -l bin/node | grep -A2 LC_BUILD_VERSION   # minos 13.5 → 需要 macOS 13.5+
```

`installer._min_macos` 会做这件事，并在 `minos > 本机 macOS` 时告警。

## 4. 官方上游构建才是真正的答案

大多数开发工具的**上游**会自己发布 Intel macOS 二进制。它比 Homebrew 的构建更接近源头，
而且不需要重定位。

可复用的分类：

| 类别 | 例子 | 二进制在哪 | 契约 |
|---|---|---|---|
| 运行时 | Node.js | `nodejs.org/dist/vX/node-vX-darwin-x64.tar.xz` | 一致 |
| 解释器 | Python | `python-build-standalone` releases | 大致一致（不等于 brew 配方） |
| JDK | openjdk | Eclipse Temurin `x64/mac` | 一致 |
| Rust/Go CLI | uv、ripgrep、fd、gh、jq | GitHub Releases，`*darwin*amd64|x86_64*` 资产 | 一致 |
| Node CLI | typescript、pnpm | npm registry | CLI 一致，安装位置不同 |
| 数据/媒体工具 | ffmpeg、imagemagick | conda-forge `osx-64` | 运行时不同 |
| Python CLI | poetry、pre-commit | PyPI wheel / zipapp | CLI 一致 |

保持诚实的几条规则：

* **资产过滤比看起来更严格。** 必须同时含 `darwin|macos|osx` 和精确架构词
  （Intel 用 `amd64`/`x86_64`/`x64`），并拒绝带**另一种**架构词的资产。
  `.pkg`、`.dmg`、`.msi` 一律拒绝（它们是安装器，不是压缩包）。
* **摘要来自 API，不来自文件名。** 2025 年起 GitHub 资产自带
  `asset.digest = "sha256:…"`；否则读同目录的 `*.sha256` 或 `SHA256SUMS`。
  没有摘要 ⇒ 不安装（除非显式覆盖）。
* **版本要求要有据可依。** `engines.node` / `engines.macos` 和 recipe 里的
  `requires_macos` 都会被强制执行；“需要一个要 Node 22 的工具不会因为‘通常能跑’
  就装在 Node 18 上”。
* **npm 只用于已知的 JS 工具。** npm 上存在被抢注的占位包
  （`uvicorn@0.0.1-security`），把任意 brew 名字映射到 npm 会装错东西。
  `planner.NPM_TOOLS` 是白名单。

## 5. 能在烂网络下活下来的镜像策略

三个独立问题，三种机制：

1. **元数据**（几 KB–MB）：`net.request` 带重试；基础 URL 来自 `IBREW_*_MIRRORS` 环境变量，
   默认是上游 + 清华/中科大/阿里/npmmirror。
2. **产物**（MB–GB）：`net.download_race` 用 `curl` 同时向所有镜像发请求，谁先完成用谁，
   杀掉其余，再校验赢家。没有 `curl` 时回退 `urllib`。实测同一台机器上 Python 的
   `urllib` 只有约 27 KB/s，而 `curl` 有 12 MB/s——所以用 `curl`。
3. **GitHub**（API 和 release 资产）：`gh-proxy.com` / `ghfast.top` 前缀代理；
   API 限流用 `GITHUB_TOKEN` / `gh auth token`。

所有镜像提供的是**同一份字节**，校验正是让我们能把它们视为可互换的原因。
镜像永远改变不了“装的是什么”，只改变“多快”。

## 6. 安全模型

五层，每层单独都有价值：

| 层 | 机制 | 防的是什么 |
|---|---|---|
| 路径安全 | `archive.py` 拒绝 `..`、绝对路径、越界符号链接、越界硬链接、设备文件 | 恶意/畸形压缩包写到 prefix 之外 |
| 完整性 | 解包前校验 sha256（npm 用 registry 的 sha1） | 损坏或被替换的下载 |
| 来源 | GPG 签名（可选）或 Apple `codesign` 团队校验 | 发布者不对 |
| 兼容性 | 链接前检查 Mach-O 架构和 `minos` | “装上了但 dyld 崩” |
| 可回滚 | 装进 `~/.intelbrew`，被顶掉的文件备份到 `~/.intelbrew/backups` | 丢掉原本能用的工具 |

Homebrew 管辖的安装**只由 Homebrew 自己**创建（`brew install <bottle 文件>`），
所以 `brew doctor`、`brew list`、卸载都照常工作。

## 7. 透明垫片（为什么它们不是 wrapper）

* `~/.intelbrew/bin/node` 是指向官方二进制的**符号链接**；`npm`/`npx` 是三行 `exec` 脚本。
  热路径上没有解释器，不存在“node 启动 node”。
* `~/.intelbrew/brew-bin/brew` 是垫片：判断子命令，需要改写的才改写，
  **其余全部 `exec` 真正的 brew**。不会多出一个要学的包管理器。
* 全部魔法就是 PATH 顺序：`~/.intelbrew/bin:~/.intelbrew/brew-bin:$PATH`。
  撤销 = 从 shell 配置里删掉一行。

## 8. 验证 recipe（大多数项目跳过的部分）

recipe 是一个**断言**：“这个 URL、这些字节、这种压缩布局、这个二进制名，在这个平台上能跑”。
`ibrew doctor <配方>` 会检验这个断言：

1. 版本要求检查（文档化的 macOS/Node 下限），
2. 下载到一次性的 `INTELBREW_HOME`，
3. 校验摘要，
4. 解包（含路径安全检查），
5. 安装到临时 prefix，
6. **真的执行**每个声明的二进制（先 `--version`，再 `-version`），
7. 成功则把 `verified: {by, at, machine}` 写回 recipe。

没有新鲜 `verified` 块的 recipe，在评审眼里只是“猜测”，不是事实。CI 会在 macOS runner
上跑 Intel 矩阵。

## 9. 在另一台 Intel Mac 上复现

```bash
git clone https://github.com/intelbrew/intelbrew && cd intelbrew
./install.sh --prefix ~/.local
ibrew doctor                 # 环境 + 网络 + recipe
ibrew census                 # 这台机器上什么会编译
ibrew setup --apply          # node/npm/npx + brew 垫片 + PATH
brew install node            # 被改写，不再编译
```

如果 `ibrew doctor` 报 `brew.platform` 与 bottle 数据不一致，跑 `brew update`；
如果 `node.ceiling` 说最新官方构建需要更新的 macOS，工具已经选了本机能跑的**最新**版本——
上限是从构建的 `minos` 量出来的，不是写死的清单。
