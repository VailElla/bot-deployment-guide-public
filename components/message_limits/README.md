# QQ 消息频率限制组件

此目录整理了原部署的限频代码，并提供独立 AstrBot 插件 `astrbot_plugin_message_limits`。它针对 OneBot v11 的 QQ 事件；有序交付和文本分段节奏要求平台实例 ID 为 `snowluma`。使用了 AstrBot 内部钩子，版本升级后必须重新验证。不要与原 `astrbot_plugin_local_codex` 的同类限频钩子或 `private_rate_limit` 插件同时启用，否则可能重复计数、覆盖钩子或造成重载顺序问题。此插件不负责接入 QQ、模型、欢迎语内容或错误通知发送。

规则、计数范围、历史变更与限制详见 [消息频率限制方案](../../docs/message-frequency.md)。

## 安装

1. 在已运行的 AstrBot 环境备份配置，并先停用重叠的私聊／群聊限频插件。插件依赖 AstrBot 及其安装依赖；本次导入和钩子测试使用 AstrBot 源码 `18425f66ee73fc1ca8b321f2f186e7e1dc1c37f3`，其他版本未验证。
2. 将本目录下 **`astrbot_plugin_message_limits` 整个文件夹**复制到 AstrBot 的 `data/plugins/`，在管理界面加载并启用。插件首次启动会在 `data/plugin_data/message_limits/settings.json` 创建配置。
3. 设置文件示例：

   ```json
   {"enabled": true, "disable_random_groups": [], "reply_pacing": true}
   ```

   `disable_random_groups` 可填写不允许随机插话的群 ID 字符串；真实号码只保存在本机。改完重新加载插件。`reply_pacing` 控制 SnowLuma 模型文本分段的间隔。`enabled` 控制同群生成与有序交付重叠，**不是总开关**，群发送预算与其他限频仍然生效；需要全部停用时请停用插件。数字限额位于各源码中的构造参数和常量，调整后须重新测试并重载。
4. 先用测试账号及可控事件验证，再检查发送回执。关闭插件会释放本插件钩子；`private.sqlite3`、`welcome.sqlite3`、群限频及续聊状态文件保留。它们可能暴露关联信息，不应提交或公开。

入群欢迎冷却只对有效入群事件放行或静默，不负责产生欢迎消息，需另有欢迎功能。`incident_aggregation.py` 提供错误分类、聚合及冷却代码，**没有连接告警插件或消息发送器**；使用者需显式接入可信日志和发送函数，且不应将聊天原文写入告警。默认 10 分钟分类冷却和 30 秒全局间隔，满载通知静默、正文拒发仍归类为失败；参考实现不是独立通知插件。

## 测试

在可导入所选 AstrBot 版本的 Python 环境，在仓库根目录执行：

```powershell
$env:PYTHONPATH = "components/message_limits;$env:PYTHONPATH"
python -m unittest discover -s components/message_limits/tests -v
```

测试使用合成事件及模拟发送，含窗口、作用域、重载、重复、欢迎冷却、回复预算、告警聚合和钩子装卸。它们不能证明目标机器上的版本兼容，也不能证明 QQ 接收端实际收到消息。取消、插件并存和高并发应在目标环境继续验证。
