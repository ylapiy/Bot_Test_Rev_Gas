"""Canal ALTERNATIVO e NÃO OFICIAL: Evolution API (WhatsApp Web / Baileys).

⚠️ Use só como ponte TEMPORÁRIA e só com aprovação da empresa:
   - não é uma API da Meta; viola os Termos de Uso do WhatsApp;
   - o número pode ser BANIDO (use um chip de teste, nunca o número principal);
   - botões não funcionam: o bot envia opções numeradas (1, 2, 3).
Vem DESLIGADO. Para ligar: WHATSAPP_PROVEDOR=evolution (ver README, seção "Ponte temporária").
"""
from __future__ import annotations

import hmac
import logging

import httpx

from app.config import config
from app.core.modelos import MensagemEntrada, Resposta
from app.db import enfileirar_saida

log = logging.getLogger("evolution")


class Evolution:
    nome = "evolution"
    suporta_botoes = False

    @staticmethod
    def token_valido(token: str | None) -> bool:
        if not config.evolution_webhook_token:
            log.warning("EVOLUTION_WEBHOOK_TOKEN vazio: webhook sem autenticação")
            return True
        return bool(token) and hmac.compare_digest(token, config.evolution_webhook_token)

    @staticmethod
    def extrair(payload: dict) -> list[MensagemEntrada]:
        evento = str(payload.get("event", "")).lower().replace("_", ".")
        if evento != "messages.upsert":
            return []
        dados = payload.get("data") or {}
        itens = dados if isinstance(dados, list) else [dados]
        out = []
        for d in itens:
            key = d.get("key") or {}
            jid = key.get("remoteJid", "")
            if key.get("fromMe") or not jid.endswith("@s.whatsapp.net"):
                continue  # ignora mensagens próprias, grupos e status
            numero = jid.split("@")[0]
            m = d.get("message") or {}
            base = dict(canal="evolution", usuario_id=numero, nome=d.get("pushName", ""), id_externo=key.get("id"))
            if "conversation" in m or "extendedTextMessage" in m:
                texto = m.get("conversation") or (m.get("extendedTextMessage") or {}).get("text", "")
                out.append(MensagemEntrada(texto=texto, **base))
            elif "locationMessage" in m:
                loc = m["locationMessage"]
                out.append(MensagemEntrada(tipo="localizacao", localizacao={
                    "latitude": loc.get("degreesLatitude"), "longitude": loc.get("degreesLongitude"),
                    "nome": loc.get("name"), "endereco": loc.get("address")}, **base))
            elif "imageMessage" in m:
                out.append(MensagemEntrada(tipo="imagem", texto=(m["imageMessage"] or {}).get("caption", ""), **base))
            elif "audioMessage" in m:
                out.append(MensagemEntrada(tipo="audio", **base))
            else:
                out.append(MensagemEntrada(tipo="outro", **base))
        return out

    def enviar(self, usuario_id: str, resposta: Resposta) -> dict:
        # botões já chegam convertidos em opções numeradas pelo orquestrador
        payload = {"number": usuario_id, "text": resposta.texto}
        if not config.whatsapp_envio_real:
            enfileirar_saida("whatsapp_sandbox", usuario_id, {"evolution": payload})
            return {"sandbox": True}
        try:
            r = httpx.post(f"{config.evolution_url}/message/sendText/{config.evolution_instancia}", json=payload,
                           headers={"apikey": config.evolution_api_key}, timeout=15)
            if r.status_code >= 400:
                log.error("Evolution recusou o envio (%s): %s", r.status_code, r.text[:300])
                return {"erro": r.text[:300]}
            return r.json() if r.content else {}
        except httpx.HTTPError as e:
            log.error("Falha ao enviar pela Evolution: %s", e)
            return {"erro": str(e)}
