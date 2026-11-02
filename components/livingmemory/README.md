# LivingMemory 2.8.2 本地适配补丁

这里提供从本项目维护源码整理的 [`livingmemory-2.8.2-local-adaptations.patch`](patches/livingmemory-2.8.2-local-adaptations.patch)，用于 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) 上游 **2.8.2、提交 `a1e9c20a8c5fab93d6143f51be26fb1bbf8143ec`**。补丁包含本项目对作用域、后台总结、恢复、存储和索引的修改及对应测试；两份包含部署相关示例的测试未收录。它不是新版本上游的通用补丁，也不包含模型、记忆数据库、聊天记录或私人配置。

LivingMemory 的许可证为 [AGPL-3.0](LICENSE)，上游版权与许可证继续适用；这里附带许可证原文。使用、修改或分发时请遵循该许可证和上游组件各自的许可。

## 准备和应用

在仓库根目录的 PowerShell 中，先准备一个**新的独立目录**，不要对已有运行插件直接打补丁：

```powershell
$guide = (Get-Location).Path
$work = Join-Path $guide 'work\astrbot_plugin_livingmemory'
$patch = Join-Path $guide 'components\livingmemory\patches\livingmemory-2.8.2-local-adaptations.patch'
if (Test-Path $work) { throw '工作目录已存在，请先核对。' }
git clone https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory.git $work
if ($LASTEXITCODE -ne 0) { throw '克隆失败' }
git -C $work checkout --detach a1e9c20a8c5fab93d6143f51be26fb1bbf8143ec
if ($LASTEXITCODE -ne 0) { throw '上游版本检出失败' }
git -C $work apply --check $patch
if ($LASTEXITCODE -ne 0) { throw '补丁不能干净应用，请停止并检查版本。' }
git -C $work apply $patch
if ($LASTEXITCODE -ne 0) { throw '补丁应用失败' }
```

先在独立 AstrBot 环境测试，再复制到其 `data/plugins/astrbot_plugin_livingmemory`。例如 Windows 可使用 `robocopy` 排除工作区 Git 元数据和测试缓存；在执行前替换为实际目标目录，并确保目标不存在：

```powershell
$target = Join-Path $env:USERPROFILE '.astrbot\data\plugins\astrbot_plugin_livingmemory'
if (Test-Path $target) { throw '目标插件已存在，不能直接覆盖。' }
robocopy $work $target /E /XD .git .pytest_cache __pycache__ .venv
if ($LASTEXITCODE -ge 8) { throw '插件复制失败' }
```

已运行的部署需先备份插件、配置及数据库，排空在途总结，并对照版本逐项迁移；不要把旧数据库恢复到已经产生新数据的实例上。补丁不能代替按目标版本进行三方合并和回归。

## 设置与功能边界

[`examples/config.json`](examples/config.json) 是配置片段，字段来自此补丁版本的 schema，**不是完整配置文件**。按实际创建的聊天和嵌入提供商 ID 填写，使用插件界面逐项设置；不要覆盖其他配置。示例的 `codex-text-memory` 必须预先建立，可参照[文字桥接](../codex_text_bridge/README.md)另建一个模型条目；本机嵌入见[中文嵌入适配](../local_embedding/README.md)。目标用户、各群及私聊的访问规则必须通过测试确认后再启用。

该补丁实现的关键行为包括：

- `memory_scope_mode=user` 时按可信用户隔离总结来源；可选 `group_memory.enabled` 另外保留当前群的共享记忆。用户层是否跨群和私聊共用，是需要提前告知并测试的策略。
- 内容预算、多话题分段及持久游标用于分批总结；整个多话题批次处理期间固定来源范围，避免清理前缀移动索引。合法的空事实列表允许保留，不能把寒暄强行编成事实。
- 后台最多并行处理配置数量的来源，使用滚动补位；422/429 和已有全局冷却按临时延期处理，正常失败仍保留范围并计数。失败来源低频试探，意外退出的后台循环有恢复逻辑，并记录停滞状态。
- 示例设定每批最多 800 条，目标／紧急／最大预算为 16384／24576／32768 估算 token；`max_summaries_per_hour=0` 在**此补丁版本**表示不限小时批次，而非关闭总结。它会增加上游请求压力；资源有限时应设为正数并验证。上游原版可能不接受 0。
- 示例中的 `max_sessions_per_run=4` 是后台来源 worker 数，不自动建立模型全局配额。仅当聊天和记忆均通过同一个支持共享四槽的桥接类、同一事件循环时，才共享该桥接的 4 个许可；其他提供商和进程不受此约束。

自动捕获、主动记忆工具、群会话轮换和消息顺序还涉及框架及其他适配；本补丁不提供完整原桥接。文字桥接也不支持主动记忆工具调用，不能因为安装补丁就宣称完整复现这些能力。

## 验证

在能导入 AstrBot 的 Python 3.12 环境安装目标插件依赖以及 `pytest`、`pytest-asyncio`，将补丁后的插件父目录和 AstrBot 源码加入 `PYTHONPATH`。从补丁后的插件目录运行：

```powershell
python -m pytest -q --asyncio-mode=auto tests/test_background_summary.py tests/test_background_adaptive.py tests/test_content_batching.py tests/test_topic_batch.py tests/test_topic_empty_facts.py tests/test_summary_rate_control.py tests/test_rolling_summary.py tests/test_auto_recovery.py tests/test_finish_safety.py tests/test_layered_memory.py tests/test_rotation_barrier.py tests/test_sqlite_graph_safety.py tests/test_graph_reconcile_bounded.py
```

在 AstrBot 源码 `18425f66ee73fc1ca8b321f2f186e7e1dc1c37f3`、Python 3.12 环境执行，上述 107 项定向测试通过。测试使用合成数据和模拟提供商，不证明新环境的真实 QQ、模型、长期运行或接收端通过。实际验收还应覆盖两个用户、两个群与私聊的隔离、重启、部分提交失败和恢复，记录数据保留及删除方式。
