"""Busca por palavras-chave (BM25) — a metade "léxica" do RAG híbrido.
Acha termos exatos: códigos (P13, P45), nomes de políticas, números de NBR etc."""
from __future__ import annotations

import math
from collections import Counter


class BM25:
    def __init__(self, documentos: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(d) for d in documentos]
        self.tamanhos = [len(d) for d in documentos]
        self.media = (sum(self.tamanhos) / len(self.tamanhos)) if self.tamanhos else 0
        n = len(documentos)
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def pontuar(self, consulta: list[str]) -> list[float]:
        notas = []
        for d, tam in zip(self.docs, self.tamanhos):
            s = 0.0
            for t in consulta:
                if t not in d:
                    continue
                tf = d[t]
                s += self.idf.get(t, 0) * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * tam / (self.media or 1)))
            notas.append(s)
        return notas
