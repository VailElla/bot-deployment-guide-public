"""Prepare the model cache once. This command downloads model files."""
import argparse
from pathlib import Path

from fastembed import TextEmbedding


def main():
    parser = argparse.ArgumentParser(description="Download and check the local 512-dimensional BGE model")
    parser.add_argument("--cache", required=True, help="Absolute plugin model-cache directory")
    args = parser.parse_args()
    cache = Path(args.cache).expanduser()
    if not cache.is_absolute():
        parser.error("--cache must be an absolute path")
    cache.mkdir(parents=True, exist_ok=True)
    model = TextEmbedding(
        model_name="BAAI/bge-small-zh-v1.5", cache_dir=str(cache),
        threads=2, providers=["CPUExecutionProvider"],
    )
    vectors = list(model.embed(["这是一个测试句子。", "这是另一个测试句子。"], batch_size=16))
    if len(vectors) != 2 or any(len(vector) != 512 for vector in vectors):
        raise RuntimeError("Unexpected embedding output dimensions")
    print("Model cache ready; two test vectors have 512 dimensions.")


if __name__ == "__main__":
    main()
