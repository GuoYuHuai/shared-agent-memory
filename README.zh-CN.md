[English](README.md) · [简体中文](README.zh-CN.md)

# Shared Agent Memory

正常和 Agent 工作，让它记下值得下次用到的事。开新会话、换工具或换电脑时，少从头解释一遍。

你在工作中随口交代的这些事，都可以成为共享记忆：

- “Atlas 的预览服务使用 4310 端口。”
- “Atlas 的进展说明控制在三个短段落。”
- “这个应用选择本地存储，是为了离线时也能用。”

Agent 负责记录、查找和更新，你不需要整理笔记或管理记录。来源和旧版本留在后台，帮助它分清什么仍然适用。

目前通过宿主的持久指令接入，自动程度取决于 Agent 是否加载并执行这些指令。接入时需要实际测试新会话；支持范围见[兼容性说明](docs/compatibility.md)。

## 开始使用

把仓库链接交给你的 Agent，让它帮你接入：

> 帮我接入 Shared Agent Memory：https://github.com/GuoYuHuai/shared-agent-memory。请阅读 BOOTSTRAP.md；如果我已有知识库，优先接续使用。

[BOOTSTRAP.md](BOOTSTRAP.md) 让 Agent 查找已有记忆，检查工具和权限，完成能代办的配置，再验证是否真的记得住、找得回。只有登录、授权或无法判断的必要选择，才需要你参与。

接好后，继续像平常一样工作：

> Atlas 的预览端口改成 4311，继续写部署说明。

Agent 应当在完成任务时更新记忆。下次只说“继续部署 Atlas”，它应先查到新端口，而不是再问你一次。你也随时可以说“记住这个”“这个已经变了”或“这条不要保存”。

已经启用私库同步时，保存和更正会顺带尝试同步；离线时先留在本地。不同 Agent 可以使用同一目录，或各自已同步的副本。新设备首次使用时，把同一个私库交给 Agent 接续即可。

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

本地工具已在 macOS、Linux 和 Windows 自动测试环境中验证，GitHub 流程也完成了真实读写检查。各款 Agent 的原生接入验证范围不同，具体集成请查看[兼容性说明](docs/compatibility.md)。

## 适合我吗？

如果一份简短的 Markdown 文件已经够用，可以继续用它。Shared Agent Memory 想省下的是换会话、换工具、换设备时反复交代的功夫：由现有 Agent 维护同一份记忆，配置改了，接着工作的 Agent 也能查到新值。

这里采用轻量的文件与文本检索，不要求额外部署模型或数据库服务。如果你更需要现成的宿主 hooks、语义搜索或 MCP 服务，可以看看[其他方案](docs/choosing.md)。选择能在你的环境里真正减少重复解释的那个。

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
