# 语音训练数据的来源隔离

`split_manifest.py` 按录音来源划分训练与验证，并以文件 SHA-256 把完全相同的音频归到同一侧；来源与重复文件构成的传递关系也合并。它不会识别不同编码或切片形式的近重复，应由使用者先审核并为同一录音的所有切片填写相同 `source`。该工具只准备清单，不执行音频审核、预处理或模型训练。

输入为 UTF-8 JSONL，每行例如：

```json
{"audio":"audio/example.wav","text":"示例台词","source":"recording-001"}
```

音频路径可以相对于清单；自行准备经授权的录音及逐字文本。命令使用新目录，避免覆盖已有划分：

```powershell
python split_manifest.py dataset.jsonl --output-dir split-v1 --validation-fraction 0.1 --seed 42
python -m unittest discover -s tests -v
```

输出 `train.jsonl` 和 `validation.jsonl`，含解析后的本地绝对路径和文件哈希，务必保存在私有位置。来源组保持完整，因此实际比例可能偏离目标；若所有数据连成一个组，程序会拒绝拆分。使用不同的清单顺序会改变结果；应固定并保存原始清单及随机种子。
