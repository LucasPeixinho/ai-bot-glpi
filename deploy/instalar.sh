#!/usr/bin/env bash
# Runbook de instalacao da Fase 7 (deploy) do chatbot-glpi.
# Idempotente: pode rodar mais de uma vez sem quebrar nada.
#
# Precisa de sudo. Rode manualmente, revisando cada bloco antes:
#   sudo bash /opt/chatbot-glpi/deploy/instalar.sh
#
# ESTADO ATUAL: o servico roda como usuario "administrator" (reaproveita o
# login pessoal do Claude Code) porque ainda nao ha ANTHROPIC_API_KEY
# dedicada no .env. Isso e temporario - ver bloco "TROCA PRO USUARIO
# DEDICADO" no final deste arquivo pra quando a chave chegar.

set -euo pipefail

PROJETO=/opt/chatbot-glpi

if [ "$(id -u)" -ne 0 ]; then
    echo "Rode com sudo: sudo bash $0" >&2
    exit 1
fi

if grep -qE '^# *ANTHROPIC_API_KEY=\s*$|^ANTHROPIC_API_KEY=\s*$' "$PROJETO/.env"; then
    echo "AVISO: ANTHROPIC_API_KEY ainda nao preenchida no .env." >&2
    echo "       Rodando mesmo assim com login pessoal (User=administrator" >&2
    echo "       no .service). Isso e temporario - ver o topo deste script." >&2
fi

echo "==> mcp-glpi global (evita depender do cache npx de um usuario especifico)"
if ! npm list -g --depth=0 2>/dev/null | grep -q mcp-glpi; then
    npm install -g mcp-glpi
else
    echo "    ja instalado, ok."
fi

echo "==> Servico systemd"
cp "$PROJETO/deploy/chatbot-glpi.service" /etc/systemd/system/chatbot-glpi.service
systemctl daemon-reload
systemctl enable --now chatbot-glpi
sleep 2
systemctl --no-pager status chatbot-glpi || true

echo "==> Apache (site chat.cdp.lub)"
a2enmod proxy proxy_http headers >/dev/null
ln -sf "$PROJETO/deploy/apache-chat.conf" /etc/apache2/sites-available/chatbot-glpi.conf
a2ensite chatbot-glpi >/dev/null
apache2ctl configtest
systemctl reload apache2

echo
echo "==> Pronto. Verificacoes:"
echo "    journalctl -u chatbot-glpi -f          (logs do servico)"
echo "    curl -sI http://127.0.0.1:8100/        (app respondendo localmente)"
echo "    http://chat.cdp.lub/                    (depois do DNS apontar pro 192.168.0.241)"

# ---------------------------------------------------------------------------
# TROCA PRO USUARIO DEDICADO (rodar quando a ANTHROPIC_API_KEY chegar)
# ---------------------------------------------------------------------------
# 1. Editar /opt/chatbot-glpi/.env: descomentar e preencher ANTHROPIC_API_KEY
# 2. Editar /opt/chatbot-glpi/deploy/chatbot-glpi.service: trocar
#    User=administrator / Group=administrator para User=chatbot-glpi /
#    Group=chatbot-glpi
# 3. Rodar (com sudo):
#      useradd --system --create-home --home-dir /var/lib/chatbot-glpi \
#          --shell /usr/sbin/nologin chatbot-glpi
#      usermod -aG administrator chatbot-glpi
#      chmod 640 /opt/chatbot-glpi/.env
#      chmod g+w /opt/chatbot-glpi /opt/chatbot-glpi/chatbot.db
#      bash /opt/chatbot-glpi/deploy/instalar.sh   # reaplica o .service novo
