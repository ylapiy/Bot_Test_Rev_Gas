"""Testa a integração HTTP contratual sem gravar vendas na RevGás."""
import json

import httpx
import pytest

from app.config import config
from app.erp import ErroERP, redefinir_erp
from app.erp.revgas_http import ErpRevGasHttp
from tests.conftest import falar, textos


@pytest.fixture
def revgas(bot, monkeypatch):
    monkeypatch.setattr(config, "erp_modo", "revgas")
    monkeypatch.setattr(config, "erp_base_url", "https://api.revgas.com/v1")
    monkeypatch.setattr(config, "erp_token", "chave-teste")
    cliente = {"id": 9, "name": "Maria Silva", "type": "PHYSICAL",
               "phones": ["(11) 99999-0001"], "location": "Rua das Flores",
               "number": "10", "neighborhood": "Centro", "postal_code": "01234000"}
    venda = {"id": 153, "customer_id": 9, "establishment_id": 42, "status": "OPEN", "total": 240,
             "location": "Rua das Flores", "number": "10", "neighborhood": "Centro",
             "products": [{"product_id": 101, "quantity": 2, "product_description": "Gás P13"}]}
    chamadas = []
    estado = {"estoque": 7, "falha_venda": False}

    def responder(r):
        assert r.headers["Authorization"] == "chave-teste"
        caminho = r.url.path
        assert caminho.startswith("/v1/")
        chamadas.append(r)
        if r.method == "GET":
            if caminho == "/v1/customers":
                if r.url.params.get("phone") != "11999990001":
                    return httpx.Response(200, json={"data": []})
                return httpx.Response(200, json={"data": [cliente]})
            if caminho == "/v1/customers/9":
                return httpx.Response(200, json=cliente)
            if caminho == "/v1/establishments":
                if r.url.params.get("page") == "1":
                    return httpx.Response(200, json={"data": [{"id": 41, "description": "Matriz"}],
                                                     "pagination": {"page": 1, "has_next": True}})
                assert r.url.params.get("page") == "2"
                return httpx.Response(200, json={"data": [{"id": 42, "description": "Centro"}],
                                                 "pagination": {"page": 2, "has_next": False}})
            if caminho == "/v1/products":
                assert r.url.params.get("establishment_id") == "42"
                return httpx.Response(200, json={"data": [{"id": 101, "description": "Gás P13",
                    "establishment_id": 42, "stock_quantity": estado["estoque"]}]})
            if caminho == "/v1/payment-types":
                assert r.url.params.get("status") == "ACTIVE"
                return httpx.Response(200, json={"data": [{"payment_type": "PIX"}]})
            if caminho == "/v1/sales":
                assert r.url.params.get("customer_id") == "9"
                return httpx.Response(200, json={"data": []})
            if caminho == "/v1/sales/153":
                return httpx.Response(200, json={**venda, "delivered_at": "2026-11-25T11:30:00"})
            if caminho == "/v1/future-receipts/33":
                return httpx.Response(200, json={"customer_id": 9, "value": 240, "due_date": "2026-11-25"})
            if caminho == "/v1/future-receipts/33/invoice":
                return httpx.Response(200, json={"invoice": {"secure_url": "https://example.test/fatura"}})
        if r.method == "POST" and caminho == "/v1/sales":
            corpo = json.loads(r.content)
            assert corpo["products"] == [{"product_id": 101, "quantity": 2, "value": 120.0}]
            assert corpo["payments"] == [{"payment_type": "PIX", "value": 240.0}]
            assert corpo["option"] == "NORMAL" and corpo["origin"] == "INTEGRATION"
            assert corpo["customer_id"] == 9 and corpo["establishment_id"] == 42
            if estado["falha_venda"]:
                raise httpx.ReadTimeout("sem resposta", request=r)
            return httpx.Response(201, json={**venda, "location": corpo["location"],
                                             "payments": [{"type": "PIX"}]})
        pytest.fail(f"Requisição inesperada: {r.method} {caminho}")

    erp = ErpRevGasHttp(transport=httpx.MockTransport(responder))
    erp.filiais = {"42": {"bairros": ["Centro"], "produtos": {"P13": {"id": 101, "preco": 120}}}}
    redefinir_erp(erp)
    try:
        yield erp, chamadas, estado
    finally:
        redefinir_erp()
        erp.http.close()


@pytest.mark.parametrize("novo_endereco", [False, True])
def test_checkout_revgas(bot, revgas, novo_endereco):
    _, chamadas, _ = revgas
    assert "Rua das Flores" in textos(falar(bot, "quero 2 gás"))
    if novo_endereco:
        falar(bot, botao="end_outro")
        falar(bot, "Rua Nova, 55 - Centro")
    else:
        falar(bot, botao="end_ok")
    cotacao = textos(falar(bot, botao="pag_pix"))
    assert "R$ 240,00" in cotacao and "Posso confirmar" in cotacao
    assert all(r.method == "GET" for r in chamadas)
    resposta = textos(falar(bot, botao="confirmar_pedido"))
    assert "PED-153" in resposta and "confirmado" in resposta
    assert "PED-153" in textos(falar(bot, "meu pedido chegou?"))
    vendas = [r for r in chamadas if r.method == "POST"]
    assert len(vendas) == 1
    assert json.loads(vendas[0].content)["location"] == ("Rua Nova" if novo_endereco else "Rua das Flores")


@pytest.mark.parametrize(("campo", "mensagem", "posts"), [
    ("estoque", "estoque mudou", 0),
    ("falha_venda", "confira no painel", 1),
    ("preco", "preço mudou", 0),
])
def test_checkout_bloqueia_confirmacao_insegura(bot, revgas, campo, mensagem, posts):
    erp, chamadas, estado = revgas
    falar(bot, "quero 2 gás")
    falar(bot, botao="end_ok")
    falar(bot, botao="pag_pix")
    if campo == "estoque":
        estado[campo] = 1
    elif campo == "preco":
        erp.filiais["42"]["produtos"]["P13"]["preco"] = 125
    else:
        estado[campo] = True
    assert mensagem in textos(falar(bot, botao="confirmar_pedido")).lower()
    falar(bot, botao="confirmar_pedido")
    assert sum(r.method == "POST" for r in chamadas) == posts


def test_segunda_via_so_busca_fatura_para_titular(revgas):
    erp, chamadas, _ = revgas
    assert erp.segunda_via("33", cliente_id=44) is None
    assert len(chamadas) == 1
    assert erp.segunda_via("33", cliente_id=9)["link"] == "https://example.test/fatura"


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_falha_em_lista_nao_vira_lista_vazia(revgas, status):
    erp, _, _ = revgas
    erp.http = httpx.Client(base_url=erp.http.base_url,
                            transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    with pytest.raises(ErroERP, match=str(status) if status in (401, 403, 404) else "limite"):
        erp.listar_filiais()
