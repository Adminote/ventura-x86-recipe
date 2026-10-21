# Playbook：没有二进制时怎么办

[English](SOURCE-PLAYBOOK.md) · **简体中文**

`ibrew install <名字>` 拒绝你，是一项五分钟工作的开始，不是结束。按下面顺序排查，
命中某一行之后，要么手工装，要么加一条 recipe。

## 0. 先确认你是不是真的处在坏情况里

```bash
ibrew census                 # 这台机器上多少配方会编译
ibrew plan <名字> -v         # intelbrew 现在会怎么做
```

`plan` 已经给出源的话，这一页你不需要看。

## 1. **上游项目**有没有发布 Intel macOS 构建？

去项目的 releases 页面，资产名必须**同时**匹配 `darwin|macos|osx` 和 Intel 词
（`x86_64`、`amd64`、`x64`、`intel`）。

| 资产名模式 | 结论 |
|---|---|
| `foo-1.2-x86_64-apple-darwin.tar.gz` | ✅ 可用 |
| `foo-1.2-darwin-amd64.zip` | ✅ 可用 |
| `foo-1.2-universal2.dmg` | ⚠️ universal 可以，但 `.dmg` 不是压缩包，要手动挂载 |
| `foo-1.2-aarch64-apple-darwin.tar.gz` | ❌ 只有 Apple Silicon |
| `foo-1.2-x86_64-unknown-linux-gnu.tar.gz` | ❌ 只有 Linux |
| `foo-1.2.pkg` | ❌ 安装器；实在要拆就 `pkgutil --expand-full` |

然后：

```bash
ibrew add foo "<url>" --sha256 <release 里 .sha256 / SHA256SUMS 的摘要> --bin foo --apply
```

## 2. 它是 Node.js、Python、Java、Rust 项目吗？

| 生态 | 用什么 | 为什么可行 |
|---|---|---|
| Node.js | `ibrew node install <大版本>` | 官方 darwin-x64 构建，多镜像赛跑 |
| Node CLI | 在托管运行时上 `npm i -g <包>` | 纯 JS + 预编译原生插件 |
| Python | `uv python install 3.12`，再 `uv tool install <包>` | 独立构建 / wheel，不需要 brew |
| Java | Temurin（`ibrew install openjdk@21`） | Adoptium 发布 x64 macOS 构建 |
| Rust CLI | 有编译器就 `cargo install --locked <crate>`；否则用 release 二进制 | 我们要避开的正是本地编译 |

## 3. 是依赖一堆原生库的媒体/数据工具吗？

（`ffmpeg`、`imagemagick`、`graphviz`、`pandoc`、`redis`…）

```bash
conda create -n tools -c conda-forge ffmpeg
# 或者
ibrew install ffmpeg        # 可用时会路由到 conda-forge
```

取舍：conda-forge 自带一套库。它能跑，但**不**链接 Homebrew 的库——不要把 brew 的
`ffmpeg` 插件和 conda 的二进制混用。

## 4. 项目已死 / 只支持 Linux？

那就没有二进制，也没有诚实的绕路：

* 用最后还发 Intel 构建的老版本（写进 recipe 固定住），
* 用容器（`docker run …`）跑 CLI，
* 自己在私有 prefix 里编译（`./configure --prefix=~/.local && make`）。

把你选了哪条记下来，下一个用同款 Mac 的人会感谢你。

## 5. 把 recipe 加进去，别人就不用重做

```bash
IBREW_REGISTRY_DIR=~/.intelbrew/registry \
  ibrew add foo "<url>" --sha256 <摘要> --bin foo --version 1.2 --apply
```

验证通过就提 PR，附 `registry/foo.json`（规范见
[docs/RECIPES.zh-CN.md](RECIPES.zh-CN.md)）。如果只在更新的 macOS 上验证通过，
就设 `requires_macos`，并在 PR 里说明。

## 6. 怎么判断一个第三方二进制值不值得信

你即将运行别人的代码。进本仓库 registry 的最低门槛：

1. 发布者是项目自己或知名厂商（Astral、Eclipse、GitHub…），
2. 下载地址是 HTTPS，且在发布者自己的域名或其 GitHub releases 上，
3. 摘要是同一个发布者给出的（资产摘要、`*.sha256`、`SHA256SUMS`），
4. 最好还有 `ibrew verify` 能验证的 GPG/Apple 签名。

任一项缺失，仍可用 `IBREW_ALLOW_UNVERIFIED=1` 在本机安装，但它会永远被报告为
`unverified`，也不应该进内置 registry。
