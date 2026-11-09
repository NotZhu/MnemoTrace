from mnemotrace.embed import Embedder


class MockEmbedder(Embedder):
    """Return test-supplied vectors, with a fixed default for other inputs."""

    def __init__(self, dim=256, *, vectors=None, default=None):
        self.dim = dim
        self.vectors = vectors or {}
        self.default = [0.0] * dim if default is None else default

    def embed(self, text):
        return list(self.vectors.get(text, self.default))


def product_embedder():
    return MockEmbedder(default=[1.0] + [0.0] * 255)
