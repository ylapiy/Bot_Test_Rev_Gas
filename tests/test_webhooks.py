"""Webhooks: WhatsApp oficial (formato Meta, assinatura, sandbox, deduplicação), ERP e painel."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import config


@pytest.fixture()
def cliente(bot):
    from app import main

    with TestClient(main.app) as c:
        yield c


def _payload(texto, de="5511999990001", wamid="wamid.T1", botao=None):
    m = {"from": de, "id": wamid, "timestamp": "1", "type": "text", "text": {"body": texto}}
    if botao:
        m = {"from": de, "id": wamid, "timestamp": "1", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": botao, "title": botao}}}
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "999"},
        "contacts": [{"profile": {"name": "Maria"}, "wa_id": de}], "messages": [m]}}]}]}


def _post(c, payload, segredo="segredo-app"):
    corpo = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return c.post("/webhooks/whatsapp", content=corpo, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def test_verificacao_do_webhook(cliente):
    ok = cliente.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verifica-123", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert cliente.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"}).status_code == 403


def test_mensagem_assinada_e_resposta_em_sandbox(cliente):
    r = _post(cliente, _payload("meu pedido chegou?"))
    assert r.status_code == 200
    saida = db.ler_saida("whatsapp_sandbox")
    assert saida and saida[0]["payload"]["to"] == "5511999990001"
    interativo = saida[0]["payload"]["interactive"]
    assert "PED-1003" in interativo["body"]["text"]
    assert any(b["reply"]["id"] == "menu" for b in interativo["action"]["buttons"])


def test_botoes_viram_mensagem_interativa(cliente):
    _post(cliente, _payload("quero um gás", wamid="wamid.B1"))
    p = db.ler_saida("whatsapp_sandbox")[0]["payload"]
    assert p["type"] == "interactive" and p["interactive"]["action"]["buttons"][0]["reply"]["id"] == "end_ok"
    _post(cliente, _payload("", wamid="wamid.B2", botao="end_ok"))
    p = db.ler_saida("whatsapp_sandbox")[0]["payload"]
    assert "pagar" in p["interactive"]["body"]["text"]


def test_assinatura_invalida_recusada(cliente):
    assert _post(cliente, _payload("oi"), segredo="errado").status_code == 401


def test_webhook_repetido_nao_e_processado_duas_vezes(cliente):
    _post(cliente, _payload("oi", wamid="wamid.DUP"))
    _post(cliente, _payload("oi", wamid="wamid.DUP"))
    assert len(db.ler_saida("whatsapp_sandbox")) == 1


def test_ponte_evolution_desligada_por_padrao(cliente):
    assert cliente.post("/webhooks/evolution", json={}).status_code == 404


def test_evolution_menu_sandbox(cliente, monkeypatch):
    monkeypatch.setattr(config, "whatsapp_provedor", "evolution")
    monkeypatch.setattr(config, "evolution_webhook_token", "tok-evolution")
    monkeypatch.setattr(config, "atendimento_modo", "auto")
    entrada = {"event": "messages.upsert", "data": {
        "key": {"remoteJid": "5511999990001@s.whatsapp.net", "id": "evo-menu"},
        "message": {"conversation": "menu"},
    }}
    assert cliente.post("/webhooks/evolution", json=entrada).status_code == 401
    cabecalho = {"X-Webhook-Token": "tok-evolution"}
    assert cliente.post("/webhooks/evolution", json=entrada, headers=cabecalho).status_code == 200
    saida = db.ler_saida("whatsapp_sandbox")[0]["payload"]["evolution"]
    assert saida["number"] == "5511999990001"
    assert "*1.* Fazer pedido" in saida["text"]


def test_webhook_do_erp_exige_token_e_avisa_cliente(cliente):
    cliente.post("/api/simulador/mensagem", json={"usuario_id": "5511999990001", "texto": "oi"})
    assert cliente.post("/webhooks/erp/status", json={"numero_pedido": "PED-1003", "status": "entregue"}).status_code == 401
    r = cliente.post("/webhooks/erp/status", json={"numero_pedido": "PED-1003", "status": "entregue"},
                     headers={"X-Webhook-Token": "tok-erp"})
    assert r.json()["ok"] and "entregue" in r.json()["texto"]
    ev = cliente.get("/api/simulador/eventos", params={"usuario_id": "5511999990001"}).json()["eventos"]
    assert "entregue" in ev[-1]["payload"]["texto"]


def test_painel_do_atendente(cliente):
    cliente.post("/api/simulador/mensagem", json={"usuario_id": "5511999990002", "texto": "quero falar com um atendente"})
    assert cliente.get("/api/atendente/handoffs").status_code == 401
    h = {"X-Painel-Senha": "senha-teste"}
    lista = cliente.get("/api/atendente/handoffs", headers=h).json()["handoffs"]
    assert lista and lista[0]["telefone_mascarado"].endswith("0002") and "Padaria" in lista[0]["resumo"]
    hid = lista[0]["id"]
    assert cliente.post(f"/api/atendente/handoffs/{hid}/responder", json={"atendente": "Ana", "texto": "Olá!"}, headers=h).json()["ok"]
    r = cliente.post("/api/simulador/mensagem", json={"usuario_id": "5511999990002", "texto": "oi Ana"}).json()
    assert r["respostas"] == [] and r["estado"] == "humano"
    cliente.post(f"/api/atendente/handoffs/{hid}/encerrar", json={"atendente": "Ana"}, headers=h)
    r = cliente.post("/api/simulador/mensagem", json={"usuario_id": "5511999990002", "texto": "meus boletos"}).json()
    assert r["estado"] == "bot" and r["respostas"]
