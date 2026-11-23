"""Estruturas de dados trocadas entre canais e o núcleo do bot."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MensagemEntrada:
    canal: str                      # whatsapp | evolution | simulador | cli
    usuario_id: str                 # telefone (WhatsApp) ou id do simulador
    texto: str = ""
    nome: str = ""
    tipo: str = "texto"             # texto | botao | localizacao | audio | imagem | outro
    botao_id: str | None = None
    localizacao: dict | None = None  # {latitude, longitude, endereco, nome}
    id_externo: str | None = None   # id da mensagem no WhatsApp (dedupe)


@dataclass
class Resposta:
    texto: str
    botoes: list[tuple[str, str]] = field(default_factory=list)  # (id, título até 20 caracteres)
    ia: bool = False  # texto gerado pela IA (passa pelos guardrails de saída mais rígidos)

    def como_dict(self) -> dict:
        return {"texto": self.texto, "botoes": [{"id": i, "titulo": t} for i, t in self.botoes]}


class Rastreio:
    """Registra cada técnica aplicada no turno (aparece no painel de debug do simulador)."""

    def __init__(self) -> None:
        self.etapas: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()
        self._ultimo = self._t0

    def add(self, tecnica: str, etapa: str, detalhe: Any = None) -> None:
        agora = time.perf_counter()
        self.etapas.append({
            "tecnica": tecnica,
            "etapa": etapa,
            "detalhe": detalhe,
            "ms": round((agora - self._ultimo) * 1000, 1),
        })
        self._ultimo = agora

    def total_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)


@dataclass
class ResultadoTurno:
    respostas: list[Resposta]
    rastreio: Rastreio
    intencao: str | None = None
    estado: str = "bot"
