"""Resolucao do colaborador que esta conversando no chat, e do papel dele
no app (colaborador/tecnico).

Isolado neste modulo de proposito: a resolucao de identidade (autenticacao)
fica em app/autenticacao_ad.py (LDAP hoje, SSO Kerberos na v3 - so o corpo
daquela funcao muda). Aqui so ficam as consultas ao GLPI que dependem do
usuario ja autenticado.
"""
from __future__ import annotations

import os

from app.glpi import buscar_perfis_usuario, buscar_usuario_por_login

PAPEL_TECNICO = "tecnico"
PAPEL_COLABORADOR = "colaborador"


async def resolver_usuario(login: str) -> dict | None:
    """Retorna {id, login, nome_completo} se o usuario existe e esta ativo
    no GLPI, senao None."""
    usuario = await buscar_usuario_por_login(login.strip())
    if usuario and usuario["ativo"]:
        return usuario
    return None


def _perfis_tecnico_ids() -> set[int]:
    bruto = os.environ.get("PERFIS_TECNICO_IDS", "")
    return {int(item) for item in bruto.split(",") if item.strip()}


async def resolver_papel(usuario_id: int) -> str:
    """PAPEL_TECNICO se o usuario tiver algum perfil GLPI listado em
    PERFIS_TECNICO_IDS (.env), senao PAPEL_COLABORADOR. Tecnico tambem
    abre chamado como colaborador - o papel so libera o painel (Fase 6)."""
    perfis_usuario = set(await buscar_perfis_usuario(usuario_id))
    if perfis_usuario & _perfis_tecnico_ids():
        return PAPEL_TECNICO
    return PAPEL_COLABORADOR
