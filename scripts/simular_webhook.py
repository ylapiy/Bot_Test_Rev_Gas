"""Envia ao bot uma mensagem NO FORMATO EXATO da Meta (WhatsApp Cloud API),
como se viesse do WhatsApp. Testa o caminho completo do webhook sem precisar da Meta.

Exemplos (com o servidor rodando em outro terminal):
    python scripts/simular_webhook.py "quero um gás"
    python scripts/simular_webhook.py "meu pedido chegou?" --de 5511999990001
    python scripts/simular_webhook.py --botao confirmar_pedido --titulo "Confirmar pedido"
    python scripts/simular_webhook.py --verificar          # testa a verificação (GET) do webhook

Se WHATSAPP_APP_SECRET estiver no .env, a mensagem vai assinada (X-Hub-Signature-256)
igual a Meta faz. Com WHATSAPP_ENVIO_REAL=false as respostas aparecem aqui e na aba
"Saída WhatsApp (sandbox)" do simulador.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import config  # noqa: E402


def payload(de: str, nome: str, texto: str | None, botao: str | None, titulo: str | None) -> dict:
    msg = {"from": de, "id": f"wamid.SIMULADO{uuid.uuid4().hex[:20]}", "timestamp": str(int(time.time()))}
    if botao:
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": botao, "title": titulo or botao}})
    else:
        msg.update(type="text", text={"body": texto or ""})
    return {"object": "whatsapp_business_account", "entry": [{"id": config.whatsapp_waba_id or "WABA_ID", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "5511900000000", "phone_number_id": config.whatsapp_phone_number_id or "PHONE_NUMBER_ID"},
            "contacts": [{"profile": {"name": nome}, "wa_id": de}], "messages": [msg]}}]}]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("texto", nargs="?", default="oi")
    ap.add_argument("--de", default="5511999990001", help="telefone do cliente (55 + DDD + número)")
    ap.add_argument("--nome", default="Cliente Teste")
    ap.add_argument("--botao", help="id do botão clicado (ex.: confirmar_pedido)")
    ap.add_argument("--titulo", help="título do botão")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--verificar", action="store_true", help="testa o GET de verificação do webhook")
    a = ap.parse_args()

    if a.verificar:
        r = httpx.get(f"{a.url}/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": config.whatsapp_verify_token,
                                                            "hub.challenge": "12345"})
        print("✅ Verificação OK" if r.status_code == 200 and r.text == "12345" else f"❌ Falhou: {r.status_code} {r.text}")
        return

    corpo = json.dumps(payload(a.de, a.nome, a.texto, a.botao, a.titulo)).encode()
    headers = {"Content-Type": "application/json"}
    if config.whatsapp_app_secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(config.whatsapp_app_secret.encode(), corpo, hashlib.sha256).hexdigest()
    try:
        antes = httpx.get(f"{a.url}/api/simulador/whatsapp-sandbox").json().get("mensagens", [])
        ultimo = max([m["id"] for m in antes], default=0)
    except Exception:  # noqa: BLE001
        ultimo = 0
    r = httpx.post(f"{a.url}/webhooks/whatsapp", content=corpo, headers=headers)
    print(f"Webhook respondeu {r.status_code}: {r.text}")
    if r.status_code != 200:
        return
    if config.whatsapp_envio_real:
        print("Envio real ligado: a resposta foi para o WhatsApp de verdade.")
        return
    time.sleep(1.0)
    novas = httpx.get(f"{a.url}/api/simulador/whatsapp-sandbox", params={"apos": ultimo}).json().get("mensagens", [])
    for m in sorted(novas, key=lambda x: x["id"]):
        p = m["payload"]
        if p.get("type") == "interactive":
            i = p["interactive"]
            print(f"\n🤖 {i['body']['text']}")
            print("   " + "  ".join(f"[{b['reply']['id']}: {b['reply']['title']}]" for b in i["action"]["buttons"]))
        elif p.get("type") == "text":
            print(f"\n🤖 {p['text']['body']}")
        else:
            print("\n🤖", json.dumps(p, ensure_ascii=False))


if __name__ == "__main__":
    main()
