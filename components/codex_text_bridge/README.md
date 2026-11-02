# 本机 Codex CLI 文字桥接

这个插件将项目原有的 Codex 提供商调用流程整理成独立、**仅支持文字**的 AstrBot 示例。源码见 [`astrbot_plugin_codex_text_bridge`](astrbot_plugin_codex_text_bridge/)，可填写的提供商条目见 [`examples/provider.json`](examples/provider.json)。它使用运行 AstrBot 的系统账户已经完成的 Codex CLI 登录，不读取或复制登录令牌。

它不是原有完整桥接：不支持图片输入、图片生成、音频、AstrBot 工具、用户沙箱、文件交付或持久 Codex 线程。收到不支持的媒体和工具请求时会报错；历史对话由 AstrBot 提供，每次 CLI 调用使用临时线程。流式接口仅在结果完成后一次交付，不是真正逐 token 输出。不要与原 `local_codex_cli` 混淆，二者注册的类型不同，也没有共享并发池。

**部署边界：**这是供可信用户测试的基础适配。`read-only` 和关闭 shell 等 CLI 开关不是完整的宿主机、账户或提示注入隔离；CLI 的实际工具和操作系统限制会随版本变化。不要据此把宿主机凭证或其他用户文件暴露给不可信聊天。实际对外开放前，应在专用、低权限系统账户及独立环境中审查当前 CLI 的可用工具、目录访问和网络权限。此组件没有提供原方案的 Shipyard 工具隔离。

## 安装与配置

1. 安装与目标 AstrBot 版本兼容的 Codex CLI。以**运行 AstrBot 的系统账户**完成 CLI 登录；认证文件留在该账户。确认 CLI 支持 `exec --ignore-user-config --ephemeral --json`。忽略用户配置意味着自定义供应商、代理和其他 CLI 设置不会自动继承。
2. 确认 AstrBot 数据目录。以默认 Desktop 数据目录为例，在本仓库根目录的 PowerShell 运行：

   ```powershell
   $pluginRoot = Join-Path $env:USERPROFILE '.astrbot\data\plugins'
   $source = Join-Path (Get-Location) 'components\codex_text_bridge\astrbot_plugin_codex_text_bridge'
   $target = Join-Path $pluginRoot 'astrbot_plugin_codex_text_bridge'
   if (Test-Path $target) { throw '目标插件已存在，请先核对，勿覆盖。' }
   New-Item -ItemType Directory -Force -Path $pluginRoot | Out-Null
   Copy-Item -Recurse $source $target
   ```

   数据目录已自定义时，改为实际的 `data\plugins`。在 AstrBot 加载并启用插件；确认插件正常加载后才创建提供商。
3. 以 [`provider.json`](examples/provider.json) 为模板，将 `codex_executable` 改为当前机器的 **`codex.exe` 绝对路径**，`workspace` 改为独立、空白且该账户可写的绝对目录。示例路径仅作占位。此处不接受 `.cmd` 包装脚本；可先用 `Get-Command codex -All` 定位安装，再核实实际可执行文件。
4. 在 AstrBot 的模型／提供商管理中新增类型 `codex_text_bridge` 的模型，填写对应字段；如果该版本界面没有显示自定义字段，停止 AstrBot 并备份 `data/cmd_config.json`，把示例**作为一项**加入其现有 `provider` 数组，保留其他项，再启动并检查加载结果。不同版本可能把来源放在 `provider_sources` 中，模型条目覆盖同名来源字段；以实际配置为准。不要用示例文件覆盖整份主配置。然后在目标配置或会话中选中 `codex-text`。
5. 先在 AstrBot 内用不含私人内容的简单文字验证，再使用经授权的测试账号检查 OneBot 收发与多轮上下文。插件加载、模型响应和 QQ 收发是分别验收的步骤。

`model=default` 由 CLI 决定实际模型；要指定模型时使用账户当前可用的真实名称。`model_reasoning_effort` 可留空，或者填写当前模型和 CLI 支持的强度。`timeout` 单位为秒，范围 1–600；每次尝试覆盖排队、上下文准备和 CLI，超时会先终止并等待旧进程结束，再重新尝试一次，因此最坏等待时间可超过一个 `timeout`。其他错误不会按超时策略重试。

同一事件循环里，此插件的所有提供商实例共享 4 个并发槽，第五个等待；另一个进程或另一套提供商不受这个池限制。它不代替 AstrBot 的会话隔离与同群顺序控制。进程输入走 stdin 而非命令行参数；角色指令和临时工作目录会在请求期间写入本机磁盘并在结束后清理，操作系统权限和异常退出后的残留仍由部署者管理。不要在工作目录中放置个人文件。

插件热重载会保留 AstrBot 已注册的提供商类；修改桥接代码后应重启 AstrBot，再确认使用的是新版本。停用该插件或移除提供商时先切换依赖该提供商的聊天和记忆任务；不要删除已有会话或数据库。

## 测试

在可以导入目标版本 AstrBot 的 Python 环境，从仓库根目录运行：

```powershell
$env:PYTHONPATH = "components/codex_text_bridge;$env:PYTHONPATH"
python -m unittest discover -s components/codex_text_bridge/tests -v
```

测试使用本地模拟 CLI 与合成消息，检查 JSONL、完整回合裁剪、跨提供商并发、超时后终止旧进程、失败处理及流式接口。已在 Python 3.12 与 AstrBot 源码 `18425f66ee73fc1ca8b321f2f186e7e1dc1c37f3` 环境执行；尚未使用此精简版完成 Windows、真实 Codex 模型或 QQ 端到端验证。正式部署需另行验收。
