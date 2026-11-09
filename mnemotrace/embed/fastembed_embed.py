"""CPU inference using a pinned multilingual ONNX snapshot."""

import os
from pathlib import Path

from .base import Embedder, EmbeddingUnavailable
from ..store._validation import validate_embedding

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REPOSITORY = 'Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q'
REVISION = 'faf4aa4225822f3bc6376869cb1164e8e3feedd0'
MODEL_ID = f'fastembed:{MODEL}@{REVISION}'
FILES = ('model_optimized.onnx', 'config.json', 'tokenizer.json',
         'tokenizer_config.json', 'special_tokens_map.json', 'unigram.json')


class FastEmbedEmbedder(Embedder):
    model_name = MODEL
    model_id = MODEL_ID

    def __init__(self, *, cache_dir=None, local_files_only=None):
        try:
            from fastembed import TextEmbedding
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise EmbeddingUnavailable('Install mnemotrace-memory[fastembed] for FastEmbed.') from exc

        if cache_dir is None:
            cache_dir = os.environ.get('FASTEMBED_CACHE_PATH') or Path.home() / '.cache' / 'mnemotrace' / 'fastembed'
        if local_files_only is None:
            local_files_only = os.environ.get('MNEMOTRACE_EMBED_OFFLINE') == '1'

        def snapshot(offline):
            path = Path(snapshot_download(REPOSITORY, revision=REVISION, cache_dir=str(cache_dir),
                                          allow_patterns=FILES, local_files_only=offline, token=False))
            if any(not (path / filename).is_file() for filename in FILES):
                raise FileNotFoundError('Incomplete FastEmbed snapshot')
            return path

        try:
            if local_files_only:
                path = snapshot(True)
            else:
                from huggingface_hub.errors import LocalEntryNotFoundError

                try:
                    path = snapshot(True)
                except (LocalEntryNotFoundError, FileNotFoundError):
                    path = snapshot(False)
        except Exception as exc:
            raise EmbeddingUnavailable('FastEmbed model cache is unavailable. Preload the pinned snapshot.') from exc
        try:
            self._model = TextEmbedding(model_name=MODEL, specific_model_path=path,
                                        cache_dir=str(cache_dir), local_files_only=True,
                                        providers=['CPUExecutionProvider'])
            self.dim = self._model.embedding_size
        except Exception as exc:
            raise EmbeddingUnavailable('FastEmbed could not load the pinned ONNX model.') from exc

    def embed(self, text):
        return self.embed_batch([text])[0]

    def embed_batch(self, texts):
        if not texts:
            return []
        try:
            vectors = [row.tolist() for row in self._model.embed(texts, batch_size=32)]
            if len(vectors) != len(texts):
                raise ValueError('Embedding batch length mismatch')
            for vector in vectors:
                validate_embedding(vector, 'FastEmbed vector', self.dim)
        except Exception as exc:
            raise EmbeddingUnavailable('FastEmbed inference failed.') from exc
        return vectors


if __name__ == '__main__' and os.environ.get('MNEMOTRACE_EMBEDDER') == 'fastembed':
    FastEmbedEmbedder()
