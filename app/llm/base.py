"""Interface comum para provedores de IA (Anthropic, OpenAI/compatível, falso p/ testes)."""
from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Callable

log = logging.getLogger("llm")

# executar(nome_ferramenta, argumentos) -> resultado (dict serializável)
Executor = Callable[[str, dict], dict]


class LLM(ABC):
    nome = "base"

    @abstractmethod
    def gerar(self, system: str, mensagens: list[dict], max_tokens: int = 800, rapido: bool = False) -> str:
        """mensagens = [{"role": "user"|"assistant", "content": "..."}]"""

    @abstractmethod
    def agente(self, system: str, mensagens: list[dict], ferramentas: list[dict], executar: Executor,
               max_passos: int = 5) -> tuple[str, list[dict]]:
        """Loop de tool calling. ferramentas = [{"name", "description", "input_schema"}].
        Retorna (texto_final, [{"nome", "args", "resultado"}])."""

    def gerar_json(self, system: str, prompt: str, rapido: bool = True, max_tokens: int = 600) -> dict | None:
        try:
            txt = self.gerar(system + "\nResponda SOMENTE com um objeto JSON válido, sem texto fora dele.",
                             [{"role": "user", "content": prompt}], max_tokens=max_tokens, rapido=rapido)
        except Exception as e:  # noqa: BLE001 - IA nunca pode derrubar o atendimento
            log.warning("Falha na IA (json): %s", e)
            return None
        return extrair_json(txt)


def extrair_json(txt: str) -> dict | None:
    if not txt:
        return None
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", txt, flags=re.S)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None
