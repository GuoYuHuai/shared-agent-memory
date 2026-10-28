# 参与贡献

欢迎贡献 Agent 或平台适配、问题修复、测试、文档和使用经验。项目关注的是让用户少重复交代背景、配置和习惯。无需先完成所有品牌认证，也不以 token 节省作为贡献或发布门槛。

请扩展共享知识内核和薄适配层，避免给每种 Agent 复制一套互不兼容的记忆系统。未知产品同样可以接入：记录实际工具、权限、操作和结果，由能力决定可用路径。

## 开始

1. 先读 [BOOTSTRAP.md](BOOTSTRAP.md)、[知识协议](docs/protocol.md)和[能力协议](docs/capabilities.md)。
2. 在独立开发目录中使用虚构样例，不把个人知识、原始会话、凭据、设备配置或私有 Git 历史带入公共贡献。
3. 保持三种入口可用：工作中主动捕获、用户直接记录、可选定时维护。不要让后台任务成为基本读写的依赖。
4. 保持默认轻量。需要历史回溯、大规模采集或索引时，提供按用户范围启用的路径，不把作者设备的存储布局变成安装前提。
5. 适配时分别说明本地文件/执行、仓库读取/写入、版本一致性、持久规则、历史访问、调度与模型能力。缺失操作应返回准确状态，并继续可用部分。
6. 对修改涉及的成功、权限不足、版本竞争、中断恢复、重复操作和已有配置保护做行为验证。不要把 mock 或同机目录当作真实产品/实机认证。
7. 在 PR 中记录实际版本、系统、权限、日期、命令或工具操作、结果与未验证项。

本地参考测试命令：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

GitHub adapter 的 Node 测试见 [docs/github.md](docs/github.md)。测试使用可清理的隔离目录，成功、失败和中断后都清理本任务可再生产物。不要针对正在使用的个人知识库运行破坏性测试。

## 兼容性与数据保护

保留稳定 ID、来源、生命周期、更正关系和精确值的政策语义。纯本地、私有 GitHub 与移动盘可以使用同一知识格式；本地 Git 不意味着需要 GitHub 账户或同步。

安装与升级只修改有所有权证据的工具文件，保留用户数据和自定义规则；冲突不能静默覆盖。版本不兼容时给出明确行为和恢复方式。卸载或停用调度不删除知识。

能力声明、契约测试、真实工具运行、原生宿主加载与物理设备验证分别记录。定时任务的计划、登记、试跑和最近成功也分别记录。错误的“没有资料”或“全部完成”同样是正确性问题。

## Issue、PR 与维护

使用仓库模板提供最小复现、预期行为和实际结果。公共报告只包含虚构或明确获准公开的材料与最小诊断片段；私人原始记录不应随问题自动上传。

Agent 可以帮助准备贡献，但向社区发送 Issue/PR 需要贡献者授权。维护者可以按需要另行安排审阅或定时维护；项目本身不自动为使用者建立公开提交、审计或合并任务。

新增依赖需要说明用途、许可证与平台影响。引用第三方代码或文档时保留来源并确认许可。除明确另有约定外，贡献采用本仓库的 MIT 许可。

## English summary

Contribute capability-based adapters, fixes, tests or documentation without duplicating the knowledge engine. Preserve lightweight defaults, local/offline use, provenance, corrections and user-owned files. Background scheduling is optional. Use fictional fixtures, report actual evidence and do not publish personal data or private Git history. Contributions are welcome before every Agent or platform is certified; token savings are not a release requirement.
