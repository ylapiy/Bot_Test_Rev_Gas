"""Canal oficial: WhatsApp Cloud API (Meta).

Recebe:  GET  /webhooks/whatsapp  (verificação do webhook)
         POST /webhooks/whatsapp  (mensagens e status; assinatura X-Hub-Signature-256)
Envia:   POST https://graph.facebook.com/{versão}/{PHONE_NUMBER_ID}/messages

Modo sandbox (WHATSAPP_ENVIO_REAL=false): tudo funciona igual, mas as respostas
NÃO vão para a Meta — ficam na aba "Saída WhatsApp (sandbox)" do simulador.
Assim dá para testar o webhook (inclusive com payloads reais) sem risco.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from app.config import config
from app.core.modelos import MensagemEntrada, Resposta
from app.db import auditar, enfileirar_saida

log = logging.getLogger("whatsapp")

# Erros mais comuns da Cloud API e o que fazer (aparecem no log e no /diagnostico)
ERROS = {
    190: "Token expirado/inválido. Gere um token PERMANENTE de Usuário do Sistema (System User) no Business Manager.",
    200: "Sem permissão. O token precisa das permissões whatsapp_business_messaging e whatsapp_business_management.",
    10: "Permissão negada para este recurso. Confira as permissões do token e se o app está vinculado à conta WhatsApp (WABA).",
    100: "Parâmetro inválido (ex.: PHONE_NUMBER_ID errado ou número do destinatário fora do formato 55DDDNÚMERO).",
    131030: "Número de destino não está na lista de teste. Com o número de TESTE da Meta só dá para enviar para até 5 números cadastrados em 'API Setup'.",
    131047: "Passaram 24h desde a última mensagem do cliente. Só é possível enviar MODELO (template) aprovado.",
    131026: "Mensagem não entregue: o número não tem WhatsApp ou o app do cliente está desatualizado.",
    131042: "Problema de pagamento da conta WhatsApp Business: cadastre/regularize a forma de pagamento no Business Manager.",
    131045: "Erro de registro do número: registre o número na Cloud API (endpoint /register com PIN de 6 dígitos).",
    131056: "Muitas mensagens para o mesmo número em pouco tempo. Aguarde.",
    130429: "Limite de envio (throughput) atingido. Aguarde e tente novamente.",
    132001: "Modelo (template) não existe ou não está aprovado no idioma informado.",
    133010: "Número não registrado na Cloud API. Faça o registro do número (POST /{PHONE_NUMBER_ID}/register).",
    368: "Conta temporariamente bloqueada por violação de política. Veja o Business Manager.",
    131031: "Conta bloqueada. Verifique o status no Business Manager.",
}


def explicar_erro(corpo: dict) -> str:
    err = (corpo or {}).get("error", {})
    cod = err.get("code")
    return f"[{cod}] {err.get('message', '')} → {ERROS.get(cod, 'Consulte o código na documentação da Meta.')}"


class WhatsAppCloud:
    nome = "whatsapp"
    suporta_botoes = True

    def __init__(self) -> None:
        self.base = f"https://graph.facebook.com/{config.whatsapp_api_versao}/{config.whatsapp_phone_number_id}"

    # ------------------------------------------------------------ webhook
    @staticmethod
    def verificar_webhook(mode: str | None, token: str | None, challenge: str | None) -> str | None:
        if mode == "subscribe" and token and config.whatsapp_verify_token and hmac.compare_digest(token, config.whatsapp_verify_token):
            return challenge
        return None

    @staticmethod
    def assinatura_valida(corpo: bytes, cabecalho: str | None) -> bool:
        if not config.whatsapp_app_secret:
            log.warning("WHATSAPP_APP_SECRET vazio: assinatura do webhook NÃO está sendo verificada (configure em produção!)")
            return True
        if not cabecalho or not cabecalho.startswith("sha256="):
            return False
        esperado = hmac.new(config.whatsapp_app_secret.encode(), corpo, hashlib.sha256).hexdigest()
        return hmac.compare_digest(esperado, cabecalho[7:])

    @staticmethod
    def extrair(payload: dict) -> tuple[list[MensagemEntrada], list[dict]]:
        msgs: list[MensagemEntrada] = []
        status: list[dict] = []
        for entry in payload.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                pnid = (v.get("metadata") or {}).get("phone_number_id")
                if config.whatsapp_phone_number_id and pnid and pnid != config.whatsapp_phone_number_id:
                    continue  # mensagem de outro número da mesma conta
                nomes = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts", [])}
                for m in v.get("messages", []):
                    tipo = m.get("type")
                    base = dict(canal="whatsapp", usuario_id=m.get("from", ""), nome=nomes.get(m.get("from"), ""),
                                id_externo=m.get("id"))
                    if tipo == "text":
                        msgs.append(MensagemEntrada(texto=(m.get("text") or {}).get("body", ""), **base))
                    elif tipo == "interactive":
                        it = m.get("interactive") or {}
                        r = it.get("button_reply") or it.get("list_reply") or {}
                        msgs.append(MensagemEntrada(tipo="botao", botao_id=r.get("id"), texto=r.get("title", ""), **base))
                    elif tipo == "button":  # resposta rápida de template
                        b = m.get("button") or {}
                        msgs.append(MensagemEntrada(tipo="botao", botao_id=b.get("payload"), texto=b.get("text", ""), **base))
                    elif tipo == "location":
                        loc = m.get("location") or {}
                        msgs.append(MensagemEntrada(tipo="localizacao", localizacao={
                            "latitude": loc.get("latitude"), "longitude": loc.get("longitude"),
                            "nome": loc.get("name"), "endereco": loc.get("address")}, **base))
                    elif tipo == "image":
                        msgs.append(MensagemEntrada(tipo="imagem", texto=(m.get("image") or {}).get("caption", ""), **base))
                    elif tipo in ("audio", "video", "document", "sticker"):
                        mapa = {"audio": "audio", "video": "video", "document": "documento", "sticker": "figurinha"}
                        msgs.append(MensagemEntrada(tipo=mapa[tipo], **base))
                    else:
                        msgs.append(MensagemEntrada(tipo="outro", **base))
                for s in v.get("statuses", []):
                    status.append(s)
        return msgs, status

    # ------------------------------------------------------------ envio
    def _payload(self, para: str, r: Resposta) -> list[dict]:
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": para}
        if not r.botoes:
            return [{**base, "type": "text", "text": {"preview_url": False, "body": r.texto[:4096]}}]
        corpo = r.texto
        envios = []
        if len(corpo) > 1024:  # limite do corpo de mensagem interativa
            envios.append({**base, "type": "text", "text": {"preview_url": False, "body": corpo[:4096]}})
            corpo = "Escolha uma opção:"
        envios.append({**base, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": corpo},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": t[:20]}} for i, t in r.botoes[:3]]}}})
        return envios

    def _post(self, payload: dict) -> dict:
        if not config.whatsapp_envio_real:
            enfileirar_saida("whatsapp_sandbox", payload.get("to", ""), payload)
            log.info("[sandbox] mensagem para %s NÃO enviada à Meta (WHATSAPP_ENVIO_REAL=false)", payload.get("to"))
            return {"sandbox": True}
        try:
            r = httpx.post(f"{self.base}/messages", json=payload, timeout=15,
                           headers={"Authorization": f"Bearer {config.whatsapp_token}"})
        except httpx.HTTPError as e:
            log.error("Falha de rede ao enviar WhatsApp: %s", e)
            return {"erro": str(e)}
        corpo = r.json() if r.content else {}
        if r.status_code >= 400:
            explicacao = explicar_erro(corpo)
            log.error("WhatsApp recusou o envio: %s", explicacao)
            auditar("whatsapp_erro_envio", {"status": r.status_code, "erro": explicacao})
            return {"erro": explicacao, "codigo": (corpo.get("error") or {}).get("code")}
        return corpo

    def enviar(self, usuario_id: str, resposta: Resposta) -> list[dict]:
        return [self._post(p) for p in self._payload(usuario_id, resposta)]

    def enviar_template(self, usuario_id: str, nome: str, parametros: list[str], idioma: str | None = None) -> dict:
        return self._post({
            "messaging_product": "whatsapp", "to": usuario_id, "type": "template",
            "template": {"name": nome, "language": {"code": idioma or config.whatsapp_template_idioma},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in parametros]}]}})

    def marcar_lido(self, message_id: str | None) -> None:
        if not message_id or not config.whatsapp_marcar_lido or not config.whatsapp_envio_real:
            return
        try:
            httpx.post(f"{self.base}/messages", timeout=10, headers={"Authorization": f"Bearer {config.whatsapp_token}"},
                       json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
        except httpx.HTTPError:
            pass

    # ------------------------------------------------------------ diagnóstico
    def diagnosticar(self) -> dict:
        """Consulta o número na Graph API para validar token e PHONE_NUMBER_ID."""
        faltando = [n for n, v in {"WHATSAPP_TOKEN": config.whatsapp_token, "WHATSAPP_PHONE_NUMBER_ID": config.whatsapp_phone_number_id,
                                   "WHATSAPP_VERIFY_TOKEN": config.whatsapp_verify_token}.items() if not v]
        if faltando:
            return {"ok": False, "faltando": faltando}
        try:
            r = httpx.get(f"https://graph.facebook.com/{config.whatsapp_api_versao}/{config.whatsapp_phone_number_id}",
                          params={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"},
                          headers={"Authorization": f"Bearer {config.whatsapp_token}"}, timeout=15)
        except httpx.HTTPError as e:
            return {"ok": False, "erro": f"Sem conexão com graph.facebook.com: {e}"}
        corpo = r.json() if r.content else {}
        if r.status_code >= 400:
            return {"ok": False, "erro": explicar_erro(corpo)}
        return {"ok": True, "numero": corpo}
