"""Caminho com IA (Agentic RAG + Tool Calling), usando uma IA FALSA e roteirizada.
Garante que as travas valem mesmo quando a IA erra ou tenta burlar regras."""
import json

from app.erp import obter_erp
from app.llm import definir_llm
from app.llm.base import LLM
from tests.conftest import falar, tecnicas, textos


class IAFalsa(LLM):
    nome = "falsa"

    def __init__(self, roteiro=None, final="ok", classificacao=None, falhar=False):
        self.roteiro = roteiro or []          # [(ferramenta, argumentos), ...]
        self.final = final                    # texto final (ou função que recebe os resultados)
        self.classificacao = classificacao or {"intencao": "duvida_informacao", "confianca": 0.9}
        self.falhar = falhar
        self.resultados = []

    def gerar(self, system, mensagens, max_tokens=800, rapido=False):
        if self.falhar:
            raise RuntimeError("IA fora do ar")
        if "JSON" in system:
            return json.dumps(self.classificacao)
        return "Resumo: cliente pediu ajuda."

    def agente(self, system, mensagens, ferramentas, executar, max_passos=5):
        if self.falhar:
            raise RuntimeError("IA fora do ar")
        assert "REGRAS INVIOLÁVEIS" in system and all("input_schema" in f for f in ferramentas)
        chamadas = []
        for nome, args in self.roteiro:
            r = executar(nome, args)
            self.resultados.append(r)
            chamadas.append({"nome": nome, "args": args, "resultado": r})
        texto = self.final(self.resultados) if callable(self.final) else self.final
        return texto, chamadas


def test_agente_busca_documentos_e_responde(bot):
    definir_llm(IAFalsa(roteiro=[("buscar_documentos", {"consulta": "formas de pagamento vale"})],
                        final="Aceitamos Pix, dinheiro e cartão na entrega."))
    r = falar(bot, "vocês trabalham com vale de empresa?")
    assert "Pix" in textos(r) and r.respostas[0].ia
    assert {"Agentic RAG", "Tool Calling", "RAG híbrido", "Reranking"} <= tecnicas(r)


def test_preco_inventado_pela_ia_e_bloqueado(bot):
    definir_llm(IAFalsa(roteiro=[("consultar_preco", {"produto_codigo": "P13"})], final="O P13 está saindo por R$ 99,00!"))
    r = falar(bot, "me explica a diferença dos botijões e o valor")
    assert "R$ 99,00" not in textos(r) and "Não consegui confirmar esse valor" in textos(r)


def test_preco_vindo_do_erp_passa(bot):
    definir_llm(IAFalsa(roteiro=[("consultar_preco", {"produto_codigo": "P13"})],
                        final=lambda res: f"O P13 custa R$ {res[0]['preco']:.2f}".replace(".", ",")))
    r = falar(bot, "me explica a diferença dos botijões e o valor")
    assert "R$ 115,00" in textos(r)


def test_ia_nao_consegue_criar_pedido_nem_ver_dado_de_outro_cliente(bot):
    ia = IAFalsa(roteiro=[("criar_pedido", {"itens": [{"produto_codigo": "P13", "quantidade": 50}]}),
                          ("consultar_pedido", {"numero_pedido": "PED-1004", "cliente_id": 2})],
                 final="Feito.")
    definir_llm(ia)
    antes = len(obter_erp().listar_pedidos(1, limite=50))
    falar(bot, "me ajuda com uma coisa diferente aqui")
    assert ia.resultados[0]["erro"] == "Ferramenta não permitida para a IA."
    assert "não encontrado" in ia.resultados[1]["erro"]          # PED-1004 é da Padaria
    assert len(obter_erp().listar_pedidos(1, limite=50)) == antes


def test_ia_prepara_troca_e_cliente_confirma_por_botao(bot):
    definir_llm(IAFalsa(roteiro=[("verificar_regras_troca", {"numero_pedido": "PED-1001"}),
                                 ("preparar_acao", {"acao": "abrir_troca", "numero_pedido": "PED-1001", "motivo": "lacre violado"})],
                        final="Posso abrir a troca do PED-1001 para você?"))
    r = falar(bot, "o lacre do botijão veio violado, quero trocar")
    assert r.respostas[-1].botoes[0][0] == "acao_ok"
    r = falar(bot, botao="acao_ok")
    assert "Protocolo" in textos(r)


def test_ia_fora_do_ar_nao_derruba_o_atendimento(bot):
    definir_llm(IAFalsa(falhar=True))
    r = falar(bot, "vocês aceitam cartão?")
    assert "Formas de pagamento" in textos(r)   # caiu para o RAG sem IA
    r = falar(bot, "meu pedido chegou?")
    assert "PED-1003" in textos(r)
