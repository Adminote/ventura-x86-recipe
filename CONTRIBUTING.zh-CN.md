# 贡献指南

[English](CONTRIBUTING.md) · **简体中文**

感谢你帮忙让 Intel Mac 继续可用。最有价值的贡献是**经过验证的 recipe**；其次是带上
`ibrew doctor` 输出的 bug 报告。

## 基本规则

1. **绝不加编译路线。** intelbrew 只安装已验证的二进制或明确拒绝。任何“回退到源码编译”的
   PR 都会被拒。
2. **绝不加未校验的下载。** 压缩包的 `sha256` 是必填项；`--allow-unverified` 只能作为用户
   显式覆盖，不能是默认行为。
3. **绝不猜架构。** 资产名里没有明确写出 Intel（`x86_64`/`amd64`）或 `universal`，就不要加。
4. **说清契约。** 如果这个二进制与 brew 配方不是同一个工具/接口，要标注为 `shortcut`，
   并在 PR 里解释。

## 添加 recipe

```bash
git clone https://github.com/Adminote/ventura-x86-recipe && cd ventura-x86-recipe
export PYTHONPATH=src
python3 -m intelbrew.cli add <名字> "<官方 url>" --sha256 <hex> --bin <命令> --apply
git add registry/<名字>.json
```

`--apply` 会下载、校验、解包、装进临时 prefix 并**真的运行**该二进制；成功后写入
`verified` 块。只提交在真实 Intel Mac 上生成过新鲜 `verified` 块的 recipe（写明 macOS 版本）。

## 分支流程（必须遵守）

任何人都不直接提交到 `main`，维护者也不例外：

```bash
git switch main && git pull
git switch -c fix/<主题>            # 或 docs/<主题>、registry/<名字>
# … 修复、测试、提交 …
git push -u origin fix/<主题>
# 在干净 clone 上验证后再合并
git switch main && git merge --no-ff fix/<主题> && git push
```

修 bug 前必须先复现，分支的提交信息要写清怎么复现的
（`Found by running a fresh clone on a real Intel Mac: …`）。合并统一用 `--no-ff`，
让修复作为一个整体可回溯。合并后删掉分支。

## 测试

```bash
python -m pytest tests/ -q                 # 离线，必须通过
python -m pip install "ruff==0.17.1"       # 与 CI 固定同一个版本
ruff check src tests                       # 必须干净；配置在 pyproject.toml
PYTHONPATH=src python -m intelbrew.cli doctor --no-apply
PYTHONPATH=src python -m intelbrew.cli plan <名字> -v
```

任何涉及下载的测试，都把 `INTELBREW_HOME=$(mktemp -d)` 设好，别碰真实的 `~/.intelbrew`。

## 提交信息

`<范围>: <改了什么>` —— 例如 `sources: read sha256 from GitHub asset digests`、
`docs: clarify bottle relocation limits`、`registry: add uv 0.13.4`。

## 报告问题

请附上：macOS 版本、`uname -m`、Homebrew 版本、`ibrew doctor` 输出（打码 token）、
以及失败的确切命令。如果是“装上了但崩溃”，请附 `otool -L <二进制>` 和 dyld 报错。
