"""Human Handoff.

Quando transfere:
  - cliente pede atendente | emergência (vazamento) | reclamação
  - bot não entendeu N vezes seguidas | pedido de desconto/condição especial aceito
  - erro do ERP em ação importante | IA decide (ferramenta solicitar_atendente)
O que leva para o atendente:
  - motivo, prioridade, resumo da conversa, dados coletados (mascarados),
    últimas mensagens — no painel /atendente e (opcional) no HANDOFF_WEBHOOK_URL
Enquanto a conversa está com humano, o bot fica em silêncio.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

import httpx

from app.config import config
from app.core import memoria
from app.core.memoria import Conversa
from app.db import agora, agora_iso, auditar, conexao
from app.util.documentos import mascarar_documento, mascarar_pii_texto, mascarar_telefone

log = logging.getLogger("handoff")
DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
ROTULOS = {
    "emergencia_vazamento": "EMERGÊNCIA/vazamento", "fazer_pedido": "pedido", "consultar_preco": "preço",
    "consultar_estoque": "estoque", "status_pedido": "status do pedido", "cancelar_pedido": "cancelamento",
    "boleto": "boleto/financeiro", "troca": "troca/devolução", "consulta_historico": "histórico",
    "duvida_informacao": "dúvida", "pedir_desconto": "pedido de desconto", "reclamacao": "reclamação",
    "falar_atendente": "pediu atendente",
}


def dentro_do_horario(momento: datetime | None = None) -> bool:
    m = momento or agora()
    dias = [d.strip() for d in config.dias_atendimento_humano.split(",")]
    if DIAS[m.weekday()] not in dias:
        return False
    hm = m.strftime("%H:%M")
    return config.horario_humano_inicio <= hm < config.horario_humano_fim


def dados_coletados(conv: Conversa) -> dict:
    s = conv.slots
    cli = s.get("cliente") or {}
    return {
        "cliente": {"nome": cli.get("nome"), "documento": mascarar_documento(cli.get("documento")),
                    "telefone": mascarar_telefone(conv.usuario_id), "id_erp": cli.get("id"),
                    "verificado": conv.nivel >= 2} if cli else {"telefone": mascarar_telefone(conv.usuario_id), "identificado": False},
        "pedido_atual": s.get("pedido_atual"),
        "produto_em_contexto": s.get("produto"),
        "carrinho": (s.get("fluxo") or {}).get("itens") if (s.get("fluxo") or {}).get("nome") == "pedido" else None,
        "ultima_intencao": s.get("ultima_intencao"),
        "intencoes_da_conversa": [ROTULOS.get(i, i) for i in dict.fromkeys(s.get("historico_intencoes", [])[-8:])],
        "canal": conv.canal,
    }


def resumo_regras(conv: Conversa, motivo: str) -> str:
    d = dados_coletados(conv)
    cli = d["cliente"]
    partes = [f"Motivo: {motivo}."]
    if cli.get("nome"):
        partes.append(f"Cliente: {cli['nome']} ({'verificado' if cli.get('verificado') else 'não verificado'}).")
    else:
        partes.append("Cliente não identificado.")
    if d["intencoes_da_conversa"]:
        partes.append("Assuntos: " + ", ".join(d["intencoes_da_conversa"]) + ".")
    if d["pedido_atual"]:
        partes.append(f"Pedido em contexto: {d['pedido_atual']}.")
    if d["carrinho"]:
        partes.append("Carrinho: " + ", ".join(f"{i['quantidade']}x {i['produto_codigo']}" for i in d["carrinho"]) + ".")
    ult = [m for m in memoria.historico(conv, 6) if m["papel"] == "cliente"][-2:]
    if ult:
        partes.append("Últimas mensagens do cliente: " + " | ".join(f"\"{m['texto'][:120]}\"" for m in ult))
    return mascarar_pii_texto(" ".join(partes))


def resumo_ia(conv: Conversa, motivo: str, llm) -> str | None:
    try:
        txt = llm.gerar(
            "Você resume atendimentos de WhatsApp de uma revenda de gás para o atendente humano que vai assumir. "
            "Seja objetivo: 3 a 5 linhas com o que o cliente quer, o que já foi feito e o que falta. Não invente.",
            [{"role": "user", "content": f"Motivo da transferência: {motivo}\n\nConversa:\n{memoria.historico_texto(conv, 16)}"}],
            max_tokens=300, rapido=True)
        return mascarar_pii_texto(txt) if txt else None
    except Exception as e:  # noqa: BLE001
        log.warning("Resumo por IA falhou: %s", e)
        return None


def _notificar_webhook(payload: dict) -> None:
    if not config.handoff_webhook_url:
        return

    def enviar():
        try:
            httpx.post(config.handoff_webhook_url, json=payload, timeout=10)
        except httpx.HTTPError as e:
            log.error("Falha ao notificar handoff (%s): %s", config.handoff_webhook_url, e)

    threading.Thread(target=enviar, daemon=True).start()


def transferir(conv: Conversa, motivo: str, prioridade: str = "normal", llm=None) -> dict:
    # já existe um handoff aberto? só atualiza a prioridade
    with conexao() as con:
        aberto = con.execute("SELECT * FROM handoffs WHERE conversa_id=? AND status!='encerrado' ORDER BY id DESC LIMIT 1",
                             (conv.id,)).fetchone()
    resumo = (resumo_ia(conv, motivo, llm) if llm else None) or resumo_regras(conv, motivo)
    dados = dados_coletados(conv)
    dados["ultimas_mensagens"] = [{"papel": m["papel"], "texto": mascarar_pii_texto(m["texto"]), "em": m["criado_em"]}
                                  for m in memoria.historico(conv, 10)]
    h = agora_iso()
    with conexao() as con:
        if aberto:
            hid = aberto["id"]
            prio = "urgente" if "urgente" in (prioridade, aberto["prioridade"]) else prioridade
            con.execute("UPDATE handoffs SET prioridade=?, resumo=?, dados=?, motivo=?, atualizado_em=? WHERE id=?",
                        (prio, resumo, json.dumps(dados, ensure_ascii=False), motivo, h, hid))
        else:
            cur = con.execute(
                "INSERT INTO handoffs (conversa_id, motivo, prioridade, resumo, dados, status, criado_em, atualizado_em) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (conv.id, motivo, prioridade, resumo, json.dumps(dados, ensure_ascii=False), "aberto", h, h))
            hid = cur.lastrowid
    # fora do horário ninguém vai responder: o chamado fica aberto e o bot continua atendendo
    conv.estado = "humano" if dentro_do_horario() else "bot"
    auditar("handoff", {"motivo": motivo, "prioridade": prioridade, "handoff_id": hid}, conv.id)
    payload = {"evento": "handoff", "handoff_id": hid, "conversa_id": conv.id, "canal": conv.canal, "motivo": motivo,
               "prioridade": prioridade, "resumo": resumo, "dados": dados, "criado_em": h,
               "painel": "/atendente"}
    _notificar_webhook(payload)
    return payload


def mensagem_ao_cliente(prioridade: str) -> str:
    if prioridade == "urgente" and dentro_do_horario():
        return "Nossa equipe foi acionada com prioridade máxima e vai falar com você em instantes."
    if dentro_do_horario():
        return ("Pronto! Já passei sua conversa para um atendente, com o resumo do que conversamos — "
                "você não vai precisar repetir nada. Em instantes alguém continua por aqui. 🙂")
    return (f"Passei sua conversa para a nossa equipe, com o resumo do que conversamos. Nosso atendimento humano funciona "
            f"das {config.horario_humano_inicio} às {config.horario_humano_fim} ({config.dias_atendimento_humano.replace(',', ', ')}). "
            "Assim que abrirmos, alguém continua daqui. Pedidos de gás e água você pode fazer comigo a qualquer hora.")


# ------------------------------------------------------------------ painel do atendente
def listar(status: str | None = None) -> list[dict]:
    sql = ("SELECT h.*, c.canal, c.usuario_id, c.nome FROM handoffs h JOIN conversas c ON c.id=h.conversa_id ")
    params: tuple = ()
    if status:
        sql += "WHERE h.status=? "
        params = (status,)
    sql += ("ORDER BY CASE h.prioridade WHEN 'urgente' THEN 0 WHEN 'alta' THEN 1 ELSE 2 END, "
            "CASE h.status WHEN 'encerrado' THEN 1 ELSE 0 END, h.id DESC LIMIT 100")
    with conexao() as con:
        rows = con.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["dados"] = json.loads(d["dados"] or "{}")
        d["telefone_mascarado"] = mascarar_telefone(d.pop("usuario_id"))
        out.append(d)
    return out


def ativo(conversa_id: int) -> dict | None:
    with conexao() as con:
        r = con.execute("SELECT * FROM handoffs WHERE conversa_id=? AND status!='encerrado' ORDER BY id DESC LIMIT 1",
                        (conversa_id,)).fetchone()
    return dict(r) if r else None


def minutos_desde(ts: str) -> float:
    try:
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=agora().tzinfo)
        return (agora() - d).total_seconds() / 60
    except ValueError:
        return 0.0


def obter(hid: int) -> dict | None:
    with conexao() as con:
        r = con.execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()
    return dict(r) if r else None


def atualizar_status(hid: int, status: str, atendente: str | None = None) -> None:
    with conexao() as con:
        con.execute("UPDATE handoffs SET status=?, atendente=COALESCE(?, atendente), atualizado_em=? WHERE id=?",
                    (status, atendente, agora_iso(), hid))
