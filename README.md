# Agent Knowledge

**让 Agent 记得你的项目、配置和习惯，少一点重复解释。**

换一台电脑、换一个 Agent，或者隔了一段时间继续工作，常常要重新交代背景：这个项目做到哪了、某个配置到底是什么、上次为什么这样决定、你习惯怎样协作。

Agent Knowledge 把这些信息保存在你掌握的文件里，让具备相应能力的 Agent 按需检索、主动记录和更新。它提取自一套持续使用的个人知识库，保留已经好用的知识内核，并开放通用接入方式。

## 开始使用

把仓库链接发给你的 Agent，让它帮你接入即可。下面这句话可以直接复制：

> 请阅读 https://github.com/GuoYuHuai/agent-knowledge 的 BOOTSTRAP.md，结合我的现有知识库、设备和你实际具备的能力，帮我接入。已有资料和设置请保留；在工作中主动检索、记录值得复用的信息，并在已配置同步时同步。完成后告诉我实际可用的功能。

Agent 的执行入口是 [BOOTSTRAP.md](BOOTSTRAP.md)：先接续已有知识，再检查当前能力，完成可用的安装、检索、记录与回读。你不需要先挑一个品牌适配器，也不需要先购买模型服务或搭建后台系统。

## 默认轻量，按需要扩展

默认只主动提炼当前工作中值得复用的知识，不自动全量回溯旧会话、归档原始历史或建立覆盖全部历史的大型索引。你不需要准备与作者个人知识库相同的存储空间。需要更多历史或更大规模时，再和 Agent 沟通来源范围与保存方式，按能力逐步开启。

## 按你的方式保存

| 方式 | 适合什么情况 |
| --- | --- |
| 纯本地 | 先在一台设备上使用，不需要 GitHub 账号或远端同步 |
| 自己的私有 GitHub 仓库 | 在多台设备、多个具备 GitHub 能力的 Agent 之间共享知识；可以接入已有库 |
| 移动硬盘 | 自己携带知识目录，在可访问该盘的设备上使用 |

首台设备和新增设备都由 Agent 结合已有知识、工具、权限与存储条件处理。已有库优先接续，不另造一套互不相通的记忆。当前本地工具依赖 Python 和 Git；**使用本地 Git 不等于必须使用 GitHub**。移动盘也不是安装前提。详情见[本地与移动存储](docs/local-storage.md)。

## 三种入口，都可以独立使用

- **工作中主动记录。** Agent 在需要背景时先查知识；工作中出现新的事实、精确配置、决定、偏好或更正时，按你的政策及时保存。配置了同步就同步。
- **你说一句话。** “记住这个设置”“以后按这个习惯处理”“上次的信息改成这个”都可以直接进入同一套知识，不必等定时任务。
- **可选的定时任务。** 有需要、也有相应能力时，再安排同步、历史采集或整理。可以暂停或关闭，基本检索与记录照常使用。

这三种方式使用同一份知识和来源记录，没有必须先开启后台任务的步骤。项目的目标是减少重复解释和重新调查；节省 token 不是目标，也不是发布门槛。

## 保留知识，也保留来龙去脉

每条知识保留来源、作用域和状态。更正会建立新旧关系，旧记录仍可追溯；模型推断与用户明确陈述分开。精确配置需要时能取回原值，不被一段模糊摘要替代。

公开仓库提供工具、协议和虚构示例。你的知识放在你选择的本地目录、移动盘或自己的私有仓库；原始会话默认留在来源设备。安装、升级和退出都应保留你的资料与自定义设置。由你决定保留范围和共享方式。

## Agent 与社区

Codex 是优先参考适配器，Claude、豆包桌面版、WorkBuddy 和未来其他 Agent 都可以按实际能力接入。判断依据是能否读取、保存、执行和验证，不是产品名字是否在名单里。某项能力暂缺时，继续完成其他可用部分。

欢迎贡献新适配器、问题修复、测试、文档和使用经验。贡献可以逐步完善各环境的接入，不必等所有 Agent 都完成认证才使用或发布。开始贡献请看 [CONTRIBUTING.md](CONTRIBUTING.md)。

当前是范围明确的实验版本。实际验证范围见[兼容性记录](docs/compatibility.md)，详细结果与未完成项见[验收记录](docs/acceptance.md)；它们区分了契约测试、真实工具运行和待实测环境。

## 继续了解

- [给 Agent 的接入入口](BOOTSTRAP.md)
- [知识协议](docs/protocol.md)与[能力协议](docs/capabilities.md)
- [无需本地命令行的 GitHub 工具接入](docs/github.md)
- [可选自动化](docs/automation.md)、[升级与退出](docs/upgrading.md)
- [演示步骤](docs/demo.md)、[开发状态](docs/development-status.md)

## English summary

Agent Knowledge helps Agents reuse your project context, exact configuration, decisions and working preferences. Give this repository URL to your Agent and ask it to follow [BOOTSTRAP.md](BOOTSTRAP.md). Connect an existing private GitHub knowledge repository, use a local directory without a GitHub account, or use removable storage. Local execution currently requires Python and Git. Proactive capture during work, a direct user request and optional scheduled maintenance are equally valid entrypoints. The default is lightweight: no automatic full-history backfill, raw-history archive or history-wide large index. Larger collections can be enabled for a user-selected scope. Scheduling can be disabled without losing basic retrieval and capture. Integration follows observed capabilities rather than an Agent brand allowlist. Contributions are welcome; compatibility claims remain tied to actual evidence. Token savings are neither the product goal nor a release requirement.
