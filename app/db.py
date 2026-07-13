"""Persistencia minima em SQLite.

- sessoes: sessao de browser <-> usuario identificado (identificacao continua
  por usuario de rede sem senha ate a Fase 2/auth AD).
- chamados: estado de relay por THREAD (chamado), indexado por usuario_id
  (nao por sessao_id) - sobrevive a um novo login, porque "Meus chamados" e
  por usuario, nao por sessao de browser. A lista em si vem sempre do GLPI ao
  vivo (fonte da verdade); esta tabela e so o watermark de relay (ultimo
  followup visto, ultimo status, se ja entrou em acompanhamento, quando o
  usuario viu por ultimo - pro badge de nao lida).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

CAMINHO_BANCO = Path(__file__).resolve().parent.parent / "chatbot.db"


def conectar() -> sqlite3.Connection:
    conexao = sqlite3.connect(CAMINHO_BANCO)
    conexao.row_factory = sqlite3.Row
    return conexao


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def inicializar_banco() -> None:
    with conectar() as conexao:
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS sessoes (
                id TEXT PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                usuario_login TEXT NOT NULL,
                usuario_nome TEXT NOT NULL,
                papel TEXT NOT NULL DEFAULT 'colaborador',
                criado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS chamados (
                ticket_id INTEGER PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                ultimo_followup_id INTEGER NOT NULL DEFAULT 0,
                ultimo_status_glpi INTEGER,
                modo_acompanhamento INTEGER NOT NULL DEFAULT 0,
                criado_pelo_bot INTEGER NOT NULL DEFAULT 0,
                visto_em TEXT,
                atualizado_em TEXT NOT NULL
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS followups_usuario (
                ticket_id INTEGER NOT NULL,
                followup_id INTEGER NOT NULL,
                PRIMARY KEY (ticket_id, followup_id)
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS followups_tecnico (
                ticket_id INTEGER NOT NULL,
                followup_id INTEGER NOT NULL,
                tecnico_nome TEXT NOT NULL,
                PRIMARY KEY (ticket_id, followup_id)
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS chamados_vistos_tecnico (
                ticket_id INTEGER NOT NULL,
                tecnico_id INTEGER NOT NULL,
                visto_em TEXT NOT NULL,
                PRIMARY KEY (ticket_id, tecnico_id)
            )
            """
        )
        conexao.execute(
            """
            CREATE TABLE IF NOT EXISTS anexos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                followup_id INTEGER,
                users_id_autor INTEGER NOT NULL,
                nome_arquivo TEXT NOT NULL,
                mime_tipo TEXT NOT NULL,
                glpi_document_id INTEGER NOT NULL,
                criado_em TEXT NOT NULL
            )
            """
        )
        _migrar_tabela_chamados_antiga(conexao)
        _migrar_coluna_papel_sessoes(conexao)
        _migrar_coluna_criado_pelo_bot(conexao)


def _migrar_coluna_criado_pelo_bot(conexao: sqlite3.Connection) -> None:
    """Compat: chamados registrados antes da Fase 4 (fechamento automatico)
    nao tinham essa distincao. Default 0 (conservador - nao fecha sozinho
    chamados que a gente nao tem certeza que foram abertos pelo bot)."""
    colunas = {linha["name"] for linha in conexao.execute("PRAGMA table_info(chamados)")}
    if "criado_pelo_bot" not in colunas:
        conexao.execute("ALTER TABLE chamados ADD COLUMN criado_pelo_bot INTEGER NOT NULL DEFAULT 0")


def _migrar_coluna_papel_sessoes(conexao: sqlite3.Connection) -> None:
    """Compat: sessoes criadas antes da Fase 2 (auth AD) nao tinham papel."""
    colunas = {linha["name"] for linha in conexao.execute("PRAGMA table_info(sessoes)")}
    if "papel" not in colunas:
        conexao.execute("ALTER TABLE sessoes ADD COLUMN papel TEXT NOT NULL DEFAULT 'colaborador'")


def _migrar_tabela_chamados_antiga(conexao: sqlite3.Connection) -> None:
    """Compat: a tabela `chamados` do MVP era so um log (sessao_id, ticket_id),
    sem watermark de relay. Se ainda existir nesse formato antigo (coluna
    `sessao_id`), so recria vazia no formato novo - sem tentar chutar um
    watermark pros chamados ja existentes (ultimo_followup_id=0 geraria
    'novidade' falsa pra todo o historico real deles). O watermark correto
    e criado sob demanda, via relay.garantir_estado_chamado, buscando o
    estado atual real no GLPI na primeira vez que o hub/thread os ve."""
    colunas = {linha["name"] for linha in conexao.execute("PRAGMA table_info(chamados)")}
    if "sessao_id" not in colunas:
        return

    conexao.execute("DROP TABLE chamados")
    conexao.execute(
        """
        CREATE TABLE chamados (
            ticket_id INTEGER PRIMARY KEY,
            usuario_id INTEGER NOT NULL,
            ultimo_followup_id INTEGER NOT NULL DEFAULT 0,
            ultimo_status_glpi INTEGER,
            modo_acompanhamento INTEGER NOT NULL DEFAULT 0,
            visto_em TEXT,
            atualizado_em TEXT NOT NULL
        )
        """
    )


def criar_sessao(sessao_id: str, usuario: dict, papel: str) -> None:
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR REPLACE INTO sessoes (id, usuario_id, usuario_login, "
            "usuario_nome, papel, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
            (
                sessao_id, usuario["id"], usuario["login"], usuario["nome_completo"],
                papel, _agora(),
            ),
        )


def obter_sessao(sessao_id: str) -> sqlite3.Row | None:
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM sessoes WHERE id = ?", (sessao_id,)
        ).fetchone()


def registrar_chamado(usuario_id: int, ticket_id: int) -> None:
    """Cria o registro de watermark do thread na criacao do chamado.
    `visto_em` ja vai preenchido: o usuario acabou de criar o chamado nesta
    mesma conversa, entao ja "viu" - sem isso o thread nunca teria visto_em
    setado (a menos que o usuario abrisse /chamados/{id} manualmente depois)
    e o badge de nao lida nunca conseguiria acender de verdade.

    `criado_pelo_bot=1`: so chamados criados por este caminho (a triagem da
    IA) sao candidatos a fechamento automatico (Fase 4) - chamados abertos
    direto no GLPI (fora do app) nunca fecham sozinhos por causa da gente."""
    agora = _agora()
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR IGNORE INTO chamados "
            "(ticket_id, usuario_id, criado_pelo_bot, visto_em, atualizado_em) "
            "VALUES (?, ?, 1, ?, ?)",
            (ticket_id, usuario_id, agora, agora),
        )


def listar_chamados_abertos_do_bot(status_fechado: int) -> list[sqlite3.Row]:
    """Candidatos a checar no fechamento automatico (Fase 4): TODOS os
    chamados criados pelo proprio app que a gente ainda nao sabe que estao
    fechados - nao filtra por 'ja sabemos que esta resolvido', de proposito:
    se o usuario nao tem sessao ativa desde que o chamado foi resolvido
    (o caso mais comum pra algo parado ha 48h!), o watermark local nunca
    seria atualizado pra 5, e o candidato nunca apareceria numa query que
    exigisse ultimo_status_glpi=resolvido. O chamador confere o status real
    contra o GLPI ao vivo pra cada um."""
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM chamados WHERE criado_pelo_bot = 1 "
            "AND (ultimo_status_glpi IS NULL OR ultimo_status_glpi != ?)",
            (status_fechado,),
        ).fetchall()


def chamados_criados_pelo_bot(ticket_ids: list[int]) -> set[int]:
    """Quais desses tickets foram criados pelo app (pra decorar a Fila/Meus
    atendimentos do painel do tecnico com o icone 'via assistente') - uma
    query em lote, nao uma por linha da lista."""
    if not ticket_ids:
        return set()
    with conectar() as conexao:
        marcadores = ",".join("?" * len(ticket_ids))
        linhas = conexao.execute(
            f"SELECT ticket_id FROM chamados WHERE criado_pelo_bot = 1 "
            f"AND ticket_id IN ({marcadores})",
            ticket_ids,
        ).fetchall()
        return {linha["ticket_id"] for linha in linhas}


def criar_estado_chamado(
    usuario_id: int,
    ticket_id: int,
    *,
    ultimo_followup_id: int,
    ultimo_status_glpi: int,
    modo_acompanhamento: bool,
) -> None:
    """Baseline de watermark pra um chamado que o app esta vendo pela
    primeira vez (aberto fora do bot, ou anterior a este refactor) -
    ultimo_followup_id/status vem do estado JA existente no GLPI, pra nao
    gerar badge/evento falso de 'novidade' sobre historico antigo."""
    agora = _agora()
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR IGNORE INTO chamados (ticket_id, usuario_id, ultimo_followup_id, "
            "ultimo_status_glpi, modo_acompanhamento, visto_em, atualizado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ticket_id, usuario_id, ultimo_followup_id, ultimo_status_glpi,
                1 if modo_acompanhamento else 0, agora, agora,
            ),
        )


def listar_chamados_usuario(usuario_id: int) -> list[sqlite3.Row]:
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM chamados WHERE usuario_id = ?", (usuario_id,)
        ).fetchall()


def obter_estado_chamado(ticket_id: int) -> sqlite3.Row | None:
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM chamados WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()


def atualizar_estado_chamado(
    ticket_id: int,
    *,
    ultimo_followup_id: int | None = None,
    ultimo_status_glpi: int | None = None,
    modo_acompanhamento: bool | None = None,
    houve_novidade: bool = False,
) -> None:
    """`houve_novidade` controla o carimbo `atualizado_em` (usado pro badge
    de nao lida no hub) - SO deve ser True quando algo badge-worthy de fato
    aconteceu (novo followup/mudanca de status), nunca num poll de rotina
    sem novidade, senao o badge fica piscando sozinho a cada ciclo."""
    campos, valores = [], []
    if ultimo_followup_id is not None:
        campos.append("ultimo_followup_id = ?")
        valores.append(ultimo_followup_id)
    if ultimo_status_glpi is not None:
        campos.append("ultimo_status_glpi = ?")
        valores.append(ultimo_status_glpi)
    if modo_acompanhamento is not None:
        campos.append("modo_acompanhamento = ?")
        valores.append(1 if modo_acompanhamento else 0)
    if not campos:
        return
    if houve_novidade:
        campos.append("atualizado_em = ?")
        valores.append(_agora())
    valores.append(ticket_id)
    with conectar() as conexao:
        conexao.execute(f"UPDATE chamados SET {', '.join(campos)} WHERE ticket_id = ?", valores)


def marcar_chamado_visto(ticket_id: int) -> None:
    with conectar() as conexao:
        conexao.execute(
            "UPDATE chamados SET visto_em = ? WHERE ticket_id = ?", (_agora(), ticket_id)
        )


def marcar_followup_do_usuario(ticket_id: int, followup_id: int) -> None:
    """Registra que ESTE followup foi criado pela propria app em nome do
    usuario (mensagem dele relayada, ou explicacao de 'ainda nao
    resolveu') - nunca um followup de tecnico. O GLPI nao distingue isso
    sozinho (os dois usam o mesmo usuario de servico como autor), entao
    precisamos guardar essa proveniencia por fora pra nao reconstruir o
    historico confundindo a propria mensagem do usuario com uma resposta
    do suporte."""
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR IGNORE INTO followups_usuario (ticket_id, followup_id) VALUES (?, ?)",
            (ticket_id, followup_id),
        )


def followups_do_usuario(ticket_id: int) -> set[int]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT followup_id FROM followups_usuario WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()
        return {linha["followup_id"] for linha in linhas}


def marcar_followup_do_tecnico(ticket_id: int, followup_id: int, tecnico_nome: str) -> None:
    """Registra qual tecnico de verdade escreveu esse followup/nota - o
    GLPI so ve o token de servico compartilhado do app como autor
    (GLPI_APP_TOKEN/GLPI_USER_TOKEN), entao sem isso todo mundo apareceria
    como 'Suporte de TI' no painel, mesmo com varios tecnicos atuando no
    mesmo chamado."""
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR REPLACE INTO followups_tecnico (ticket_id, followup_id, tecnico_nome) "
            "VALUES (?, ?, ?)",
            (ticket_id, followup_id, tecnico_nome),
        )


def autores_tecnico_followups(ticket_id: int) -> dict[int, str]:
    with conectar() as conexao:
        linhas = conexao.execute(
            "SELECT followup_id, tecnico_nome FROM followups_tecnico WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchall()
        return {linha["followup_id"]: linha["tecnico_nome"] for linha in linhas}


def marcar_chamado_visto_tecnico(ticket_id: int, tecnico_id: int) -> None:
    """Quando ESSE tecnico abre a thread - usado pra saber se tem mensagem
    nova do colaborador desde a ultima vez que ele olhou (bolinha de
    notificacao no card de 'Em atendimento' do painel)."""
    with conectar() as conexao:
        conexao.execute(
            "INSERT OR REPLACE INTO chamados_vistos_tecnico (ticket_id, tecnico_id, visto_em) "
            "VALUES (?, ?, ?)",
            (ticket_id, tecnico_id, _agora()),
        )


def obter_vistos_tecnico(tecnico_id: int, ticket_ids: list[int]) -> dict[int, str]:
    """Em lote (nao 1 query por card) - visto_em de cada ticket pra esse
    tecnico, so os que ele ja abriu alguma vez (ausentes = nunca viu)."""
    if not ticket_ids:
        return {}
    with conectar() as conexao:
        marcadores = ",".join("?" * len(ticket_ids))
        linhas = conexao.execute(
            f"SELECT ticket_id, visto_em FROM chamados_vistos_tecnico "
            f"WHERE tecnico_id = ? AND ticket_id IN ({marcadores})",
            [tecnico_id, *ticket_ids],
        ).fetchall()
        return {linha["ticket_id"]: linha["visto_em"] for linha in linhas}


def registrar_anexo(
    ticket_id: int,
    users_id_autor: int,
    nome_arquivo: str,
    mime_tipo: str,
    glpi_document_id: int,
    followup_id: int | None = None,
) -> int:
    """Registra localmente um anexo ja enviado/vinculado no GLPI. followup_id
    None quando o anexo ainda nao esta ligado a um followup de verdade (ex:
    mandado no modo 'ajuda' antes do chamado existir, ou numa mensagem que a
    IA decidiu nao registrar como followup) - nesse caso ele e mostrado
    junto do card de descricao do chamado, nunca perdido silenciosamente."""
    with conectar() as conexao:
        cursor = conexao.execute(
            "INSERT INTO anexos (ticket_id, followup_id, users_id_autor, nome_arquivo, "
            "mime_tipo, glpi_document_id, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, followup_id, users_id_autor, nome_arquivo, mime_tipo, glpi_document_id, _agora()),
        )
        return cursor.lastrowid


def listar_anexos_chamado(ticket_id: int) -> list[sqlite3.Row]:
    """Todos os anexos de um chamado, em ordem de insercao - base pra
    reconstruir o campo 'anexos' de cada mensagem (colaborador e tecnico)."""
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM anexos WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()


def anexos_do_followup(ticket_id: int, followup_id: int) -> list[sqlite3.Row]:
    """So os anexos de UM followup especifico - usado pelo relay pra decorar
    o evento SSE 'seguimento_tecnico' sem recarregar todos os anexos do
    chamado."""
    with conectar() as conexao:
        return conexao.execute(
            "SELECT * FROM anexos WHERE ticket_id = ? AND followup_id = ?",
            (ticket_id, followup_id),
        ).fetchall()
