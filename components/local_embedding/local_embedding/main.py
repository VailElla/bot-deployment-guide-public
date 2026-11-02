"""Local Chinese embeddings for AstrBot; downloads are prepared before activation."""
import asyncio
import threading
from pathlib import Path

from astrbot.api.star import Context, Star, StarTools
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import EmbeddingProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter

MODEL = 'BAAI/bge-small-zh-v1.5'
TYPE = 'local_fastembed_zh'

if TYPE in provider_cls_map:
    LocalEmbedding = provider_cls_map[TYPE].cls_type
else:
    @register_provider_adapter(
        TYPE, '本机中文嵌入模型（CPU，512维）', provider_type=ProviderType.EMBEDDING,
        provider_display_name='本机 BGE 中文嵌入',
        default_config_tmpl={'id':'livingmemory-local-zh','type':TYPE,'enable':False,
                             'provider_type':'embedding','embedding_model':MODEL,'embedding_dimensions':512},
    )
    class LocalEmbedding(EmbeddingProvider):
        def __init__(self, provider_config, provider_settings):
            super().__init__(provider_config, provider_settings)
            self.model = MODEL
            self.set_model(MODEL)
            self._model = None
            self._lock = threading.Lock()
            self._gate = asyncio.Semaphore(1)
            self._tasks = set()

        def _embed(self, texts):
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding
                    cache = Path(StarTools.get_data_dir('local_embedding'))/'models'
                    self._model = TextEmbedding(model_name=MODEL, cache_dir=str(cache),
                                                threads=2, local_files_only=True,
                                                providers=['CPUExecutionProvider'])
                vectors = [v.tolist() for v in self._model.embed(texts,batch_size=16)]
                if len(vectors) != len(texts) or any(len(v)!=512 for v in vectors):
                    raise RuntimeError('Local embedding returned an invalid vector shape')
                return vectors

        async def get_embeddings(self, text):
            if not text:
                return []
            if len(text)>256 or any(not isinstance(t,str) for t in text):
                raise ValueError('Expected at most 256 text inputs')
            async def run():
                async with self._gate:
                    return await asyncio.to_thread(self._embed, text)
            task=asyncio.create_task(run())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            # Cancellation must not release the gate while ONNX still runs.
            return await asyncio.shield(task)

        async def get_embedding(self,text):
            return (await self.get_embeddings([text]))[0]

        def get_dim(self):
            return 512

        async def terminate(self):
            if self._tasks:
                await asyncio.gather(*self._tasks,return_exceptions=True)
            self._model=None

class LocalEmbeddingPlugin(Star):
    def __init__(self,context:Context):
        super().__init__(context)
