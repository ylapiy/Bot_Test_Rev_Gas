"""Fábrica de IA. Sem chave configurada o bot funciona só com regras locais."""
from __future__ import annotations

import logging

from app.config import config
from app.llm.base import LLM

log = logging.getLogger("llm")
_instancia: LLM | None = None
_carregado = False


def obter_llm() -> LLM | None:
    global _instancia, _carregado
    if _carregado:
        return _instancia
    _carregado = True
    if not config.llm_ativo:
        _instancia = None
    elif config.llm_provedor == "anthropic":
        from app.llm.anthropic_llm import AnthropicLLM

        _instancia = AnthropicLLM()
    elif config.llm_provedor == "openai":
        from app.llm.openai_llm import OpenAICompativelLLM

        _instancia = OpenAICompativelLLM()
    log.info("IA: %s", _instancia.nome if _instancia else "desligada (modo regras)")
    return _instancia


def definir_llm(llm: LLM | None) -> None:
    """Para testes: injeta um LLM falso."""
    global _instancia, _carregado
    _instancia, _carregado = llm, True
