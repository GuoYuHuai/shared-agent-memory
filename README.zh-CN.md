[English](README.md) · [简体中文](README.zh-CN.md)

# Shared Agent Memory

让不同 Agent、不同设备共用你已经交代过的配置、决定和偏好。换工具或换电脑时，接着工作的 Agent 可以查到这些记录和来源。

例如：

- “Atlas 的预览服务使用 4310 端口。”
- “Atlas 的进展说明控制在三个短段落。”
- “这个应用选择本地存储，是为了离线时也能用。”

每条记录保留来源。信息改变时，更正会保留旧记录，让接着工作的 Agent 知道现在应该采用什么。

## 开始使用

把仓库链接交给你的 Agent，让它帮你接入：

> 帮我接入 Shared Agent Memory：https://github.com/GuoYuHuai/shared-agent-memory。请阅读 BOOTSTRAP.md；如果我已有知识库，优先接续使用。

[BOOTSTRAP.md](BOOTSTRAP.md) 提供具体步骤：查找已有安装，检查当前能使用的工具，连接知识目录，再实际验证保存、读取和更正。

接入后的指令会要求 Agent 在工作前检索相关知识，并在工作中及时保存值得复用的新信息。你也可以直接说：

> 记住：以后 Atlas 的进展说明用简短表格。

之后，另一个接入的 Agent 可以查到这条偏好和它的来源。要共享知识，两个 Agent 需要访问同一目录，或各自已同步的副本。

## 选择保存位置

| 位置 | 适用情况 |
| --- | --- |
| 本地目录 | 在一台电脑上使用，不需要 GitHub 账号或远端 |
| 你自己的私有 GitHub 仓库 | 在能够访问仓库的设备和 Agent 之间共享选定的知识 |
| 已挂载硬盘上的目录 | 把知识文件保存在你选择的存储设备上 |

已经通过本项目接入的知识库可以继续使用。公开项目存放工具，个人知识保存在你选择的位置。具体操作见[存储与接入说明](docs/local-storage.md)。

安装器会先检查所选存储能否安全保存记录。macOS 上的 exFAT 移动盘目前无法通过检查；可以使用本地目录或 APFS 目录，需要跨系统共享时再连接私有 Git 仓库。

本地工具需要 **Python 3.9+ 和 Git**。默认设置不需要额外的模型 API 或后台服务，也不会导入旧会话。历史导入和定时维护可以按需启用；关闭定时任务后，平常的查询和记录仍然可用。

## 在本地试用示例

安装 Python 3.9+ 和 Git 后运行：

```sh
git clone https://github.com/GuoYuHuai/shared-agent-memory.git
cd shared-agent-memory
python3 examples/local_demo.py
```

示例会保存一条虚构事实、进行更正，再从第二份本地副本读取新记录。它使用临时目录，结束后会清理。接入自己的知识库时，可以把上面的提示交给 Agent，或按 [BOOTSTRAP.md](BOOTSTRAP.md) 操作。

## 哪些 Agent 可以使用？

Agent 可以通过本地文件或 GitHub 工具使用知识库。不同产品使用同一份接入说明，由 Agent 检查当前可用的工具和权限。只有读取权限的 Agent 可以查阅知识，但不能保存修改。

本地流程已在 macOS 验证，GitHub 流程也完成了真实读写检查。其他产品和系统的验证范围不同，具体集成请查看[兼容性说明](docs/compatibility.md)。

## 适合我吗？

如果一份简短的 Markdown 文件已经够用，可以继续用它。当你需要在多个 Agent 或设备之间交接，保留配置原值、查清决定的来源，或确保改过的信息不再被当成现状时，Shared Agent Memory 会更有用。

这里采用文本检索和明确的更正关系。如果你更需要语义搜索、知识图谱或现成的 MCP 服务，可以看看[其他方案](docs/choosing.md)。

## 更多说明

- [从本地使用转为私有 Git 同步](docs/sync.md)
- [日常命令](docs/commands.md)
- [记录、来源与更正](docs/protocol.md)
- [没有本地命令行时使用 GitHub 工具](docs/github.md)
- [可选定时任务](docs/automation.md)
- [升级与卸载](docs/upgrading.md)
- [参与贡献](CONTRIBUTING.md)

Shared Agent Memory 目前是早期版本。可复现的测试结果和已知限制见[验证记录](docs/acceptance.md)。

采用 [MIT 许可](LICENSE)。
