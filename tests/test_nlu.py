"""Intent Classification, Entity Extraction e Query Rewriting."""
import pytest

from app.nlu import entidades, intencoes, reescrita
from app.util.documentos import cnpj_valido, cpf_valido, variantes_telefone_br


# ---------------------------------------------------------------- entidades
def test_cpf_cnpj_com_digito_verificador():
    assert cpf_valido("529.982.247-25") and not cpf_valido("529.982.247-26") and not cpf_valido("111.111.111-11")
    assert cnpj_valido("11.222.333/0001-81") and not cnpj_valido("11.222.333/0001-82")
    e = entidades.extrair("meu cpf é 529.982.247-25")
    assert e["documento"] == "52998224725"
    e = entidades.extrair("cpf 123.456.789-00")
    assert "documento" not in e and e["documento_invalido"] == "12345678900"


def test_produtos_e_quantidades():
    e = entidades.extrair("quero 2 gás e 1 água")
    assert e["produtos"] == [{"codigo": "P13", "quantidade": 2}, {"codigo": "AGUA20", "quantidade": 1}]
    assert entidades.extrair("me vê dois botijões de 45")["produtos"] == [{"codigo": "P45", "quantidade": 2}]
    assert entidades.extrair("quero o botijão completo")["produto"] == "P13C"
    assert "produtos" not in entidades.extrair("tá com cheiro de gás aqui")


def test_pedido_periodo_pagamento_endereco():
    assert entidades.extrair("cadê o PED-1003")["numero_pedido"] == "PED-1003"
    assert entidades.extrair("meu pedido 1042 chegou?")["numero_pedido"] == "PED-1042"
    assert entidades.extrair("quanto gastei mês passado")["periodo"]["descricao"] == "mês passado"
    e = entidades.extrair("pago em dinheiro, troco pra 200")
    assert e["forma_pagamento"] == "dinheiro" and e["troco_para"] == 200.0
    filiais = [{"id": 1, "nome": "Centro", "bairros": ["Vila Nova"]}]
    e = entidades.extrair("Rua das Flores, 120 - Vila Nova", filiais)
    assert e["endereco"]["numero"] == "120" and e["bairro"] == "Vila Nova" and e["filial_id"] == 1


def test_telefone_brasileiro_com_e_sem_nono_digito():
    assert set(variantes_telefone_br("5511999990001")) == {"5511999990001", "551199990001"}


# ---------------------------------------------------------------- intenções
@pytest.mark.parametrize("msg,esperada", [
    ("oi bom dia", "saudacao"), ("quero um gás", "fazer_pedido"), ("acabou meu gás", "fazer_pedido"),
    ("quero trocar meu botijão vazio", "fazer_pedido"), ("meu pedido chegou?", "status_pedido"),
    ("quanto tá o P45?", "consultar_preco"), ("tem p45?", "consultar_estoque"), ("me manda a 2ª via do boleto", "boleto"),
    ("o botijão veio amassado, quero trocar", "troca"), ("quantos pedidos fiz esse mês?", "consulta_historico"),
    ("que horas vocês abrem domingo?", "duvida_informacao"), ("faz o P13 por 100 reais?", "pedir_desconto"),
    ("quero falar com um atendente", "falar_atendente"), ("tá com cheiro de gás aqui em casa", "emergencia_vazamento"),
    ("como faço pra testar se tem vazamento?", "duvida_informacao"), ("cancela meu pedido", "cancelar_pedido"),
    ("o entregador foi grosseiro, péssimo", "reclamacao"), ("sim", "confirmar"), ("qual a capital da frança", "fora_escopo"),
])
def test_intencoes(msg, esperada):
    assert intencoes.classificar_regras(msg)["intencao"] == esperada


def test_multiplas_intencoes():
    r = intencoes.classificar_regras("quero trocar o botijão que veio amassado e saber o preço do P45")
    assert r["intencao"] == "troca" and r["secundarias"] == ["consultar_preco"]


# ---------------------------------------------------------------- query rewriting
def test_meu_pedido_chegou_vira_consulta_estruturada():
    slots = {"pedido_atual": "PED-1003"}
    exp = reescrita.expandir("meu pedido chegou?", slots)
    assert "PED-1003" in exp["texto"]
    ent = entidades.extrair(exp["texto"])
    q = reescrita.estruturar("status_pedido", ent, slots, {"id": 1, "nome": "Maria"}, exp["texto"])
    assert q["acao"] == "consultar_status_pedido" and q["pedido"] == "PED-1003" and q["cliente_id"] == 1


def test_elipse_de_produto():
    exp = reescrita.expandir("e o de 45?", {"ultima_intencao": "consultar_preco", "produto": "P13"})
    assert exp["texto"].startswith("quanto custa")
    assert entidades.extrair(exp["texto"])["produto"] == "P45"


def test_o_de_sempre_repete_ultimo_pedido():
    exp = reescrita.expandir("o de sempre", {"ultimo_pedido_itens": [{"produto_codigo": "P13", "quantidade": 1}]})
    assert entidades.extrair(exp["texto"])["produtos"] == [{"codigo": "P13", "quantidade": 1}]


def test_expansao_de_sinonimos_para_o_rag():
    consulta, extras = reescrita.expandir_para_busca("quanto custa o frete?")
    assert "taxa de entrega" in consulta and extras
