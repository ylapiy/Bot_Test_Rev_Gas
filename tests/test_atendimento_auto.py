import pytest

from app.config import config
from app.llm import definir_llm
from tests.conftest import falar, textos
from tests.test_agente_ia import IAFalsa


@pytest.fixture(autouse=True)
def modo_auto(monkeypatch):
    monkeypatch.setattr(config, "atendimento_modo", "auto")


def test_menu_numerado_e_retomada_no_evolution(bot):
    def enviar(texto):
        return falar(bot, texto, canal="evolution")

    menu = enviar("Quero dois botijões")
    assert "*1.* Fazer pedido" in textos(menu)
    assert not menu.respostas[0].botoes
    produtos = enviar("1")
    assert "O que você precisa" in textos(produtos)
    assert textos(enviar("quero ver meus boletos")) == textos(produtos)
    endereco = enviar("1")
    assert "Entrego em" in textos(endereco)
    assert textos(enviar("continuar")) == textos(endereco)
    assert "Como prefere pagar" in textos(enviar("1"))
    assert "Como prefere pagar" in textos(enviar("Pix"))
    assert "encerrado" in textos(enviar("sair"))
    assert "*1.* Fazer pedido" in textos(enviar("continuar"))


def test_interpreta_com_ia_e_mantem_fluxo_apos_falha(bot):
    definir_llm(IAFalsa(classificacao={
        "intencao": "fazer_pedido", "confianca": 0.9,
        "entidades": {"produto": "P13", "quantidade": 2},
    }))
    assert "Entrego em" in textos(falar(bot, "Separe duas unidades do menor."))
    definir_llm(IAFalsa(falhar=True))
    assert "Como prefere pagar" in textos(falar(bot, "1"))
    assert "*1.* Fazer pedido" in textos(falar(bot, "Preciso de gás", usuario="5511988887777"))


def test_ia_usa_as_mesmas_mensagens_da_opcao_do_menu(bot):
    falar(bot, "menu")
    guiado = falar(bot, "2")
    definir_llm(IAFalsa(
        classificacao={"intencao": "status_pedido", "confianca": 0.99,
                       "resposta": "Mensagem inventada pelo modelo."},
        final="Mensagem inventada pelo agente.",
    ))
    interpretado = falar(bot, "Já saiu para entrega?")
    assert textos(interpretado) == textos(guiado)
    assert all(not r.ia for r in interpretado.respostas)


def test_ia_nao_abre_rotas_fora_do_menu(bot):
    definir_llm(IAFalsa(classificacao={"intencao": "cancelar_pedido", "confianca": 0.99}))
    resposta = falar(bot, "Cancela a minha entrega")
    assert "*1.* Fazer pedido" in textos(resposta)
    assert resposta.intencao is None


def test_confirmacao_exige_opcao_e_so_cria_uma_vez(bot):
    for texto in ("menu", "1", "1", "1", "1"):
        resposta = falar(bot, texto)
    assert "*1.* Confirmar pedido" in textos(resposta)
    for campo in ("Subtotal:", "Taxa de entrega:", "Total:", "Pagamento:", "Endereço:", "Filial:"):
        assert campo in textos(resposta)
    assert "*1.* Confirmar pedido" in textos(falar(bot, "sim"))
    recibo = falar(bot, "1")
    assert "confirmado!" in textos(recibo)
    assert "Pagamento: Pix" in textos(recibo)
    assert "*2.* Voltar ao menu" in textos(recibo)
    status = falar(bot, "1")
    assert "confirmado!" not in textos(status)
    assert "Status:" in textos(status)
    assert "Pagamento: Pix" in textos(status)
    assert "Endereço:" in textos(status)
    assert "*1.* Fazer pedido" in textos(falar(bot, "3"))


def test_voltar_ao_menu_descarta_fluxo_em_aberto(bot):
    falar(bot, "menu")
    falar(bot, "1")
    endereco = falar(bot, "1")
    assert "*3.* Voltar ao menu" in textos(endereco)
    assert "*1.* Fazer pedido" in textos(falar(bot, "3"))
    assert "*1.* Fazer pedido" in textos(falar(bot, "continuar"))


def test_whatsapp_real_encaminha_sem_consultar_erp_mock(bot, monkeypatch):
    monkeypatch.setattr(config, "whatsapp_envio_real", True)

    def falhar(*args, **kwargs):
        pytest.fail("Atendimento real consultou o ERP de demonstração")

    monkeypatch.setattr("app.core.fluxos.identificar_por_telefone", falhar)
    assert "Fazer pedido" in textos(falar(bot, "menu", canal="evolution"))
    resposta = falar(bot, "1", canal="evolution")
    assert resposta.estado == "humano"
    assert "confirmado" not in textos(resposta)
