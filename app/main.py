"""Servidor HTTP do bot (FastAPI).

Rotas principais
  /simulador                  conversa estilo WhatsApp + painel com cada técnica aplicada
  /atendente                  painel do atendente humano (handoff)
  /webhooks/whatsapp          webhook da API oficial (Meta)
  /webhooks/evolution         webhook da ponte não oficial (opcional, desligada)
  /webhooks/erp/status        ERP/app de entrega avisam mudança de status do pedido
  /saude  /diagnostico        verificação de configuração

Rodar:  uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from app import canais, db
from app.config import config
from app.core import handoff, memoria
from app.core.modelos import MensagemEntrada, Resposta
from app.core.notificacoes import notificar_status
from app.core.orquestrador import obter_orquestrador
from app.erp import obter_erp, redefinir_erp

logging.basicConfig(level=config.log_nivel, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")
ESTATICOS = Path(__file__).parent / "static"

PERFIS_SIMULADOR = [
    {"telefone": "5511999990001", "nome": "Maria Silva", "descricao": "Cliente PF cadastrada, com pedido saindo para entrega"},
    {"telefone": "5511999990002", "nome": "Padaria Pão Quente", "descricao": "Cliente PJ (CNPJ), com boletos em aberto"},
    {"telefone": "5511999990003", "nome": "João Souza", "descricao": "Cliente PF cadastrado, sem pedidos"},
    {"telefone": "5511988887777", "nome": "Número desconhecido", "descricao": "Telefone sem cadastro (testa identificação por CPF + CEP e cadastro novo)"},
]


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    db.inicializar()
    erp = obter_erp()
    if hasattr(erp, "preparar_pedido_em_andamento"):
        erp.preparar_pedido_em_andamento()
    from app.rag.hibrido import obter_busca

    obter_busca()
    obter_orquestrador()
    log.info("Bot pronto. ERP=%s | WhatsApp=%s (envio real: %s) | IA=%s", config.erp_modo, config.whatsapp_provedor,
             config.whatsapp_envio_real, config.llm_provedor if config.llm_ativo else "desligada")
    yield


app = FastAPI(title=f"Chatbot {config.empresa_nome}", lifespan=ciclo_de_vida)


# ============================================================================ util
def processar_e_enviar(msg: MensagemEntrada) -> None:
    """Usado pelos webhooks: processa e envia as respostas pelo mesmo canal."""
    try:
        if msg.id_externo and db.ja_processado(msg.id_externo):
            log.info("Mensagem %s já processada (reenvio do webhook) — ignorada", msg.id_externo)
            return
        canal = canais.obter_canal(msg.canal)
        if msg.canal == "whatsapp":
            canal.marcar_lido(msg.id_externo)
        res = obter_orquestrador().processar(msg)
        for r in res.respostas:
            canal.enviar(msg.usuario_id, r)
    except Exception:  # noqa: BLE001
        log.exception("Erro ao processar mensagem de %s", msg.usuario_id)
        try:
            canais.enviar(msg.canal, msg.usuario_id, Resposta("Tive um problema técnico agora. 😕 Pode tentar de novo em instantes?"))
        except Exception:  # noqa: BLE001
            pass


def _local(request: Request) -> bool:
    # atrás de proxy (nginx, túnel ngrok/cloudflared) o IP parece local: nesses casos exige senha
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or request.headers.get("cf-connecting-ip"):
        return False
    return (request.client.host if request.client else "") in ("127.0.0.1", "::1", "localhost", "testclient")


def exige_painel(request: Request, x_painel_senha: str | None = Header(default=None)) -> None:
    if not x_painel_senha or not hmac.compare_digest(x_painel_senha, config.atendente_senha):
        raise HTTPException(401, "Senha do painel inválida (cabeçalho X-Painel-Senha)")


def exige_simulador(request: Request, x_painel_senha: str | None = Header(default=None)) -> None:
    if not config.simulador_ativo:
        raise HTTPException(404, "Simulador desligado (SIMULADOR_ATIVO=false)")
    # fora do computador local, o simulador pede a senha do painel (ele permite "ser" qualquer telefone)
    if (config.erp_modo == "revgas" or not _local(request)) and not (
        x_painel_senha and hmac.compare_digest(x_painel_senha, config.atendente_senha)
    ):
        exige_painel(request, x_painel_senha)


# ============================================================================ páginas
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi import Response

    return Response(content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🔥</text></svg>',
                    media_type="image/svg+xml")


@app.get("/", include_in_schema=False)
def inicio():
    return RedirectResponse("/simulador")


@app.get("/simulador", include_in_schema=False)
def pagina_simulador():
    if not config.simulador_ativo:
        raise HTTPException(404)
    return FileResponse(ESTATICOS / "simulador.html")


@app.get("/atendente", include_in_schema=False)
def pagina_atendente():
    return FileResponse(ESTATICOS / "atendente.html")


@app.get("/saude")
def saude():
    return {"ok": True, "componentes": config.resumo_status()}


@app.get("/diagnostico", dependencies=[Depends(exige_painel)])
def diagnostico():
    out = {"config": config.resumo_status()}
    if config.whatsapp_provedor == "cloud":
        out["whatsapp"] = canais.obter_canal("whatsapp").diagnosticar()
    try:
        out["erp"] = {"ok": True, "filiais": len(obter_erp().listar_filiais())}
    except Exception as e:  # noqa: BLE001
        out["erp"] = {"ok": False, "erro": str(e)}
    return out


# ============================================================================ WhatsApp oficial
@app.get("/webhooks/whatsapp")
def whatsapp_verificar(hub_mode: str | None = Query(None, alias="hub.mode"), hub_token: str | None = Query(None, alias="hub.verify_token"),
                       hub_challenge: str | None = Query(None, alias="hub.challenge")):
    from app.canais.whatsapp_cloud import WhatsAppCloud

    resposta = WhatsAppCloud.verificar_webhook(hub_mode, hub_token, hub_challenge)
    if resposta is None:
        log.warning("Verificação do webhook recusada: WHATSAPP_VERIFY_TOKEN não confere")
        raise HTTPException(403, "verify_token inválido")
    log.info("Webhook do WhatsApp verificado pela Meta ✅")
    return PlainTextResponse(resposta)


@app.post("/webhooks/whatsapp")
async def whatsapp_receber(request: Request, tarefas: BackgroundTasks, x_hub_signature_256: str | None = Header(default=None)):
    from app.canais.whatsapp_cloud import WhatsAppCloud

    corpo = await request.body()
    if not WhatsAppCloud.assinatura_valida(corpo, x_hub_signature_256):
        log.warning("Webhook com assinatura inválida recusado (confira WHATSAPP_APP_SECRET)")
        raise HTTPException(401, "assinatura inválida")
    try:
        payload = json.loads(corpo or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "JSON inválido")
    msgs, status = WhatsAppCloud.extrair(payload)
    for s in status:
        if s.get("errors"):
            from app.canais.whatsapp_cloud import ERROS

            for e in s["errors"]:
                log.error("Falha de entrega WhatsApp para %s: [%s] %s → %s", s.get("recipient_id"), e.get("code"),
                          e.get("title"), ERROS.get(e.get("code"), ""))
    for m in msgs:
        tarefas.add_task(processar_e_enviar, m)
    return {"ok": True, "mensagens": len(msgs)}  # responder 200 rápido evita reenvio pela Meta


# ============================================================================ Evolution (opcional)
@app.post("/webhooks/evolution")
async def evolution_receber(request: Request, tarefas: BackgroundTasks, token: str | None = Query(None),
                            x_webhook_token: str | None = Header(default=None)):
    if config.whatsapp_provedor != "evolution":
        raise HTTPException(404, "Ponte Evolution desligada (WHATSAPP_PROVEDOR != evolution)")
    from app.canais.evolution import Evolution

    if not Evolution.token_valido(x_webhook_token or token):
        raise HTTPException(401, "token inválido")
    for m in Evolution.extrair(await request.json()):
        tarefas.add_task(processar_e_enviar, m)
    return {"ok": True}


# ============================================================================ ERP / app de entrega
class StatusPedido(BaseModel):
    numero_pedido: str
    status: str | None = None


@app.post("/webhooks/erp/status")
def erp_status(dados: StatusPedido, x_webhook_token: str | None = Header(default=None)):
    if not config.erp_webhook_token or not x_webhook_token or not hmac.compare_digest(x_webhook_token, config.erp_webhook_token):
        raise HTTPException(401, "token inválido (X-Webhook-Token / ERP_WEBHOOK_TOKEN)")
    erp = obter_erp()
    if dados.status and config.erp_modo == "mock":
        erp.atualizar_status_pedido(dados.numero_pedido, dados.status)
    return notificar_status(dados.numero_pedido)


# ============================================================================ simulador
class MsgSimulador(BaseModel):
    usuario_id: str
    nome: str = ""
    texto: str = ""
    botao_id: str | None = None
    botao_titulo: str | None = None
    localizacao: dict | None = None
    tipo: str = "texto"


@app.get("/api/simulador/perfis", dependencies=[Depends(exige_simulador)])
def sim_perfis():
    return {"perfis": PERFIS_SIMULADOR, "config": config.resumo_status()}


@app.post("/api/simulador/mensagem", dependencies=[Depends(exige_simulador)])
def sim_mensagem(m: MsgSimulador):
    if m.botao_id:
        msg = MensagemEntrada(canal="simulador", usuario_id=m.usuario_id, nome=m.nome, tipo="botao", botao_id=m.botao_id,
                              texto=m.botao_titulo or "")
    elif m.localizacao:
        msg = MensagemEntrada(canal="simulador", usuario_id=m.usuario_id, nome=m.nome, tipo="localizacao", localizacao=m.localizacao)
    else:
        msg = MensagemEntrada(canal="simulador", usuario_id=m.usuario_id, nome=m.nome, texto=m.texto,
                              tipo=m.tipo if m.tipo in ("texto", "audio", "imagem") else "texto")
    res = obter_orquestrador().processar(msg)
    return {"respostas": [r.como_dict() for r in res.respostas], "rastreio": res.rastreio.etapas,
            "total_ms": res.rastreio.total_ms(), "intencao": res.intencao, "estado": res.estado}


@app.get("/api/simulador/eventos", dependencies=[Depends(exige_simulador)])
def sim_eventos(usuario_id: str, apos: int = 0):
    return {"eventos": db.ler_saida("simulador", usuario_id, apos)}


@app.get("/api/simulador/historico", dependencies=[Depends(exige_simulador)])
def sim_historico(usuario_id: str):
    conv = memoria.carregar("simulador", usuario_id)
    return {"mensagens": memoria.historico(conv, 100), "estado": conv.estado, "slots": conv.slots}


class Usuario(BaseModel):
    usuario_id: str


@app.post("/api/simulador/reset", dependencies=[Depends(exige_simulador)])
def sim_reset(u: Usuario):
    memoria.resetar("simulador", u.usuario_id)
    return {"ok": True}


@app.post("/api/simulador/resetar-demo", dependencies=[Depends(exige_simulador)])
def sim_resetar_demo():
    if config.erp_modo != "mock":
        raise HTTPException(400, "Só disponível com ERP_MODO=mock")
    obter_erp().resetar_demo()
    redefinir_erp()
    obter_erp()
    with db.conexao() as con:
        for tabela in ("mensagens", "handoffs", "conversas", "caixa_saida", "ids_processados"):
            con.execute(f"DELETE FROM {tabela}")
    return {"ok": True}


class Avancar(BaseModel):
    usuario_id: str
    numero_pedido: str | None = None


@app.post("/api/simulador/avancar-pedido", dependencies=[Depends(exige_simulador)])
def sim_avancar(a: Avancar):
    """Simula a central/app de entrega mudando o status do pedido (dispara o aviso proativo)."""
    if config.erp_modo != "mock":
        raise HTTPException(400, "Só disponível com ERP_MODO=mock")
    conv = memoria.carregar("simulador", a.usuario_id)
    numero = a.numero_pedido or conv.slots.get("pedido_atual")
    if not numero:
        raise HTTPException(400, "Nenhum pedido em andamento nesta conversa")
    erp = obter_erp()
    p = erp.consultar_pedido(numero)
    proximo = {"recebido": "em_separacao", "em_separacao": "em_rota", "em_rota": "entregue"}.get(p["status"]) if p else None
    if not proximo:
        raise HTTPException(400, f"Pedido {numero} já está '{p['status'] if p else '?'}'")
    erp.atualizar_status_pedido(numero, proximo)
    return notificar_status(numero)


@app.get("/api/simulador/whatsapp-sandbox", dependencies=[Depends(exige_simulador)])
def sim_sandbox(apos: int = 0):
    return {"mensagens": db.ler_saida("whatsapp_sandbox", None, apos)}


# ============================================================================ painel do atendente
@app.get("/api/atendente/handoffs", dependencies=[Depends(exige_painel)])
def at_listar(status: str | None = None):
    return {"handoffs": handoff.listar(status), "dentro_do_horario": handoff.dentro_do_horario()}


@app.get("/api/atendente/conversa/{conversa_id}", dependencies=[Depends(exige_painel)])
def at_conversa(conversa_id: int):
    conv = memoria.obter_por_id(conversa_id)
    if not conv:
        raise HTTPException(404)
    from app.core.handoff import dados_coletados

    return {"mensagens": memoria.historico(conv, 200), "estado": conv.estado, "dados": dados_coletados(conv), "canal": conv.canal}


class AcaoAtendente(BaseModel):
    atendente: str = "Atendente"
    texto: str = ""


def _conv_do_handoff(hid: int):
    h = handoff.obter(hid)
    if not h:
        raise HTTPException(404, "handoff não encontrado")
    return h, memoria.obter_por_id(h["conversa_id"])


@app.post("/api/atendente/handoffs/{hid}/assumir", dependencies=[Depends(exige_painel)])
def at_assumir(hid: int, a: AcaoAtendente):
    h, conv = _conv_do_handoff(hid)
    handoff.atualizar_status(hid, "em_atendimento", a.atendente)
    conv.estado = "humano"
    memoria.salvar(conv)
    return {"ok": True}


@app.post("/api/atendente/handoffs/{hid}/responder", dependencies=[Depends(exige_painel)])
def at_responder(hid: int, a: AcaoAtendente):
    if not a.texto.strip():
        raise HTTPException(400, "texto vazio")
    h, conv = _conv_do_handoff(hid)
    if conv.canal == "whatsapp" and canais.fora_da_janela_24h(conv.ultima_msg_cliente):
        raise HTTPException(409, "Mais de 24h sem mensagem do cliente: o WhatsApp só permite template aprovado.")
    if h["status"] == "aberto":
        handoff.atualizar_status(hid, "em_atendimento", a.atendente)
    conv.estado = "humano"
    memoria.registrar_mensagem(conv, "atendente", a.texto, {"atendente": a.atendente})
    memoria.salvar(conv)
    r = canais.enviar(conv.canal, conv.usuario_id, Resposta(f"*{a.atendente}:* {a.texto}"))
    return {"ok": True, "envio": r}


@app.post("/api/atendente/handoffs/{hid}/encerrar", dependencies=[Depends(exige_painel)])
def at_encerrar(hid: int, a: AcaoAtendente):
    h, conv = _conv_do_handoff(hid)
    handoff.atualizar_status(hid, "encerrado", a.atendente)
    conv.estado = "bot"
    conv.slots["falhas_seguidas"] = 0
    memoria.salvar(conv)
    msg = a.texto.strip() or "Atendimento finalizado. Se precisar de algo, é só mandar mensagem por aqui! 😊"
    memoria.registrar_mensagem(conv, "atendente", msg, {"atendente": a.atendente, "encerramento": True})
    canais.enviar(conv.canal, conv.usuario_id, Resposta(msg))
    return {"ok": True}


@app.exception_handler(HTTPException)
async def erro_http(_req: Request, exc: HTTPException):
    return JSONResponse({"erro": exc.detail}, status_code=exc.status_code)
