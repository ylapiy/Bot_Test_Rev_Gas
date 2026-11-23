"""Reranking: reordena os candidatos do RAG híbrido e fica só com os mais relevantes.

1) Sempre: reranker local (cobertura de termos, título/seção, categoria esperada
   pela intenção, código de produto citado).
2) Se houver IA configurada (RERANK_COM_LLM=true): a IA dá nota 0–10 para os
   8 melhores e a nota final mistura as duas coisas.
"""
from __future__ import annotations

import logging

from app.config import config
from app.util.texto import termos

log = logging.getLogger("rag.rerank")

CATEGORIAS_POR_INTENCAO = {
    "troca": ["politica", "garantia"],
    "duvida_informacao": ["faq", "manual", "politica", "regras"],
    "consultar_preco": ["catalogo", "regras"],
    "consultar_estoque": ["catalogo"],
    "fazer_pedido": ["catalogo", "faq"],
    "boleto": ["faq", "politica"],
    "pedir_desconto": ["regras"],
    "reclamacao": ["politica"],
}


def reranquear(consulta: str, candidatos: list[dict], intencao: str | None = None,
               produtos: list[str] | None = None, llm=None, top_n: int = 3) -> list[dict]:
    if not candidatos:
        return []
    q = set(termos(consulta))
    max_rrf = max(c["rrf"] for c in candidatos) or 1
    preferidas = CATEGORIAS_POR_INTENCAO.get(intencao or "", [])
    for c in candidatos:
        t = c["trecho"]
        cobertura = len(q & set(t.termos)) / (len(q) or 1)
        titulo = len(q & set(termos(f"{t.titulo} {t.secao}"))) / (len(q) or 1)
        similaridade = min(1.0, max(0.0, c.get("vetor", 0)) / 0.3)
        bonus = (0.1 if t.categoria in preferidas else 0.0) + \
                (0.1 if produtos and any(p.lower() in t.texto.lower() for p in produtos) else 0.0)
        # relevância "absoluta" (serve de limiar: abaixo dela o bot diz que não sabe)
        c["relevancia"] = round(0.5 * cobertura + 0.3 * similaridade + 0.2 * titulo + bonus, 4)
        c["score_local"] = round(0.7 * c["relevancia"] + 0.3 * c["rrf"] / max_rrf, 4)
        c["score"] = c["score_local"]

    if llm is not None and config.rerank_com_llm:
        top = sorted(candidatos, key=lambda c: -c["score_local"])[:8]
        lista = "\n".join(f"[{i}] ({c['trecho'].titulo} > {c['trecho'].secao}) {c['trecho'].resumo(300)}"
                          for i, c in enumerate(top))
        r = llm.gerar_json(
            "Você avalia relevância de trechos para responder a pergunta de um cliente de uma revenda de gás.",
            f"Pergunta: {consulta}\n\nTrechos:\n{lista}\n\nDê nota de 0 a 10 para cada trecho. "
            'Formato: {"notas": {"0": 7, "1": 2, ...}}',
        )
        notas = (r or {}).get("notas") or {}
        for i, c in enumerate(top):
            try:
                n = float(notas.get(str(i), notas.get(i, -1)))
            except (TypeError, ValueError):
                n = -1
            if n >= 0:
                c["score_llm"] = n
                c["score"] = round(0.4 * c["score_local"] + 0.6 * (n / 10), 4)
                c["relevancia"] = round(0.4 * c["relevancia"] + 0.6 * (n / 10), 4)

    return sorted(candidatos, key=lambda c: -c["score"])[:top_n]
