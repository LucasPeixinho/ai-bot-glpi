"""Autenticacao contra o Active Directory via bind LDAP direto.

Isolada nesta unica funcao (`autenticar`) de proposito: a troca futura por
SSO Kerberos (v3, header REMOTE_USER do Apache) muda so o corpo desta
funcao / o jeito de chegar no `login` ja confirmado - o resto do fluxo de
login em main.py nao precisa mudar.

NUNCA loga a senha. Falha de conexao com o AD (servidor fora, rede) vira
LDAPIndisponivel - o chamador deve mostrar mensagem amigavel e nunca a
stack trace pro usuario.
"""
from __future__ import annotations

import asyncio
import logging
import os

from ldap3 import SIMPLE, Connection, Server
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError

logger = logging.getLogger("chatbot.auth_ad")


class LDAPIndisponivel(Exception):
    """Servidor LDAP fora do ar / inacessivel - erro de infra, nao de
    credencial invalida."""


def _formatos_bind(username: str) -> list[str]:
    """Tenta os 2 formatos de bind mais comuns em AD, sem precisar que o
    usuario final saiba qual o dominio usa: primeiro UPN
    (usuario@dominio.completo), depois NetBIOS (DOMINIO\\usuario) usando o
    primeiro rotulo do dominio como aproximacao do nome NetBIOS."""
    dominio = os.environ["LDAP_DOMAIN"]
    formatos = [f"{username}@{dominio}"]
    dominio_curto = dominio.split(".")[0]
    if dominio_curto.lower() != dominio.lower():
        formatos.append(f"{dominio_curto}\\{username}")
    return formatos


def _autenticar_sync(username: str, senha: str) -> bool:
    ldap_server = os.environ.get("LDAP_SERVER")
    ldap_domain = os.environ.get("LDAP_DOMAIN")
    if not ldap_server or not ldap_domain:
        raise LDAPIndisponivel("LDAP_SERVER/LDAP_DOMAIN nao configurados no .env")

    server = Server(ldap_server, connect_timeout=5)
    erro_conexao: Exception | None = None

    for user_str in _formatos_bind(username):
        try:
            with Connection(
                server, user=user_str, password=senha,
                authentication=SIMPLE, receive_timeout=5,
            ) as conexao:
                if conexao.bind():
                    return True
        except LDAPSocketOpenError as exc:
            erro_conexao = exc
            continue
        except LDAPException:
            logger.exception("erro LDAP tentando formato de bind (usuario=%s)", username)
            continue

    if erro_conexao is not None:
        logger.error("servidor LDAP inacessivel: %s", erro_conexao)
        raise LDAPIndisponivel(str(erro_conexao))
    return False


async def autenticar(username: str, senha: str) -> bool:
    """True se usuario+senha batem no AD. Levanta LDAPIndisponivel se o
    servidor nao respondeu (trate diferente de credencial invalida na tela
    de login)."""
    return await asyncio.to_thread(_autenticar_sync, username, senha)


async def _testar_cli() -> None:
    """Teste manual do bind real, sem passar pela web: `python -m
    app.autenticacao_ad`. A senha e lida com getpass (nunca aparece na tela
    nem em historico de shell/log)."""
    import getpass

    username = input("Usuario de rede: ").strip()
    senha = getpass.getpass("Senha: ")
    try:
        ok = await autenticar(username, senha)
    except LDAPIndisponivel as exc:
        print(f"LDAP indisponivel: {exc}")
        return
    print("Autenticado com sucesso!" if ok else "Usuario ou senha incorretos.")


if __name__ == "__main__":
    asyncio.run(_testar_cli())
