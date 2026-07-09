"""Polling de acompanhamento: followups publicos e mudanca de status de
TODOS os chamados abertos de cada usuario logado agora (1 task global em
background, watermark por chamado persistido no SQLite - nao mais 1 chamado
"ativo" por sessao).

Followups privados nunca chegam aqui - ja ficam de fora em
glpi.listar_followups_publicos.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app import db
from app.glpi import (
    HORAS_CONFIRMACAO_SOLUCAO,
    ROTULOS_STATUS,
    STATUS_FECHADO,
    STATUS_RESOLVIDO,
    STATUS_RESOLVIDO_OU_FECHADO,
    aprovar_solucao,
    listar_followups_publicos,
    obter_chamado_completo,
    obter_chamado_resumo,
    obter_solucao,
    obter_usuario_por_id,
    texto_prazo_confirmacao,
)

logger = logging.getLogger("chatbot.relay")

INTERVALO_SEGUNDOS = 15
INTERVALO_FECHAMENTO_AUTOMATICO_SEGUNDOS = 60 * 60


async def garantir_estado_chamado(usuario_id: int, ticket_id: int) -> None:
    """Baseline pra chamados que o app ainda nao conhece (nao foram criados
    nesta sessao - abertos direto no GLPI, ou chamados de antes deste
    refactor). Sem isso, o primeiro poll trataria todo o historico existente
    como 'novidade'."""
    if db.obter_estado_chamado(ticket_id):
        return

    followups = await listar_followups_publicos(ticket_id)
    ultimo_followup_id = followups[-1]["id"] if followups else 0
    resumo = await obter_chamado_resumo(ticket_id)
    db.criar_estado_chamado(
        usuario_id,
        ticket_id,
        ultimo_followup_id=ultimo_followup_id,
        ultimo_status_glpi=resumo["status"],
        modo_acompanhamento=resumo["status"] not in (1, *STATUS_RESOLVIDO_OU_FECHADO),
    )


async def verificar_chamado(ticket_id: int, estado_db) -> list[dict]:
    """Confere novidades de 1 chamado a partir do watermark salvo em
    `estado_db` (linha da tabela `chamados`) e persiste o watermark
    atualizado. Retorna os eventos novos a empurrar pro chat/badge."""
    eventos: list[dict] = []
    ultimo_followup_id = estado_db["ultimo_followup_id"]
    ultimo_status = estado_db["ultimo_status_glpi"]
    modo_acompanhamento = bool(estado_db["modo_acompanhamento"])

    followups = await listar_followups_publicos(ticket_id)
    novos = [f for f in followups if f["id"] > ultimo_followup_id]

    # followups que a PROPRIA app criou em nome do usuario (mensagem dele
    # relayada, explicacao de "ainda nao resolveu") nunca contam como
    # "o tecnico respondeu" - o GLPI nao distingue isso sozinho (mesmo
    # usuario de servico como autor dos dois).
    ids_usuario = db.followups_do_usuario(ticket_id)
    novos_do_tecnico = [f for f in novos if f["id"] not in ids_usuario]
    primeira_vez = not modo_acompanhamento and bool(novos_do_tecnico)

    for indice, followup in enumerate(novos_do_tecnico):
        autor = await obter_usuario_por_id(followup["users_id"])
        nome_autor = autor["nome_completo"] if autor else "Suporte"

        if indice == 0 and primeira_vez:
            modo_acompanhamento = True
            logger.info(
                "chamado=%s entrou em modo acompanhamento (1o followup publico, autor=%s)",
                ticket_id, nome_autor,
            )
            eventos.append({"tipo": "tecnico_assumiu", "nome": nome_autor})

        eventos.append(
            {"tipo": "seguimento_tecnico", "autor": nome_autor, "texto": followup["conteudo"]}
        )

    if novos:
        ultimo_followup_id = max(ultimo_followup_id, max(f["id"] for f in novos))

    resumo = await obter_chamado_resumo(ticket_id)
    if ultimo_status is None:
        # baseline: chamado recem-criado (registrar_chamado nao sabe o status
        # inicial) - so grava, sem evento.
        ultimo_status = resumo["status"]
    elif resumo["status"] != ultimo_status:
        rotulo = ROTULOS_STATUS.get(resumo["status"], "Atualizado")

        if resumo["status"] in STATUS_RESOLVIDO_OU_FECHADO:
            solucao = await obter_solucao(ticket_id)
            aguardando = resumo["status"] == STATUS_RESOLVIDO
            evento_resolvido = {
                "tipo": "resolvido",
                "texto": solucao or f"Seu chamado #{ticket_id} foi marcado como {rotulo.lower()}.",
                "mostrarBotoes": aguardando,
            }
            if aguardando:
                completo = await obter_chamado_completo(ticket_id)
                evento_resolvido["prazoTexto"] = texto_prazo_confirmacao(completo["data_resolucao"])
            eventos.append(evento_resolvido)
            modo_acompanhamento = False
        else:
            eventos.append(
                {
                    "tipo": "status",
                    "status": resumo["status"],
                    "texto": f"O status do seu chamado #{ticket_id} mudou pra: {rotulo}",
                }
            )
        ultimo_status = resumo["status"]

    db.atualizar_estado_chamado(
        ticket_id,
        ultimo_followup_id=ultimo_followup_id,
        ultimo_status_glpi=ultimo_status,
        modo_acompanhamento=modo_acompanhamento,
        houve_novidade=bool(eventos),
    )
    return eventos


async def loop_polling(sessoes_ativas: dict) -> None:
    logger.info("polling de acompanhamento iniciado (intervalo=%ss)", INTERVALO_SEGUNDOS)
    while True:
        await asyncio.sleep(INTERVALO_SEGUNDOS)

        usuarios_ja_processados: set[int] = set()

        for sessao_id, sessao in list(sessoes_ativas.items()):
            usuario_id = sessao["usuario"]["id"]
            if usuario_id in usuarios_ja_processados:
                continue
            usuarios_ja_processados.add(usuario_id)

            # Dirigido pelo watermark LOCAL (nao pelo status ao vivo do GLPI):
            # um chamado so sai do polling depois que verificar_chamado
            # observa e registra a transicao pra resolvido/fechado. Filtrar
            # antes pelo status ao vivo faria o app pular direto pra
            # "ja esta resolvido" sem nunca emitir o evento de resolucao.
            pendentes = [
                linha
                for linha in db.listar_chamados_usuario(usuario_id)
                if linha["ultimo_status_glpi"] is None
                or linha["ultimo_status_glpi"] not in STATUS_RESOLVIDO_OU_FECHADO
            ]

            for estado_db in pendentes:
                ticket_id = estado_db["ticket_id"]
                try:
                    eventos = await verificar_chamado(ticket_id, estado_db)
                except Exception:
                    logger.exception(
                        "erro no polling usuario=%s chamado=%s", usuario_id, ticket_id
                    )
                    continue

                if not eventos:
                    continue

                logger.info(
                    "usuario=%s chamado=%s eventos_novos=%d", usuario_id, ticket_id, len(eventos)
                )
                # empurra pra TODAS as sessoes de browser abertas deste usuario
                # (ex: 2 abas) - eventos_pendentes fica por ticket_id, nao mais
                # um unico balde da sessao.
                for outra_sessao in sessoes_ativas.values():
                    if outra_sessao["usuario"]["id"] != usuario_id:
                        continue
                    outra_sessao.setdefault("eventos_pendentes", {}).setdefault(
                        ticket_id, []
                    ).extend(eventos)


async def fechar_se_vencido(ticket_id: int, estado_db) -> bool:
    """Fase 4 - fechamento automatico: se este chamado foi ABERTO PELO BOT
    (nunca um chamado criado direto no GLPI) e continua resolvido ha mais
    de HORAS_CONFIRMACAO_SOLUCAO sem o usuario confirmar, aprova a solucao
    sozinho (mesmo caminho do botao "Funcionou"). Confere contra o GLPI ao
    vivo antes de agir - o watermark local pode estar desatualizado (ex:
    checagem oportunista no hub, chamado ainda nao passou por nenhum poll
    desde que foi resolvido) - so PROPOSITALMENTE nao filtra aqui por
    ultimo_status_glpi==RESOLVIDO, so por criado_pelo_bot. Quem chama pra
    uma varredura em massa (loop_fechamento_automatico) ja filtra isso na
    query do banco, antes de chegar aqui. Retorna True se fechou agora."""
    if not estado_db["criado_pelo_bot"]:
        return False

    completo = await obter_chamado_completo(ticket_id)
    if completo["status"] != STATUS_RESOLVIDO or not completo["data_resolucao"]:
        return False

    resolvido_em = datetime.strptime(completo["data_resolucao"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() - resolvido_em < timedelta(hours=HORAS_CONFIRMACAO_SOLUCAO):
        return False

    await aprovar_solucao(ticket_id)
    # houve_novidade=True de proposito: o usuario nao foi quem agiu aqui,
    # entao isso PRECISA acender o badge de nao lida da proxima vez que ele
    # abrir o hub (diferente do botao "Funcionou", onde e o proprio usuario
    # que ja esta vendo a tela na hora).
    db.atualizar_estado_chamado(
        ticket_id, ultimo_status_glpi=STATUS_FECHADO, modo_acompanhamento=False, houve_novidade=True
    )
    logger.info(
        "chamado=%s fechado automaticamente (sem confirmacao em %sh)",
        ticket_id, HORAS_CONFIRMACAO_SOLUCAO,
    )
    return True


async def loop_fechamento_automatico() -> None:
    """Varredura horaria (Fase 4): fecha sozinho chamados abertos pelo bot
    que ficaram resolvidos sem confirmacao do usuario por tempo demais.
    Roda independente de quem esta logado no momento - diferente do
    loop_polling, que so olha usuarios com sessao de browser ativa."""
    logger.info(
        "fechamento automatico iniciado (a cada %sh, prazo=%sh)",
        INTERVALO_FECHAMENTO_AUTOMATICO_SEGUNDOS // 3600, HORAS_CONFIRMACAO_SOLUCAO,
    )
    while True:
        try:
            candidatos = db.listar_chamados_abertos_do_bot(STATUS_FECHADO)
            for estado_db in candidatos:
                try:
                    await fechar_se_vencido(estado_db["ticket_id"], estado_db)
                except Exception:
                    logger.exception(
                        "erro ao verificar fechamento automatico chamado=%s", estado_db["ticket_id"]
                    )
        except Exception:
            logger.exception("erro no loop de fechamento automatico")
        await asyncio.sleep(INTERVALO_FECHAMENTO_AUTOMATICO_SEGUNDOS)
