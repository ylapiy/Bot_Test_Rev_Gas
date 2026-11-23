"""Busca vetorial — a metade "semântica" do RAG híbrido.

Dois modos (EMBEDDINGS_PROVEDOR no .env):
  - local  : vetores TF-IDF de n-gramas de caracteres (sem custo e sem internet).
             Tolera erros de digitação e variações ("butijão", "botijões").
  - openai : embeddings de verdade via API (OpenAI ou compatível). Recomendado em
             produção para entender sinônimos ("acabou meu gás" ~ "fazer pedido").
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter

import httpx

from app.config import config
from app.util.texto import expandir_girias, termos, tokens

log = logging.getLogger("rag.vetorial")


def _features(texto: str) -> Counter:
    f: Counter = Counter()
    for tok in tokens(expandir_girias(texto)):
        w = f"#{tok}#"
        for n in (3, 4):
            for i in range(len(w) - n + 1):
                f["c:" + w[i:i + n]] += 1
    for t in termos(texto):
        f["w:" + t] += 2
    return f


class IndiceLocal:
    def __init__(self, textos: list[str]) -> None:
        feats = [_features(t) for t in textos]
        n = len(feats)
        df: Counter = Counter()
        for f in feats:
            df.update(f.keys())
        self.idf = {k: math.log((n + 1) / (v + 1)) + 1 for k, v in df.items()}
        self.vetores = [self._vetor(f) for f in feats]

    def _vetor(self, f: Counter) -> dict:
        v = {k: (1 + math.log(c)) * self.idf.get(k, 1.0) for k, c in f.items()}
        norma = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {k: x / norma for k, x in v.items()}

    def similaridades(self, consulta: str) -> list[float]:
        q = self._vetor(_features(consulta))
        return [sum(q[k] * d.get(k, 0.0) for k in q) for d in self.vetores]


class IndiceAPI:
    """Embeddings via API compatível com OpenAI (/embeddings). Usa cache em disco."""

    def __init__(self, textos: list[str]) -> None:
        self.cache_arq = config.pasta_dados / "embeddings_cache.json"
        try:
            self.cache = json.loads(self.cache_arq.read_text()) if self.cache_arq.exists() else {}
        except json.JSONDecodeError:
            self.cache = {}
        self.http = httpx.Client(base_url=config.embeddings_base_url, timeout=30,
                                 headers={"Authorization": f"Bearer {config.embeddings_api_key}"})
        self.vetores = self._embed(textos)
        self.cache_arq.write_text(json.dumps(self.cache))

    def _chave(self, t: str) -> str:
        return hashlib.sha256(f"{config.embeddings_modelo}|{t}".encode()).hexdigest()

    def _embed(self, textos: list[str]) -> list[list[float]]:
        faltando = [t for t in textos if self._chave(t) not in self.cache]
        for i in range(0, len(faltando), 64):
            lote = faltando[i:i + 64]
            r = self.http.post("/embeddings", json={"model": config.embeddings_modelo, "input": lote})
            r.raise_for_status()
            for t, item in zip(lote, r.json()["data"]):
                self.cache[self._chave(t)] = item["embedding"]
        return [self.cache[self._chave(t)] for t in textos]

    def similaridades(self, consulta: str) -> list[float]:
        q = self._embed([consulta])[0]
        nq = math.sqrt(sum(x * x for x in q)) or 1
        out = []
        for v in self.vetores:
            nv = math.sqrt(sum(x * x for x in v)) or 1
            out.append(sum(a * b for a, b in zip(q, v)) / (nq * nv))
        return out


def criar_indice(textos: list[str]):
    if config.embeddings_provedor == "openai" and config.embeddings_api_key:
        try:
            return IndiceAPI(textos)
        except Exception as e:  # noqa: BLE001
            log.warning("Embeddings via API falharam (%s). Usando índice local.", e)
    return IndiceLocal(textos)
