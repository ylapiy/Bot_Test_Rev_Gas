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


def main() -> None:
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
    else:
        linha(OK if config.erp_base_url else ERRO, f"ERP_BASE_URL {'= ' + config.erp_base_url if config.erp_base_url else 'VAZIO'}")
        linha(OK if config.erp_token or config.erp_usuario else ERRO, "Credencial do ERP " + ("preenchida" if config.erp_token or config.erp_usuario else "VAZIA"))
        linha(AVISO, "Confira se config/erp_revgas.json foi ajustado com a documentação real da API do RevGás")
        if a.online:
            try:
                from app.erp import obter_erp

                fs = obter_erp().listar_filiais()
                linha(OK, f"ERP respondeu: {len(fs)} filial(is)")
            except Exception as e:  # noqa: BLE001
                linha(ERRO, f"ERP não respondeu: {e}")
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


if __name__ == "__main__":
    main()
