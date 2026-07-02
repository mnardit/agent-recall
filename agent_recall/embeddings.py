"""Pluggable embedding providers.

Default: sentence-transformers (all-MiniLM-L6-v2) via ONNX — 384d, ~23MB, zero network.

Supported providers:
  - 'sentence_transformers': Local ONNX, zero network calls
  - 'ollama': nomic-embed-text (768d) via localhost:11434
  - 'openai': text-embedding-3-small via API
  - callable(text) -> list[float]: Custom provider
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger("agent_recall.embeddings")


class EmbeddingProvider(ABC):
    """Abstract base for embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed a single text. Returns float list of dimension d."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Returns list of float lists."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimension."""

    @property
    def available(self) -> bool:
        """Whether the provider is ready to use."""
        return True


class SentenceTransformerProvider(EmbeddingProvider):
    """Local sentence-transformers (all-MiniLM-L6-v2, 384d).

    Model is lazy-loaded on first embed()/embed_batch() call to avoid
    blocking MCP server startup (~2-6s load time in __init__).
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: str = "cpu",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._model = None
        self._dimension = 384
        self._available = True  # ponytail: optimistic — verify on first use
        self._init_error: str | None = None
        self._load_attempted = False

    def _ensure_model(self) -> None:
        """Lazy-load the model on first use. Called by embed/embed_batch."""
        if self._model is not None:
            return
        if self._load_attempted:
            raise RuntimeError(
                f"Provider not available: {self._init_error}"
            )
        self._load_attempted = True
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(
                self._model_name, device=self._device,
            )
            self._dimension = self._model.get_sentence_embedding_dimension()
            self._available = True
        except ImportError:
            self._init_error = (
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )
            self._available = False
            raise RuntimeError(self._init_error)
        except Exception as e:
            self._init_error = str(e)
            self._available = False
            logger.warning(
                "SentenceTransformerProvider unavailable (%s): %s",
                self._model_name, e,
            )
            raise

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        self._ensure_model()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        embeddings = self._model.encode(
            texts, normalize_embeddings=True,
            batch_size=min(32, len(texts)),
        )
        return embeddings.tolist()


class FastembedProvider(EmbeddingProvider):
    """ONNX-based embeddings via fastembed — ~5× faster startup than PyTorch.

    Uses BAAI/bge-small-en-v1.5 (384d) by default. No PyTorch dependency.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
    ) -> None:
        self._model_name = model_name
        self._model = None
        self._dimension = 384
        self._available = True  # ponytail: optimistic — verify on first use
        self._init_error: str | None = None
        self._load_attempted = False

    def _ensure_model(self) -> None:
        """Lazy-load the ONNX model on first use."""
        if self._model is not None:
            return
        if self._load_attempted:
            raise RuntimeError(
                f"Provider not available: {self._init_error}"
            )
        self._load_attempted = True
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self._model_name)
            # Get dimension from first embedding
            test_emb = list(self._model.embed(["test"]))
            if test_emb:
                self._dimension = len(test_emb[0])
            self._available = True
        except ImportError:
            self._init_error = (
                "fastembed not installed. Run: pip install fastembed"
            )
            self._available = False
            raise RuntimeError(self._init_error)
        except Exception as e:
            self._init_error = str(e)
            self._available = False
            logger.warning(
                "FastembedProvider unavailable (%s): %s",
                self._model_name, e,
            )
            raise

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        self._ensure_model()
        result = list(self._model.embed([text]))
        return result[0].tolist() if result else []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        results = list(self._model.embed(texts))
        return [r.tolist() for r in results]


class OllamaProvider(EmbeddingProvider):
    """Ollama nomic-embed-text (768d) via local REST API."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimension = 768
        self._available = False
        self._init_error: str | None = None
        try:
            self._ping()
            self._available = True
        except Exception as e:
            self._init_error = str(e)
            logger.warning(
                "OllamaProvider unavailable (%s): %s", base_url, e,
            )

    def _ping(self) -> None:
        """Verify Ollama is reachable."""
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            f"{self._base_url}/api/tags", method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                _json.loads(resp.read().decode())
        except Exception as e:
            raise RuntimeError(f"Ollama not reachable at {self._base_url}: {e}")

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        return self._call_api(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._call_api(t) for t in texts]

    def _call_api(self, text: str) -> list[float]:
        import urllib.request
        import json as _json
        data = _json.dumps({"model": self._model, "prompt": text})
        req = urllib.request.Request(
            f"{self._base_url}/api/embeddings",
            data=data.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = _json.loads(resp.read().decode())
                return result["embedding"]
        except Exception as e:
            raise RuntimeError(f"Ollama embed failed: {e}")


class OpenaiProvider(EmbeddingProvider):
    """OpenAI text-embedding-3-small via API."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._dimension = 1536
        self._available = False
        self._init_error: str | None = None
        self._client = None
        try:
            import os
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY not set")
            from openai import OpenAI
            kwargs = {"api_key": key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = OpenAI(**kwargs)
            self._available = True
        except ImportError:
            self._init_error = "openai package not installed. Run: pip install openai"
            logger.warning(self._init_error)
        except Exception as e:
            self._init_error = str(e)
            logger.warning("OpenaiProvider unavailable: %s", e)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        if not self._client:
            raise RuntimeError(f"Provider not available: {self._init_error}")
        resp = self._client.embeddings.create(
            model=self._model, input=text,
        )
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._client:
            raise RuntimeError(f"Provider not available: {self._init_error}")
        resp = self._client.embeddings.create(
            model=self._model, input=texts,
        )
        # Sort by index to preserve order
        results = sorted(resp.data, key=lambda x: x.index)
        return [r.embedding for r in results]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# ponytail: global cache — provider init is cheap now (lazy load), reuse it
_provider_cache: EmbeddingProvider | None = None
_provider_cache_config_id: int = 0


def get_provider(
    config=None,  # EmbeddingConfig | None
) -> EmbeddingProvider | None:
    """Create an embedding provider from config.

    Returns None if no provider is available.
    Priority: fastembed > sentence_transformers > ollama > openai.
    Tries config.provider first if set. All providers lazy-load on first use.
    Results are cached globally.
    """
    global _provider_cache, _provider_cache_config_id

    config_id = hash(str(config)) if config is not None else 0
    if _provider_cache is not None and _provider_cache_config_id == config_id:
        return _provider_cache

    if config is not None:
        provider_name = config.provider
    else:
        provider_name = "fastembed"  # default: fastembed (ONNX, no PyTorch)

    # 1. fastembed (ONNX, fastest startup, no PyTorch)
    if provider_name == "fastembed" or provider_name is None:
        try:
            p = FastembedProvider(
                model_name=getattr(config, "model", "BAAI/bge-small-en-v1.5"),
            )
            if p.available:
                _provider_cache = p
                _provider_cache_config_id = config_id
                return p
        except Exception:
            pass

    # 2. sentence_transformers (PyTorch, fallback)
    if provider_name == "sentence_transformers":
        try:
            p = SentenceTransformerProvider(
                model_name=getattr(config, "model", "all-MiniLM-L6-v2"),
                device=getattr(config, "device", "cpu"),
            )
            if p.available:
                _provider_cache = p
                _provider_cache_config_id = config_id
                return p
        except Exception:
            pass

    # 3. ollama (local REST API, 768d)
    if provider_name == "ollama":
        try:
            p = OllamaProvider()
            if p.available:
                _provider_cache = p
                _provider_cache_config_id = config_id
                return p
        except Exception:
            pass

    # 4. openai (cloud API, 1536d)
    if provider_name == "openai":
        try:
            p = OpenaiProvider(
                model=getattr(config, "model", "text-embedding-3-small"),
            )
            if p.available:
                _provider_cache = p
                _provider_cache_config_id = config_id
                return p
        except Exception:
            pass

    # Fallback: try providers in priority order
    for provider_name in ("fastembed", "sentence_transformers"):
        if provider_name == "fastembed":
            try:
                p = FastembedProvider()
                if p.available:
                    _provider_cache = p
                    _provider_cache_config_id = config_id
                    return p
            except Exception:
                pass
        elif provider_name == "sentence_transformers":
            try:
                p = SentenceTransformerProvider()
                if p.available:
                    _provider_cache = p
                    _provider_cache_config_id = config_id
                    return p
            except Exception:
                pass

    return None
