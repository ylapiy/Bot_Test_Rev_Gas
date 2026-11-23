"""Despacho de mensagens para o canal certo (WhatsApp oficial, Evolution, simulador, terminal)."""
from __future__ import annotations

import logging
from datetime import datetime

from app.config import config
from app.core.modelos import Resposta
from app.db import agora, enfileirar_saida

log = logging.getLogger("canais")

CANAIS_SEM_BOTOES = {"evolution"}


class Simulador:
    nome = "simulador"
    suporta_botoes = True

    def enviar(self, usuario_id: str, resposta: Resposta) -> dict:
        enfileirar_saida("simulador", usuario_id, resposta.como_dict())
        return {"ok": True}


class Terminal(Simulador):
    nome = "cli"

    def enviar(self, usuario_id: str, resposta: Resposta) -> dict:
        enfileirar_saida("cli", usuario_id, resposta.como_dict())
        return {"ok": True}


_cache: dict = {}


def obter_canal(nome: str):
    if nome not in _cache:
        if nome == "whatsapp":
            from app.canais.whatsapp_cloud import WhatsAppCloud

            _cache[nome] = WhatsAppCloud()
        elif nome == "evolution":
            from app.canais.evolution import Evolution

            _cache[nome] = Evolution()
        elif nome == "cli":
            _cache[nome] = Terminal()
        else:
            _cache[nome] = Simulador()
    return _cache[nome]


def enviar(canal: str, usuario_id: str, resposta: Resposta):
    return obter_canal(canal).enviar(usuario_id, resposta)


def fora_da_janela_24h(ultima_msg_cliente: str | None) -> bool:
    """WhatsApp: depois de 24h sem mensagem do cliente, só template aprovado."""
    if not ultima_msg_cliente:
        return True
    try:
        d = datetime.strptime(ultima_msg_cliente, "%Y-%m-%d %H:%M:%S").replace(tzinfo=agora().tzinfo)
    except ValueError:
        return True
    return (agora() - d).total_seconds() > 23.5 * 3600


def enviar_proativo(conv, texto: str, parametros_template: list[str] | None = None) -> dict:
    """Mensagem iniciada pela empresa (ex.: 'seu pedido saiu para entrega')."""
    if conv.canal == "whatsapp" and fora_da_janela_24h(conv.ultima_msg_cliente):
        if not config.whatsapp_template_status:
            log.warning("Fora da janela de 24h e sem WHATSAPP_TEMPLATE_STATUS_PEDIDO: aviso não enviado a %s", conv.usuario_id)
            return {"erro": "fora_da_janela_24h_sem_template"}
        return obter_canal("whatsapp").enviar_template(conv.usuario_id, config.whatsapp_template_status, parametros_template or [texto])
    return enviar(conv.canal, conv.usuario_id, Resposta(texto))
