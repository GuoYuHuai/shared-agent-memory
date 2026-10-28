# 本地目录与移动存储

知识保存在你选择的目录中，工具放在单独的目录。本地与移动盘使用都需要 Python 和 Git，可以不配置远端。新安装只创建知识区、政策、配置和本地 Git 历史；历史导入与后台整理默认关闭。

## 从本地目录开始

进入工具目录后执行：

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" init
```

Windows PowerShell 对应为：

```powershell
py -3 .\scripts\ak.py --root "$env:USERPROFILE\Documents\My-Knowledge" init
```

已有知识应先接续。安装器可以创建新目录或接续已识别的知识库；普通非空目录需要审查迁入，不能直接覆盖。先读知识目录的 `POLICY.md`，再用虚构信息试一次：

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" remember "示例项目使用蓝色主题" --source fictional:onboarding --scope project:demo --id demo-theme
python3 scripts/ak.py --root "/path/to/my-knowledge" correct demo-theme "示例项目改用绿色主题" --source fictional:onboarding-correction
python3 scripts/ak.py --root "/path/to/my-knowledge" recall "主题" --scope project:demo
```

结果应给出绿色主题及其来源，旧记录仍可追溯。示例会保存在这个知识目录中。日常也可以直接让 Agent 记住、查询或更正信息；不必自己输入这些命令。

将知识连接到宿主实际读取的指令文件，才能让后续工作找到它：

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" connect --instructions "/path/to/work-project/AGENTS.md"
python3 scripts/ak.py --root "/path/to/my-knowledge" status
```

先确认宿主会读取所选文件，再在新聊天里查询示例主题，验证连接是否生效。完整流程见 [BOOTSTRAP](../BOOTSTRAP.md)。

## 使用移动盘

先挂载实际设备，选择卷内的知识子目录：

```sh
python3 scripts/ak.py --root "/Volumes/MyDrive/My-Knowledge" init --mode removable
python3 scripts/ak.py --root "/Volumes/MyDrive/My-Knowledge" recall "主题" --scope project:demo
```

Windows 使用实际盘符，例如：

```powershell
py -3 .\scripts\ak.py --root "E:\My-Knowledge" init --mode removable
py -3 .\scripts\ak.py --root "E:\My-Knowledge" recall "主题" --scope project:demo
```

`removable` 模式检查挂载点与卷身份，拒绝系统卷、缺席盘对应的空目录和不匹配的卷。它不会挂载设备或复制文件。已有绑定无法确认时，先检查设备与路径，不要新建同名替代库。更换卷需要单独审查迁移方式。

## 在新设备上继续

先取得同一或兼容的工具版本，再取得已有知识：可以连接原移动盘，也可以克隆自己的私库。纯本地保存不会自动出现在另一台电脑。

已有私库克隆到本机后，接续时提供同一仓库身份：

```sh
python3 scripts/ak.py --root "/path/to/cloned-knowledge" init --repository owner/private-knowledge
```

移动盘仍使用 `init --mode removable`。随后连接新设备上的宿主指令，并检索一条已知记录，核对相同 ID 与来源。普通 Markdown 目录的导入和未知 schema 的迁移需要单独处理，见[升级与退出](upgrading.md)。

安装记录绑定设备、知识目录和工具位置。移动目录或换设备后，需要重新接续并检查指令路径；旧设备留下的任务回执不表示新设备已经运行相同任务。原始会话默认留在来源设备，新增设备不会自动复制它们。

## 按需要开启同步或历史

本地知识可以稍后连接自己的已有私库：

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" connect-sync --repository owner/private-knowledge
python3 scripts/ak.py --root "/path/to/my-knowledge" sync
```

连接步骤验证仓库与历史，不上传资料；`sync` 是单独的同步动作。所需权限、空远端与已有历史的处理见[同步指南](sync.md)。

历史采集和后台整理按选定来源范围另行配置，见[自动化](automation.md)。迁移目录或拔盘前，先停用该设备上使用此路径的任务并确认停止。卸载保留知识和用户政策，操作见[升级与退出](upgrading.md)。
