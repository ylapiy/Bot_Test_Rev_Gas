"""Estado de um turno de conversa (uma mensagem do cliente sendo processada)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.ferramentas import Contexto
from app.core.memoria import Conversa
from app.core.modelos import MensagemEntrada, Rastreio, Resposta

NOMES_PRODUTO = {
    "P13": "Gás P13 (13 kg)", "P13C": "Gás P13 completo (com vasilhame)", "P45": "Gás P45 (45 kg)",
    "P20": "Gás P20 empilhadeira", "P5": "Gás P5 (5 kg)", "AGUA20": "Água mineral 20 L", "KITREG": "Kit registro + mangueira",
}


@dataclass
class Turno:
    msg: MensagemEntrada
    conv: Conversa
    ctx: Contexto
    texto: str
    texto_exp: str = ""
    ent: dict = field(default_factory=dict)
    cls: dict = field(default_factory=dict)
    respostas: list[Resposta] = field(default_factory=list)
    gerado_por_ia: bool = False

    @property
    def rastreio(self) -> Rastreio:
        return self.ctx.rastreio

    @property
    def botao(self) -> str | None:
        return self.msg.botao_id

    @property
    def intencao(self) -> str:
        return self.cls.get("intencao", "fora_escopo")

    def responder(self, texto: str, botoes: list[tuple[str, str]] | None = None, ia: bool = False) -> None:
        self.respostas.append(Resposta(texto, [(i, t[:20]) for i, t in (botoes or [])][:3], ia))

    def primeiro_nome(self) -> str:
        cli = self.conv.cliente or {}
        nome = (cli.get("nome") or self.conv.nome or "").strip()
        if not nome or cli.get("tipo") == "PJ":
            return ""
        return nome.split()[0].title()
