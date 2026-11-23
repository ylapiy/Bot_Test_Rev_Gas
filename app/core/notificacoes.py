"""Avisos proativos de status do pedido (central de pedidos / app de entrega -> cliente).

O ERP RevGás (ou o app do entregador) chama POST /webhooks/erp/status quando o
pedido muda de situação; o bot avisa o cliente no mesmo canal em que ele pediu.
No WhatsApp, fora da janela de 24h, usa o template WHATSAPP_TEMPLATE_STATUS_PEDIDO.
"""
from __future__ import annotations

import logging
from datetime import datetime

from app import canais
from app.config import config
from app.core import memoria
from app.db import auditar, conexao
from app.erp import obter_erp

log = logging.getLogger("notificacoes")

MENSAGENS = {
    "em_separacao": "📦 Seu pedido *{numero}* está sendo separado na filial {filial}.",
    "em_rota": "🛵 Seu pedido *{numero}* saiu para entrega{com}! Previsão: até *{previsao}*.",
    "entregue": "✅ Pedido *{numero}* entregue. Obrigado pela preferência! Se tiver qualquer problema com o produto, é só me chamar.",
    "cancelado": "Seu pedido *{numero}* foi cancelado. Se você não pediu esse cancelamento, fale com a gente por aqui.",
}


def conversa_do_cliente(cliente_id) -> memoria.Conversa | None:
    with conexao() as con:
        r = con.execute("SELECT id FROM conversas WHERE CAST(json_extract(slots, '$.cliente.id') AS TEXT) = CAST(? AS TEXT) "
                        "ORDER BY atualizado_em DESC LIMIT 1", (cliente_id,)).fetchone()
    return memoria.obter_por_id(r["id"]) if r else None


def notificar_status(numero: str) -> dict:
    p = obter_erp().consultar_pedido(numero)
    if not p:
        return {"ok": False, "erro": "pedido não encontrado"}
    st = p["status"]
    if st not in MENSAGENS:
        return {"ok": True, "ignorado": f"status '{st}' não gera aviso"}
    conv = conversa_do_cliente(p["cliente_id"])
    if not conv:
        return {"ok": False, "erro": "cliente sem conversa registrada no bot"}
    if conv.canal == "evolution" and config.whatsapp_envio_real and config.erp_modo != "revgas":
        return {"ok": True, "ignorado": "avisos reais dependem do ERP RevGás"}
    try:
        previsao = datetime.strptime(p["previsao_entrega"][:19], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except (TypeError, ValueError, KeyError):
        previsao = "-"
    texto = MENSAGENS[st].format(numero=p["numero"], filial=p.get("filial_nome") or "", previsao=previsao,
                                 com=f" com {p['entregador']}" if p.get("entregador") else "")
    r = canais.enviar_proativo(conv, texto, [p["numero"], p["status_texto"]])
    memoria.registrar_mensagem(conv, "bot", texto, {"proativo": True, "status": st})
    if st in ("entregue", "cancelado") and conv.slots.get("pedido_atual") == p["numero"]:
        conv.slots.pop("pedido_atual", None)
    elif st not in ("entregue", "cancelado"):
        conv.slots["pedido_atual"] = p["numero"]
    memoria.salvar(conv)
    auditar("notificacao_status", {"pedido": numero, "status": st, "resultado": r}, conv.id)
    return {"ok": not (isinstance(r, dict) and r.get("erro")), "canal": conv.canal, "texto": texto, "envio": r}
