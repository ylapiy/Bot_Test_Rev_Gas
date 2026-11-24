"""Confere o .env e diz, em português, o que falta para cada parte do bot funcionar.

    python scripts/verificar_config.py            # só confere o preenchimento
    python scripts/verificar_config.py --online   # também testa WhatsApp, ERP e IA de verdade
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import RAIZ, config  # noqa: E402

OK, AVISO, ERRO = "[OK]", "[AVISO]", "[ERRO]"


def linha(simbolo: str, texto: str) -> None:
    print(f"  {simbolo} {texto}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true")
    a = ap.parse_args()
    problemas = 0

    print("\n1) Arquivo .env")
    if (RAIZ / ".env").exists():
        linha(OK, ".env encontrado")
    else:
        linha(ERRO, "Não existe .env. Rode:  cp .env.example .env   (e preencha seguindo o GUIA_DE_CONFIGURACAO.md)")
        problemas += 1

    print("\n2) Empresa")
    linha(OK if config.empresa_nome != "Gás Exemplo" else AVISO, f"EMPRESA_NOME = {config.empresa_nome}")
    linha(OK, f"Prazo de entrega: {config.prazo_entrega_min} min | atendimento humano {config.horario_humano_inicio}-{config.horario_humano_fim} ({config.dias_atendimento_humano})")
    if config.atendente_senha in ("trocar-esta-senha", "", "123456"):
        linha(ERRO, "PAINEL_ATENDENTE_SENHA está com o valor padrão. Troque antes de colocar no ar.")
        problemas += 1

    print("\n3) WhatsApp")
    if config.whatsapp_provedor == "nenhum":
        linha(AVISO, "WHATSAPP_PROVEDOR=nenhum habilita o simulador e o terminal.")
    elif config.whatsapp_provedor == "cloud":
        for nome, valor in [("WHATSAPP_TOKEN", config.whatsapp_token), ("WHATSAPP_PHONE_NUMBER_ID", config.whatsapp_phone_number_id),
                            ("WHATSAPP_VERIFY_TOKEN", config.whatsapp_verify_token), ("WHATSAPP_APP_SECRET", config.whatsapp_app_secret)]:
            linha(OK if valor else ERRO, f"{nome} {'preenchido' if valor else 'VAZIO'}")
            problemas += 0 if valor else 1
        linha(OK if config.whatsapp_envio_real else AVISO,
              f"WHATSAPP_ENVIO_REAL={config.whatsapp_envio_real} " + ("" if config.whatsapp_envio_real else "(sandbox: nada é enviado à Meta)"))
        linha(OK if config.whatsapp_template_status else AVISO,
              "WHATSAPP_TEMPLATE_STATUS_PEDIDO " + ("ok" if config.whatsapp_template_status else "vazio: avisos de status só dentro da janela de 24h"))
        if a.online:
            from app.canais.whatsapp_cloud import WhatsAppCloud

            d = WhatsAppCloud().diagnosticar()
            if d.get("ok"):
                n = d["numero"]
                linha(OK, f"Token válido. Número: {n.get('display_phone_number')} | nome: {n.get('verified_name')} | qualidade: {n.get('quality_rating')}")
            else:
                linha(ERRO, f"Teste na Meta falhou: {d}")
                problemas += 1
    elif config.whatsapp_provedor == "evolution":
        linha(OK, "Evolution configurada como canal (opções numeradas).")
        for nome, valor in [("EVOLUTION_URL", config.evolution_url), ("EVOLUTION_API_KEY", config.evolution_api_key),
                            ("EVOLUTION_INSTANCIA", config.evolution_instancia),
                            ("EVOLUTION_WEBHOOK_TOKEN", config.evolution_webhook_token)]:
            linha(OK if valor else ERRO, f"{nome} {'preenchido' if valor else 'VAZIO'}")
            problemas += 0 if valor else 1

    print("\n4) ERP")
    if config.erp_modo == "mock":
        linha(AVISO, "ERP_MODO=mock usa dados fictícios de demonstração.")
        if config.whatsapp_provedor == "evolution" and config.whatsapp_envio_real:
            linha(AVISO, "Evolution com envio real: solicitações vão para atendimento humano.")
    elif config.erp_modo == "revgas":
        from app.erp import ErroERP
        from app.erp.revgas_http import ErpRevGasHttp, _dinheiro, _id

        try:
            erp = ErpRevGasHttp()
            linha(OK, "Chave RevGás preenchida.")
            if not erp.filiais:
                linha(ERRO, "Preencha filiais, bairros, IDs dos produtos e preços em config/erp_revgas.json.")
                problemas += 1
            if config.json_config("regras_comerciais.json").get("taxa_entrega", 0):
                linha(ERRO, "A venda automática da RevGás requer taxa_entrega igual a zero.")
                problemas += 1
            for filial_id, filial in erp.filiais.items():
                _id(filial_id)
                if not filial.get("bairros") or not filial.get("produtos"):
                    raise ErroERP(f"Informe bairros e produtos para o estabelecimento {filial_id}.")
                for oferta in filial["produtos"].values():
                    _id(oferta.get("id"))
                    _dinheiro(oferta.get("preco"))
            if a.online:
                filiais = erp.listar_filiais()
                ids = {str(f["id"]) for f in filiais}
                linha(OK, "RevGás autenticada. IDs de estabelecimentos: " + ", ".join(sorted(ids)))
                if set(erp.filiais) - ids:
                    linha(ERRO, "Há estabelecimentos configurados que a chave RevGás não retornou.")
                    problemas += 1
                for fid in sorted(ids):
                    if erp.filiais and fid not in erp.filiais:
                        continue
                    produtos = erp._produtos_filial(fid)
                    configurados = erp.filiais.get(fid, {}).get("produtos", {})
                    if not configurados:
                        linha(AVISO, f"Produtos do estabelecimento {fid}: " + ", ".join(
                            f"{p['id']} {p.get('description', '')}" for p in produtos.values()))
                    for codigo, oferta in configurados.items():
                        if str(oferta["id"]) not in produtos:
                            linha(ERRO, f"{codigo}: produto {oferta['id']} indisponível no estabelecimento {fid}.")
                            problemas += 1
                    pagamentos = list(erp._listar("/payment-types", establishment_id=int(fid), status="ACTIVE"))
                    linha(OK, f"Estabelecimento {fid}, pagamentos ativos: " + ", ".join(
                        str(p.get("payment_type")) for p in pagamentos))
        except (ErroERP, ValueError, OSError, TypeError, AttributeError) as e:
            linha(ERRO, f"RevGás: {e}")
            problemas += 1
    else:
        linha(ERRO, "ERP_MODO precisa ser mock ou revgas.")
        problemas += 1
    if not config.erp_webhook_token:
        linha(AVISO, "ERP_WEBHOOK_TOKEN vazio deixa os avisos automáticos de status desligados.")

    print("\n5) Atendimento e IA")
    linha(OK, f"ATENDIMENTO_MODO={config.atendimento_modo}")
    if not config.llm_ativo:
        texto_modo = "Menu guiado disponível." if config.atendimento_modo != "legado" else "Roteamento por regras locais."
        linha(AVISO, f"IA desligada (LLM_PROVEDOR={config.llm_provedor}). {texto_modo}")
    else:
        linha(OK, f"IA: {config.llm_provedor} / {config.llm_modelo}")
        if a.online:
            try:
                from app.llm import obter_llm

                r = obter_llm().gerar("Responda apenas OK.", [{"role": "user", "content": "teste"}], max_tokens=10, rapido=True)
                linha(OK, f"IA respondeu: {r[:40]!r}")
            except Exception as e:  # noqa: BLE001
                linha(ERRO, f"IA falhou: {e}")
                problemas += 1
    linha(OK if config.embeddings_provedor == "local" or config.embeddings_api_key else ERRO, f"Busca vetorial: {config.embeddings_provedor}")

    print("\n6) Segurança")
    linha(AVISO if config.simulador_ativo else OK, f"SIMULADOR_ATIVO={config.simulador_ativo}" + (" (em produção, deixe false ou proteja o acesso)" if config.simulador_ativo else ""))

    print("\n" + (f"{OK} Tudo certo para o modo escolhido." if not problemas else f"{ERRO} {problemas} problema(s) para resolver acima."))

    return int(bool(problemas))


if __name__ == "__main__":
    raise SystemExit(main())
