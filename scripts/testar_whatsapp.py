"""Teste direto da API oficial do WhatsApp (sem o bot no meio).

    python scripts/testar_whatsapp.py --diagnostico
        Confere token + PHONE_NUMBER_ID na Meta e mostra o número conectado.

    python scripts/testar_whatsapp.py --para 5511999999999 --template hello_world --idioma en_US
        Envia o template de teste da Meta (funciona mesmo sem conversa aberta).

    python scripts/testar_whatsapp.py --para 5511999999999 --texto "teste"
        Envia texto livre (só funciona se o número te mandou mensagem nas últimas 24h).

    python scripts/testar_whatsapp.py --assinar-waba
        Causa nº 1 de "o webhook não recebe mensagens": o app não está inscrito na conta
        WhatsApp (WABA). Mostra as inscrições e inscreve o app (POST /{WABA_ID}/subscribed_apps).

Ignora WHATSAPP_ENVIO_REAL: este script SEMPRE envia de verdade.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.canais.whatsapp_cloud import WhatsAppCloud, explicar_erro  # noqa: E402
from app.config import config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnostico", action="store_true")
    ap.add_argument("--para")
    ap.add_argument("--texto")
    ap.add_argument("--template")
    ap.add_argument("--idioma", default="pt_BR")
    ap.add_argument("--assinar-waba", action="store_true")
    a = ap.parse_args()

    if a.assinar_waba:
        if not config.whatsapp_waba_id:
            print("❌ Preencha WHATSAPP_BUSINESS_ACCOUNT_ID no .env")
            return
        url = f"https://graph.facebook.com/{config.whatsapp_api_versao}/{config.whatsapp_waba_id}/subscribed_apps"
        h = {"Authorization": f"Bearer {config.whatsapp_token}"}
        antes = httpx.get(url, headers=h, timeout=20)
        print("Apps inscritos antes:", antes.json() if antes.status_code < 400 else explicar_erro(antes.json()))
        r = httpx.post(url, headers=h, timeout=20)
        print("✅ App inscrito na WABA:" if r.status_code < 400 else "❌", r.json() if r.status_code < 400 else explicar_erro(r.json()))
        return

    if a.diagnostico or not a.para:
        print(json.dumps(WhatsAppCloud().diagnosticar(), ensure_ascii=False, indent=2))
        return
    url = f"https://graph.facebook.com/{config.whatsapp_api_versao}/{config.whatsapp_phone_number_id}/messages"
    if a.template:
        corpo = {"messaging_product": "whatsapp", "to": a.para, "type": "template",
                 "template": {"name": a.template, "language": {"code": a.idioma}}}
    else:
        corpo = {"messaging_product": "whatsapp", "to": a.para, "type": "text", "text": {"body": a.texto or "Teste do chatbot ✅"}}
    r = httpx.post(url, json=corpo, headers={"Authorization": f"Bearer {config.whatsapp_token}"}, timeout=20)
    if r.status_code >= 400:
        print("❌", explicar_erro(r.json()))
    else:
        print("✅ Enviado:", r.json())


if __name__ == "__main__":
    main()
