# 本机中文嵌入模型

此目录提供项目中使用的 AstrBot 嵌入适配源码 [`local_embedding`](local_embedding/main.py)，将 `BAAI/bge-small-zh-v1.5` 以 CPU 方式提供给 LivingMemory。模型为 512 维；初始化使用 FastEmbed，并设定 `local_files_only=True`，因此需要先单独下载缓存。模型本身和缓存不随仓库发布。

项目的历史运行环境使用 FastEmbed 0.9.1 和 ONNX Runtime 1.31.1。先检查 Python、操作系统和依赖兼容性；插件依赖必须安装到 **AstrBot 实际使用的 Python 环境**，不一定是系统 `python`。以下示例中的 Python 路径需替换：

```powershell
$python = 'C:\path\to\astrbot-python.exe'
& $python -m pip install 'fastembed==0.9.1' 'onnxruntime==1.31.1'
$cache = Join-Path $env:USERPROFILE '.astrbot\data\plugin_data\local_embedding\models'
& $python components/local_embedding/warm_model.py --cache $cache
```

最后一条会联网下载模型并检查两个合成向量的维度。缓存路径必须与运行插件时 `StarTools.get_data_dir('local_embedding')/models` 一致；若 AstrBot 数据根不是默认值，调整 `$cache`。模型下载与许可须以当前上游为准，不要把缓存提交到仓库。

把本目录的 `local_embedding` 文件夹复制到 AstrBot 的 `data/plugins/`，加载并启用；然后按 [`examples/provider.json`](examples/provider.json) 创建 `local_fastembed_zh` 嵌入提供商，选为 LivingMemory 的 `embedding_provider_id`。若界面不能编辑此类型的字段，可在停止 AstrBot 并备份主配置后，将该条目加入现有 `provider` 数组；不要覆盖整份配置。为避免插件目录覆盖，先确认目标目录不存在。

适配器使用单进程锁和单槽限制模型调用。被取消的请求不会提前释放实际仍在执行的嵌入任务；停止插件时会等待任务结束。更换模型或维度前应备份数据并按目标插件的方式迁移或重建索引，不能将不同模型向量混用。

测试在可以导入 AstrBot 的 Python 环境中从仓库根目录运行：

```powershell
$env:PYTHONPATH = "components/local_embedding;$env:PYTHONPATH"
python -m unittest discover -s components/local_embedding/tests -v
```

本仓库测试用模拟 FastEmbed 验证 512 维输出、离线加载参数和取消后的串行约束；不下载模型，也不证明目标机器的 ONNX 安装或真实模型可用。项目历史部署曾使用真实模型验证；迁移后须重新执行预热及检索验证。
