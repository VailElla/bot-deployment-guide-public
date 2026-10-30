# 本地 Qwen ASR 组件

这里包含方案中的本地 ASR 源码：校验文件哈希、跨进程锁、WAV 规范化、静音检测和有界分段，以及调用 OneBot `get_record` 的适配类。它是桥接插件的一部分，**不是独立 AstrBot 插件**；QQ 原生转写、框架 STT 优先级和事件绑定仍需其余桥接代码。模型、引擎及 ffmpeg 须自行准备，本仓库不含这些二进制文件。

需要 Python 3.11+、Windows 版兼容 Qwen3-ASR GGUF CLI（可执行文件名 `qwen3-asr-cli.exe`）和 ffmpeg。两者的命令行参数须与 `qwen_runtime.py` 对应；更换引擎需重新验证。以下在本目录 PowerShell 运行，路径均替换为本机文件：

```powershell
python asr_cli.py manifest --engine C:\asr\qwen3-asr-cli.exe --model C:\asr\model.gguf --ffmpeg C:\asr\ffmpeg.exe --lock C:\asr\shared.lock --output C:\asr\runtime.json
python asr_cli.py transcribe --manifest C:\asr\runtime.json --wav C:\asr\test.wav --timeout 45
```

清单生成时计算引擎及模型哈希，不会覆盖已有清单。清单包含本机路径，应保留在本地。用不同进程共享模型时必须使用同一个锁路径；锁等待计入总时限。示例 `voice_asr.json` 应复制到 AstrBot 数据目录 `plugin_data/local_codex/voice_asr.json`，填写上述清单绝对路径；仅有此文件不会自动安装其余桥接。运行期间创建并清理临时文件；语音内容及输出应按实际隐私需求处理。

本组件的测试不需要模型或联网：

```powershell
python -m unittest discover -s tests -v
```

测试主要使用模拟进程和数据，不证明实际 GGUF、ffmpeg、QQ 收发或模型识别质量已通过。实际验收需自行测试多个时长、静音与低音量、取消、进程竞争及识别准确率。清单校验 ffmpeg 文件存在但不校验其哈希；请从可信来源安装并维护。
