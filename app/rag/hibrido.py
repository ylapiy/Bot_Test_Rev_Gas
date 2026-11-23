"""RAG híbrido = BM25 (palavras-chave) + vetorial, combinados por Reciprocal Rank Fusion."""
from __future__ import annotations

import logging
import threading

from app.config import config
from app.rag.base_conhecimento import Trecho, carregar
from app.rag.bm25 import BM25
from app.rag.vetorial import criar_indice
from app.util.texto import termos

log = logging.getLogger("rag")
RRF_K = 60


class BuscaHibrida:
    def __init__(self, pasta=None) -> None:
        self.pasta = pasta or config.pasta_conhecimento
        self.recarregar()

    def recarregar(self) -> None:
        self.trechos: list[Trecho] = carregar(self.pasta)
        self.bm25 = BM25([t.termos for t in self.trechos])
        self.vetorial = criar_indice([f"{t.titulo}. {t.secao}. {t.texto}" for t in self.trechos])
        log.info("Base de conhecimento: %d trechos de %d arquivos", len(self.trechos),
                 len({t.arquivo for t in self.trechos}))

    def buscar(self, consulta: str, k: int = 8, categorias: list[str] | None = None,
               peso_bm25: float = 1.0, peso_vetor: float = 1.0) -> list[dict]:
        if not self.trechos:
            return []
        s_bm = self.bm25.pontuar(termos(consulta))
        s_vt = self.vetorial.similaridades(consulta)
        idx = list(range(len(self.trechos)))
        if categorias:
            filtrados = [i for i in idx if self.trechos[i].categoria in categorias]
            idx = filtrados or idx  # filtro "suave": se não sobrar nada, busca em tudo
        rank_bm = [i for i in sorted(idx, key=lambda i: -s_bm[i]) if s_bm[i] > 0][:20]
        rank_vt = [i for i in sorted(idx, key=lambda i: -s_vt[i]) if s_vt[i] > 0.05][:20]
        rrf: dict[int, float] = {}
        for pos, i in enumerate(rank_bm):
            rrf[i] = rrf.get(i, 0) + peso_bm25 / (RRF_K + pos + 1)
        for pos, i in enumerate(rank_vt):
            rrf[i] = rrf.get(i, 0) + peso_vetor / (RRF_K + pos + 1)
        melhores = sorted(rrf, key=lambda i: -rrf[i])[:k]
        return [{
            "trecho": self.trechos[i], "bm25": round(s_bm[i], 3), "vetor": round(s_vt[i], 3),
            "rrf": round(rrf[i], 5),
            "pos_bm25": rank_bm.index(i) + 1 if i in rank_bm else None,
            "pos_vetor": rank_vt.index(i) + 1 if i in rank_vt else None,
        } for i in melhores]


_busca: BuscaHibrida | None = None
_lock = threading.Lock()


def obter_busca() -> BuscaHibrida:
    global _busca
    with _lock:
        if _busca is None:
            _busca = BuscaHibrida()
    return _busca


def redefinir_busca() -> None:
    global _busca
    _busca = None
