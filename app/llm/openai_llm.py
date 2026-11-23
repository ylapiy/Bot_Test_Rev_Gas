"""Provedor OpenAI ou qualquer API compatível (/chat/completions com tools)."""
from __future__ import annotations

import json
import logging

import httpx

from app.config import config
from app.llm.base import LLM, Executor

log = logging.getLogger("llm.openai")


class OpenAICompativelLLM(LLM):
    nome = "openai"

    def __init__(self) -> None:
        h = {"Content-Type": "application/json"}
        if config.openai_api_key:
            h["Authorization"] = f"Bearer {config.openai_api_key}"
        self.http = httpx.Client(base_url=config.openai_base_url, headers=h, timeout=config.llm_timeout)

    def _chat(self, payload: dict) -> dict:
        r = self.http.post("/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def gerar(self, system, mensagens, max_tokens=800, rapido=False) -> str:
        msg = self._chat({
            "model": config.llm_modelo_rapido if rapido else config.llm_modelo,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *mensagens],
        })
        return (msg.get("content") or "").strip()

    def agente(self, system, mensagens, ferramentas, executar: Executor, max_passos=5):
        tools = [{"type": "function", "function": {"name": f["name"], "description": f["description"],
                                                    "parameters": f["input_schema"]}} for f in ferramentas]
        msgs = [{"role": "system", "content": system}, *mensagens]
        chamadas: list[dict] = []
        for _ in range(max_passos):
            msg = self._chat({"model": config.llm_modelo, "max_tokens": 1200, "messages": msgs, "tools": tools})
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip(), chamadas
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                saida = executar(c["function"]["name"], args)
                chamadas.append({"nome": c["function"]["name"], "args": args, "resultado": saida})
                msgs.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(saida, ensure_ascii=False, default=str)[:6000]})
        msgs.append({"role": "user", "content": "Responda agora ao cliente com o que já foi apurado."})
        msg = self._chat({"model": config.llm_modelo, "max_tokens": 800, "messages": msgs})
        return (msg.get("content") or "").strip(), chamadas
