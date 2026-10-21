# 常见问题

[English](FAQ.md) · **简体中文**

**这是又一个 Homebrew 的 fork 吗？**
不是。没有包宇宙、没有 formula DSL、没有 bottle 服务器。它只是把每次安装路由到**已经存在**
的官方二进制；`brew` 垫片对不需要改写的一切都调用真正的 `brew`。

**为什么不直接用 `brew install --force-bottle`？**
因为 Homebrew 用自己的平台标签问“有我的 bottle 吗”，而这个标签永远指向已经停止发版的平台。
没有任何参数能改变这件事。强行换标签会被拒（“bottle 是给另一个 macOS 版本的”）；手动从 OCI
仓库里拖 bottle 解包，会因为没有 Homebrew 的重定位元数据而得到坏安装。

**为什么不直接用 `nvm` / `fnm` 装 Node？**
你应该用——`ibrew node` 做的就是同一件事（官方构建、多镜像、垫片），并且有可验证的
SHA256/签名。它额外把 `brew install node` 也改写了，这样你剩下的 brew 流程不会再去编译 node。
两者可以共存，用你喜欢的。

**`brew upgrade` 会不会搞坏我的环境？**
`ibrew` 从不修改 Homebrew 的数据库。`brew upgrade` 有可能仍按 Homebrew 自己的意思用源码重装；
垫片会改写 install 类子命令，所以 `brew install`/`reinstall`/`upgrade <名字>` 会走规划器。
不确定就跑 `ibrew status`。

**在 Apple Silicon / 更新的 macOS 上能用吗？**
能，但通常没必要：Homebrew 还在发 arm64 bottle。`ibrew census` 会告诉你这台机器上到底有什么
需要编译。

**怎么撤销？**
从 shell 配置里删掉 `intelbrew` 那段，再删 `~/.intelbrew`。被顶掉的文件在
`~/.intelbrew/backups`；Homebrew 装的东西用 `brew uninstall` 删。

**这些二进制安全吗？**
它们是官方上游构建，用同一个项目发布的摘要校验，可能还校验了 GPG/Apple 签名。镜像只能改变
**速度**，改不了字节。信任模型的漏洞见 [LIMITATIONS.zh-CN.md](LIMITATIONS.zh-CN.md) §4。

**如果 `node` / `npm` / `npx` 找不到二进制呢？**
`ibrew node` 有一条第 1→3 的阶梯，且**绝不会**在你指定大版本时偷偷换一个：

1. 该大版本下、本架构最新的补丁版；
2. 若这个构建声明的 macOS 比你的新，就退到同大版本更老的补丁；
3. 全都不行才停下，并明确说明：`Node 99 has no official x86_64 macOS build.
   Available majors: 26, 25, 24, …`，同时给出提示（`ibrew node use 26`）。

装好之后，`node`/`npm`/`npx` 三个垫片都是**带守卫的**：运行时目录被删或被移走时，它们会以
退出码 127 失败，并打印

```
intelbrew: the managed Node runtime is missing: /Users/…/.intelbrew/node/v20.20.2/bin/node
  fix: ibrew node use 20   (or: ibrew node install)
```

不会“继续在 PATH 里往后找”然后悄悄用另一个 `node`（那会导致版本和 npm 都不对）。
`ibrew doctor` 会逐个报告坏掉的垫片，`ibrew node list` 也会提示。

**我已经在用 nvm / fnm / asdf，会不会打架？**
intelbrew 的 PATH 块生效时，它的垫片优先（`~/.intelbrew/bin` 在最前）。`ibrew setup --apply`
检测到别的版本管理器会给出警告。想让 nvm/fnm 继续管 `node`，跑
`ibrew setup --uninstall-profile` 即可——`brew` 和 `ibrew install` 仍然照常工作。

**为什么 `ibrew doctor` 说最新 Node 在这台机器上跑不了？**
因为那个 Intel 构建声明了更高的 macOS 下限（`otool -l … minos`）。`ibrew node` 会装
**能跑的最新版**——它读取刚下载构建的部署目标，所以上限是按机器实测的，从不写死。

**公司代理需要认证怎么办？**
设置 `http_proxy`/`https_proxy`（curl 和 urllib 都认）。需要不同线路的镜像可以用
`IBREW_NODE_MIRRORS`、`IBREW_PYPI_MIRRORS`、`IBREW_NPM_MIRRORS`、`IBREW_CONDA_MIRRORS`、
`IBREW_BREW_API_MIRRORS` 覆盖。

**我需要的配方既没有 recipe 也没有上游二进制怎么办？**
`ibrew install <名字>` 会拒绝并指向 playbook。可选：用老版本、用容器、自己编译，或者为你信任的
厂商二进制写一条 recipe（`ibrew add`，必须有 `--sha256`）。

**能锁版本吗？**
能：recipe 带 `version` 和 `sha256`；`ibrew plan <配方>@<版本>` 会在源支持 tag 时请求具体
上游 tag（GitHub releases、Node 大版本）。

**会动 `/usr/local` 吗？**
只有 `brew install` 会写进 Homebrew 的 Cellar。其他来源的一切都在 `~/.intelbrew` 里。
`~/.intelbrew/bin` 被加到 `PATH` 最前，所以它**遮蔽**而不会覆盖 Homebrew 的软链。
