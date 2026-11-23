"""Provedor Anthropic (Claude) com tool calling nativo."""
from __future__ import annotations

import json
import logging

from app.config import config
from app.llm.base import LLM, Executor

log = logging.getLogger("llm.anthropic")


class AnthropicLLM(LLM):
    nome = "anthropic"

    def __init__(self) -> None:
        import anthropic

        self.cliente = anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=config.llm_timeout, max_retries=2)

    def gerar(self, system, mensagens, max_tokens=800, rapido=False) -> str:
        r = self.cliente.messages.create(
            model=config.llm_modelo_rapido if rapido else config.llm_modelo,
            max_tokens=max_tokens, system=system, messages=mensagens,
        )
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()

    def agente(self, system, mensagens, ferramentas, executar: Executor, max_passos=5):
        msgs = list(mensagens)
        chamadas: list[dict] = []
        for _ in range(max_passos):
            r = self.cliente.messages.create(
                model=config.llm_modelo, max_tokens=1200, system=system, messages=msgs, tools=ferramentas,
            )
            if r.stop_reason != "tool_use":
                return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip(), chamadas
            msgs.append({"role": "assistant", "content": r.content})
            resultados = []
            for b in r.content:
                if getattr(b, "type", "") != "tool_use":
                    continue
                saida = executar(b.name, dict(b.input or {}))
                chamadas.append({"nome": b.name, "args": b.input, "resultado": saida})
                resultados.append({
                    "type": "tool_result", "tool_use_id": b.id,
                    "content": json.dumps(saida, ensure_ascii=False, default=str)[:6000],
                    **({"is_error": True} if isinstance(saida, dict) and saida.get("erro") else {}),
                })
            msgs.append({"role": "user", "content": resultados})
        # estourou o limite de passos: pede uma resposta final sem ferramentas
        msgs.append({"role": "user", "content": "Responda agora ao cliente com o que já foi apurado."})
        r = self.cliente.messages.create(model=config.llm_modelo, max_tokens=800, system=system, messages=msgs, tools=ferramentas,
                                         tool_choice={"type": "none"})
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip(), chamadas
