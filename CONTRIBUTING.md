# Contributing / 参与贡献

Bug reports, fixes, documentation and support for more Agents are welcome.

## Make a change

1. Describe the problem with a small example and the result you expected.
2. Use a separate development checkout and fictional knowledge records. Keep personal notes, credentials, conversation archives and device settings out of commits.
3. Add tests for the behavior you change, including relevant permission failures, repeated operations and recovery after interruption.
4. Run the tests below. In the PR, state what changed, which checks you ran and any remaining limitations.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node tests/test_github.mjs
```

Tests should use temporary folders and clean them up. Do not run destructive tests against a knowledge folder someone uses for work.

## Support another Agent

Start with [BOOTSTRAP.md](BOOTSTRAP.md) and the [adapter guide](docs/adapters.md). Reuse the existing record format and operations. Describe what the Agent can actually read, write and execute, along with its version, system and required permissions.

Show a saved record being read back, a correction being retrieved and any existing settings being preserved. Note whether the evidence comes from a test fixture or an actual installation. A missing scheduler should not prevent normal reading and recording.

## Preserve user data

Changes should keep record IDs, sources, exact authorized values and correction history intact. Preserve user policy and custom settings during installation and upgrades. Document incompatible changes and how to recover. Keep history imports and background tasks optional.

Use the Issue and PR templates for reports. Share only the diagnostic excerpts needed to reproduce a problem. If an Agent prepares a contribution, get the contributor's authorization before it posts publicly.

Explain new dependencies and their licenses. Credit third-party code and documentation. Contributions are licensed under [MIT](LICENSE) unless explicitly stated otherwise.

---

## 中文

欢迎提交问题、修复、文档改进，以及更多 Agent 的接入支持。

### 提交修改

1. 用一个小例子说明问题，以及你期待的结果。
2. 在独立开发目录中使用虚构知识。不要提交个人资料、凭据、会话档案或设备设置。
3. 为改动添加必要的行为测试，包括相关的权限不足、重复操作和中断恢复。
4. 运行上面的 Python 与 Node 测试命令，在 PR 中说明修改内容、已运行的检查和剩余限制。

测试使用临时目录并负责清理。不要在日常使用的知识库上运行破坏性测试。

### 增加 Agent 支持

从 [BOOTSTRAP.md](BOOTSTRAP.md) 和[适配指南](docs/adapters.md)开始，复用已有记录格式和操作。说明该 Agent 实际能读取、保存和执行什么，以及版本、系统和所需权限。

验证保存后的记录能读回、更正能查到、已有设置能保留。说明结果来自测试样例还是实际安装。缺少定时能力不应影响平常的查询和记录。

### 保留用户数据

修改应保留记录 ID、来源、已授权的精确值和更正历史。安装与升级保留用户政策和自定义设置；不兼容的修改需要说明恢复方法。历史导入与后台任务继续保持可选。

报告问题时使用 Issue 或 PR 模板，只提供复现所需的诊断片段。Agent 可以帮助准备贡献，公开提交前应取得贡献者授权。

新增依赖需说明用途和许可证，引用第三方代码或文档时注明来源。除明确另有约定外，贡献采用 [MIT 许可](LICENSE)。
