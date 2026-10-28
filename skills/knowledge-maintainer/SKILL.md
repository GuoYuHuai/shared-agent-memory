---
name: knowledge-maintainer
description: 在工作中主动保存可复用事实、精确配置、决定、偏好和更正，按已配置方式同步；同样处理用户的一句话记录与可选定时整理。
---

# Knowledge Maintainer

从安装配置取得明确的工具根目录与知识根目录，先读用户的 `POLICY.md` 和数据版本。默认只处理当前工作中值得复用的信息；历史采集须使用用户选定的来源范围。前台记录不依赖远端或后台模型。

1. 按政策主动保存事实、决定、配置原值、偏好及更正。保留作用域、稳定 ID 和完整来源；不要等待定时任务。用户陈述保持原权威类型，模型推断使用 `--authority agent-inferred` 并保持候选。
2. 本地使用统一入口，替换下方路径、来源和示例内容：

   ```sh
   python3 /path/to/tools/scripts/ak.py --root "/path/to/my-knowledge" --json remember "需要保存的事实" --source "user:session-reference" --scope project:demo --id demo-fact
   python3 /path/to/tools/scripts/ak.py --root "/path/to/my-knowledge" --json recall "相关关键词" --scope project:demo
   ```

   需要保存完整原值时加 `--value-file "/path/to/value.txt"`，不以模糊摘要代替精确值。同 ID 同内容重试是幂等操作；同 ID 不同内容是冲突，不能覆盖。
3. 明确更正使用旧记录的稳定 ID，追加新记录并保留替代关系与旧来源：

   ```sh
   python3 /path/to/tools/scripts/ak.py --root "/path/to/my-knowledge" --json correct demo-fact "更正后的事实" --source "user:correction-reference"
   ```

   保存返回的新 ID。无法判断的争议保持候选或冲突，不作为已确认的更正。
4. 从实际目的地回读记录，核对正文、ID、来源与路径；需要精确值时验证完整原值。使用 `recall` 验证可发现性，它与文件已保存是两个检查。
5. 已配置且授权同步时运行 `ak.py --root ROOT sync`。分别报告本地保存、远端同步和另一客户端读取；离线或同步失败时保留本地记录并报告待同步，不重复制造事实。本地使用没有远端时，完成本地验证即可。
6. 只有 GitHub 工具时按 `docs/github.md` 追加事件并回读，检查预期版本或原子提交结果。只有只读能力时，明确输出尚未保存的提案。
7. 原始历史、检查点与运行日志默认留在来源设备，只同步已授权共享的知识。需要定时维护时按 `docs/automation.md` 配置，区分计划、登记、实际执行与最近成功。

任务暂停、卸载或换 Agent 时保留知识。规则文件已写入不代表宿主已经加载；用新聊天验证实际检索。
