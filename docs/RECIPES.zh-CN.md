# Recipe 规范

[English](RECIPES.md) · **简体中文**

recipe 是一份**经过机器验证的断言**：某个配方如何从二进制安装。它是 intelbrew 里唯一会
随时间增长的部分，也是贡献者唯一需要碰的部分。

## 存放位置

* `registry/`（仓库内）——内置、经评审、随工具发布。
* `~/.intelbrew/registry/`——你自己的；格式相同，优先级高于内置。

`ibrew install <名字>` 先查注册表；命中 recipe 就跳过所有启发式猜测。

## 字段

```json
{
  "name": "ripgrep",
  "version": "15.2.0",
  "requires": { "macos": ">=11" },
  "verified": { "by": "ibrew doctor", "at": "2026-10-21", "machine": "macOS 13.7.8 x86_64" },
  "steps": [
    {
      "kind": "archive",
      "source": "github-release",
      "url": "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-apple-darwin.tar.gz",
      "sha256": "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
      "fallback_urls": ["https://gh-proxy.com/https://github.com/…"],
      "strip": 1,
      "install": { "bin": ["rg"] }
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `kind` | `archive`（带校验的包）、`npm-global`、或 `bottle`（brew ghcr） |
| `url` / `fallback_urls` | 主地址和可互换镜像（字节必须一致） |
| `sha256` | `archive` **必填**；没有摘要就拒绝安装 |
| `fmt` | 当 URL **就是**二进制本身（没有压缩包）时写 `"raw"`；tar/zip 省略 |
| `strip` | 去掉几层顶层目录（1 = 标准 release 包） |
| `install.bin` | 包内哪些路径变成命令；省略则自动探测 |
| `requires_macos` | 硬下限；低于它直接拒绝该步骤 |
| `gpg` | `{sig_url, keys_url, fingerprint}`，装了 `gpg` 时校验 |
| `codesign_team` | 例如 `HX7739G8FX`（Node.js Foundation）；只报告，不强制 |
| `verified` | 由 `ibrew doctor` 写入；评审时看它，不要手改 |

## 创建一条

```bash
# 1. 找到该工具的官方 Intel macOS 构建
#    （GitHub release 资产、上游发行目录、或厂商下载页）

# 2. 一条命令创建并验证
ibrew add ripgrep \
  "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-apple-darwin.tar.gz" \
  --sha256 af7825fc… --bin rg --version 15.2.0 --apply
```

`--apply` 会在一次性的 `INTELBREW_HOME` 里跑完整 doctor 流程（下载 → 校验 → 解包 → 安装 →
执行）。成功后会写回 `verified` 并打印要提交的确切文件。

## 评审清单（PR 用）

1. **确实有 Intel 构建** —— `x86_64`/`amd64`，不是只能靠 Rosetta 的 universal。
2. **摘要是上游的** —— 来自 release API、`*.sha256` 或 `SHA256SUMS`；不要只用自己的下载
   算一遍而不交叉验证。
3. **契约诚实** —— 如果二进制与 brew 配方不是同一个东西（运行时不同、CLI 不同），在 PR 里
   说明，并尽量不要加。
4. **`requires_macos` 有据可查** —— 来自项目 README 或 `minos`。
5. **`verified` 是新鲜的** —— 由真实 Intel Mac 上的 `ibrew doctor` 生成，`machine` 里带
   macOS 版本。
6. **回退镜像字节一致** —— 镜像必须提供相同的 `sha256`。

## 优先想要的 recipe

`node@20`、`node@22`、`uv`、`jq`、`gh`、`yq`、`fzf`、`fd`、`bat`、`ruby`（ruby-build）、
`openjdk@21`。上游 Intel 支持不确定的（`ffmpeg`、`python@3.x`）需要写清取舍说明，
而不是悄悄加一条 recipe。
