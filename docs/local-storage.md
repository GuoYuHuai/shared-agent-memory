# 本地目录与移动硬盘接入

本地和移动盘都可以独立使用，不要求 GitHub 账号、远端或后台模型。Git 在这里也用于本地版本与一致性检查；安装器创建的是用户选定的知识目录，不把知识写进公共工具目录。Agent 应先检查现有知识位置，接续已有库，并自行替换以下示例路径。

新安装采用轻量配置：`history_capture.enabled=false`、`semantic_maintenance.enabled=false`、`retrieval.include_raw_history=false`。会创建空知识区、政策、入口、配置和本地 Git 历史；不会扫描全部对话、创建原始历史数据库或登记定时任务。后续前台检索和记录可以按需生成可重建的热知识索引。

这里没有预留 40 GiB 或 50 GiB 的安装要求。开发者自己的构建/模拟器存储门槛不属于本产品的容量要求。初始体积主要是少量文本和 Git 元数据；最终用量取决于用户保存的知识、历史范围和索引，不能承诺固定上限。磁盘剩余空间仍应足够完成当前实际写入，不能在盘缺席时改写到同名的本机目录。

## 第一台设备：本地目录

先取得并固定一个经过检查的工具版本，进入该工具目录，确认 Python 与 Git 可以实际运行：

```sh
python3 --version
git --version
python3 scripts/install.py --root "/path/to/my-knowledge" --mode local
```

在 Windows PowerShell 中，可使用已验证可用的解释器：

```powershell
py -3 --version
git --version
py -3 .\scripts\install.py --root "$env:USERPROFILE\Documents\My-Knowledge" --mode local
```

安装后读该目录的 `POLICY.md`，确认保存范围，再运行一次可识别的虚构示例。以下命令从工具目录执行：

```sh
python3 scripts/kb.py --root "/path/to/my-knowledge" capture --id demo-local-blue --kind fact --scope project:demo --statement "示例项目使用蓝色主题" --source fictional:onboarding --authority user-stated --json
python3 scripts/kb.py --root "/path/to/my-knowledge" search "主题" --scope project:demo --json
python3 scripts/kb.py --root "/path/to/my-knowledge" capture --id demo-local-green --kind fact --scope project:demo --statement "示例项目改用绿色主题" --source fictional:onboarding-correction --authority user-stated --json
python3 scripts/kb.py --root "/path/to/my-knowledge" supersede --old-id demo-local-blue --new-id demo-local-green --source fictional:onboarding-correction --json
python3 scripts/kb.py --root "/path/to/my-knowledge" search "主题" --scope project:demo --json
```

确认默认结果采用新记录，旧记录与来源仍可追溯。Windows 将 `python3 scripts/...` 换成 `py -3 .\scripts\...` 并使用实际知识路径即可。示例保存在用户知识目录，是否删除遵循用户政策，不自动删除已有事实。

该过程没有建立远端同步。若之后需要 GitHub，Agent 应核实目标用户私库的身份、私有可见性和读写能力，再按 BOOTSTRAP 与同步说明接入；不能仅根据目录名或仓库名宣称已验证。

## 第一台设备：移动硬盘

先在系统中确认目标盘确实挂载、盘与路径正确、目录可访问。安装器的 `removable` 模式向上查找实际挂载点，拒绝系统根卷（Windows 拒绝系统盘）和只有空目录的缺席盘，并保存卷身份；创建目录前及进入写入锁后都会复核。它不会代替系统挂载或复制文件，也不证明设备一定是物理移动硬盘。知识目录必须是挂载卷内的子目录：

```sh
python3 scripts/install.py --root "/Volumes/MyDrive/My-Knowledge" --mode removable
python3 scripts/kb.py --root "/Volumes/MyDrive/My-Knowledge" search "主题" --scope project:demo --json
```

Windows 示例：

```powershell
py -3 .\scripts\install.py --root "E:\My-Knowledge" --mode removable
py -3 .\scripts\kb.py --root "E:\My-Knowledge" search "主题" --scope project:demo --json
```

示例盘符、卷名和路径必须替换成当前实际位置。同一知识库已有卷身份绑定时，换成另一卷会被拒绝；本版不提供自动跨卷迁移，普通目录的显式迁移使用经过用户确认的 `--mode local` 路径。卷身份在平台可提供时使用稳定卷标识，其余环境可能只有本次挂载标识；无法确认是原卷时保持受阻并重新核验，不静默换绑。盘没有挂载、权限不足或原路径缺失时，停止针对该目录的写入并报告；不要新建同名卷目录、空知识库或替代冷层。需要更换根路径时，先停用并查询确认本机已登记的任务，再移动目录或拔盘。

## 新设备或挂载路径变化

工具可以放在每台设备自己的目录中。新设备先取得同一或兼容的工具版本、验证 Python/Git，再对**已有**知识目录运行相同安装命令：

```sh
python3 scripts/install.py --root "/actual/mounted/path/My-Knowledge" --mode removable
python3 scripts/kb.py --root "/actual/mounted/path/My-Knowledge" search "主题" --scope project:demo --json
```

Windows 使用实际盘符执行对应命令。新设备应读到同一稳定知识 ID 与来源，而不是初始化另一套事实。对于本地目录跨设备，先使用用户已选的私库克隆或已授权的文件传输取得相同知识，再运行 `--mode local` 接续；仅在原设备本地保存不会自动出现在另一台电脑。

安装记录绑定 `source_device_id`、平台指纹、知识库 ID、实际知识路径与工具路径。原设备原路径重装保留本机任务归属；换设备、换路径或遇到缺少绑定的旧记录时，检索数据保留，旧安装记录备份保留，但任务成功与私库验证状态不继承。新环境必须重新验证。原设备的任务回执位于 `.ai-knowledge/automation/<device_id>/`，移动整块盘时这些文件仍可能存在，因此不能把“文件在盘上”当成本机任务已经登记。

来源定位在读取时以当前知识根重新解析并校验，旧热索引中的旧绝对路径不能让读取跑到别处。重接续不默认构建完整历史索引、不扫描历史、不删除数据。历史保留与来源指向仍依照用户政策；只有得到明确范围授权才转移原始历史，不能借“新增设备”默认复制所有对话。

设备/路径迁移的测试使用同一电脑上的独立目录、模拟已验证挂载和虚构设备身份，并覆盖挂载点空目录与换卷拒绝反例；这不代表已经验证两台实机、所有移动盘文件系统或全部 Agent。需要实际跨机验收时，逐台记录平台、工具、权限、路径和读取到的知识 ID。

## 需要时再启用历史和后台任务

前台检索、记录、更正不依赖历史回溯或定时任务。用户选择历史范围后，再编辑知识目录中的 `config/kb.json`，例如只启用受支持的 Codex 来源并限制每轮完整源行数：

```json
{
  "history_capture": {
    "enabled": true,
    "source": "codex",
    "maximum_lines": 2000
  }
}
```

这是对现有配置的局部合并示例，不是用它覆盖整个文件。`source` 也可使用实际支持的 Claude 格式；未知产品仍可通过通用前台/GitHub路径接入，历史格式不支持时明确记录缺口。`retrieval.include_raw_history` 与历史采集是独立开关，只有需要查询原始对话且已获授权时才设为 `true`。

```sh
python3 scripts/automation.py --root "/path/to/my-knowledge" run capture
python3 scripts/automation.py --root "/path/to/my-knowledge" status capture
```

这只是本次试跑，不代表已建立周期调度。按 [automation.md](automation.md) 另行登记、查询、试跑和停用，分别报告状态。超大记录、未完成回溯、认证或权限失败都必须保留待处理状态。后台模型和周审也按各自能力与预算单独启用。

## 停用与卸载

已登记任务应先在登记它的设备与原路径调用 `automation.py ... disable <job>`，确认查询已消失，再移动目录或卸载。未经归属验证的旧任务不能被宣称已停止。安装器会阻止带有本机活动任务、同机旧路径活动回执或未知旧任务归属的卸载；另一台机器的回执保留并报告未验证，不删除它们。

```sh
python3 scripts/install.py --root "/path/to/my-knowledge" --uninstall
```

卸载只移除本工具托管的规则块和活动安装标记，保留知识、政策、历史和备份。仍可直接读取文件，或用另一具备能力的 Agent 接续。没有登记任务的安装不需要先创建任务再停用。
