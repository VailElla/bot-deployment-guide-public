# 群聊上下文组件

此目录整理原部署的群公开消息捕获与筛选代码，提供独立 AstrBot 插件 `astrbot_plugin_group_context`。它与已有的[消息频率组件](../message_limits/README.md)分工：本组件负责群消息来源、短期背景及引用筛选；后者负责同群有序交付与限频。设计和参数见[多消息与上下文方案](../../docs/multi-message-context.md)。

本组件是从定制桥接中拆出的适配，并非完整机器人或完整原桥接。仅在 OneBot 群聊且 AstrBot 的 `provider_ltm_settings.group_icl_enable` 已开启时介入；私聊沿用框架流程。具体开关在目标版本中如何配置，以该版本为准。选中的群背景作为临时请求内容，捕获和筛选不能保证模型准确理解或回复。

## 安装与配合

1. 备份目标环境，先停用已有桥接中修改 `GroupChatContext.handle_message` / `on_req_llm` 的同类功能，避免重复注入或钩子冲突。此插件使用 AstrBot 内部类；本地测试基于 AstrBot 源码提交 `18425f66ee73fc1ca8b321f2f186e7e1dc1c37f3`，其他版本需自行验证。
2. 将 `astrbot_plugin_group_context` 整个目录复制到 AstrBot 的 `data/plugins/`，加载并启用插件，确认目标配置启用了群聊上下文。若也需要已发布的同群有序交付，可按[消息频率组件说明](../message_limits/README.md)单独安装。两者负责不同钩子，不要同时启用原桥接中的对应实现。
3. 使用受授权的测试群逐项验证捕获、发送者、引用、顺序和隔离；关闭插件后复测钩子和其他插件。插件卸载时只恢复仍由自身持有的钩子，若其他插件已包装它，旧包装会退化为转发，但完整的热重载/升级兼容仍需在目标环境验证。

默认短期背景最多 128 群、每群 80 条、600 秒；单轮选取最多 8 条，背景数据通常受约 4000 字符预算限制。先捕获序号再异步格式化，按触发边界过滤后来的消息；晚完成的较早消息可能在本轮缺席。此缓存仅在进程内，普通插件重载可保留，进程重启不保证保留。临时背景、原生会话历史和长期记忆是不同机制。

`context_budget.py` 根据可信平台引用组件、当前发送者、@、词面与新近程度排序，并过滤非必要的旧图像素；不是模型语义检索。只有真正进入请求的图片才能供模型查看。`recent_images.py` 与 `image_archive.py` 提供原部署的图片捕获、归档及恢复实现供适配参考，**独立插件没有将它们接入事件钩子**；归档涉及真实图片，接入前须明确保留、删除、访问权限和隐私策略。本目录不提供转发或视频注入、失败事件登记、长期记忆、用户沙箱及完整 Codex 桥接；相应回调与失败快照函数需其他适配显式接入，单独安装本插件不会自动启用。

`reference/concurrency.py` 是原桥接将同一提供商信号量从 1 扩至 4 的参考实现，不由本插件自动加载。它依赖该桥接的 `_semaphore`、`_request_concurrency` 和构造器约定，不能直接套用于任意提供商；直接设置 `LIMIT` 也不建立会话锁、请求期限或交付队列。调整并发之前需评估模型和主机容量，并验证活跃请求、等待者、取消、重载及回退。

## 测试

在可导入目标 AstrBot 版本的环境，从仓库根目录运行：

```powershell
$env:PYTHONPATH = "components/group_context;components/message_limits;$env:PYTHONPATH"
python -m unittest discover -s components/group_context/tests -v
python -m unittest discover -s components/message_limits/tests -v
```

测试使用合成事件和模拟模型/发送，覆盖触发边界、异步格式化乱序、跨成员引用归属、钩子共存和装卸、并发许可及有序交付。它们不代表真实 QQ、任意 AstrBot 版本、模型效果或接收端通过。

本目录没有附带模型、权重、配置、日志或聊天记录；来源中的个人角色名已改为通用称呼。组件使用的 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 受其自身许可证约束；集成与再分发时应核对所用版本及相关许可。本仓库目前未声明统一的代码许可证，不能据此假定获得额外授权。
