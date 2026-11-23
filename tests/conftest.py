"""Configuração dos testes: tudo roda em pastas temporárias, sem IA, sem WhatsApp real."""
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bot-testes-"))
os.environ.update({
    "PASTA_DADOS": str(_TMP), "BOT_DB_PATH": str(_TMP / "bot.db"), "ERP_MOCK_DB_PATH": str(_TMP / "erp.db"),
    "LLM_PROVEDOR": "nenhum", "ATENDIMENTO_MODO": "legado", "EMBEDDINGS_PROVEDOR": "local", "ERP_MODO": "mock",
    "WHATSAPP_PROVEDOR": "cloud", "WHATSAPP_ENVIO_REAL": "false", "WHATSAPP_VERIFY_TOKEN": "verifica-123",
    "WHATSAPP_APP_SECRET": "segredo-app", "WHATSAPP_PHONE_NUMBER_ID": "999", "WHATSAPP_TOKEN": "",
    "ERP_WEBHOOK_TOKEN": "tok-erp", "PAINEL_ATENDENTE_SENHA": "senha-teste", "SIMULADOR_ATIVO": "true",
    "HORARIO_ATENDIMENTO_INICIO": "00:00", "HORARIO_ATENDIMENTO_FIM": "23:59",
    "DIAS_ATENDIMENTO": "seg,ter,qua,qui,sex,sab,dom", "FALHAS_PARA_TRANSFERIR": "2",
})

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.config import config  # noqa: E402

config.recarregar()


@pytest.fixture()
def bot():
    """Bot 'zerado' a cada teste: banco do bot e ERP de demonstração recriados."""
    from app.core.orquestrador import Orquestrador
    from app.erp import obter_erp, redefinir_erp
    from app.llm import definir_llm

    for f in ("bot.db", "erp.db"):
        (_TMP / f).unlink(missing_ok=True)
    redefinir_erp()
    db.inicializar()
    obter_erp()
    definir_llm(None)
    return Orquestrador()


def falar(orq, texto="", usuario="5511999990001", canal="simulador", botao=None, **kw):
    from app.core.modelos import MensagemEntrada

    if botao:
        msg = MensagemEntrada(canal=canal, usuario_id=usuario, tipo="botao", botao_id=botao, texto=kw.get("titulo", ""))
    else:
        msg = MensagemEntrada(canal=canal, usuario_id=usuario, texto=texto, tipo=kw.get("tipo", "texto"),
                              localizacao=kw.get("localizacao"))
    return orq.processar(msg)


def textos(res) -> str:
    return "\n".join(r.texto for r in res.respostas)


def tecnicas(res) -> set:
    return {e["tecnica"] for e in res.rastreio.etapas}


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
