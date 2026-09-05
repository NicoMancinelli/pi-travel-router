#!/bin/bash
# install/08-security.sh — SSH hardening, 2FA, auto-updates
# Defines run_security(). Source this file; do not execute directly.

run_security() {
    # ── fail2ban ─────────────────────────────────────────────────────────────────
    section "fail2ban"

    if ! command -v fail2ban-server > /dev/null 2>&1; then
        run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban
    fi
    install_file config/fail2ban/jail.d/travel-router.conf \
        /etc/fail2ban/jail.d/travel-router.conf 644
    install_file config/fail2ban/filter.d/travel-router-web.conf \
        /etc/fail2ban/filter.d/travel-router-web.conf 644
    run_or_dry systemctl enable fail2ban
    run_or_dry systemctl restart fail2ban
    ok "fail2ban configured (SSH + web dashboard jails active)"

    # ── SSH hardening ────────────────────────────────────────────────────────────
    section "SSH hardening"

    install_file config/sshd-travel-router.conf /etc/ssh/sshd_config.d/99-travel-router.conf 644

    local _ADMIN_USER="${SUDO_USER:-}"
    if [[ -z "$_ADMIN_USER" ]]; then
        _ADMIN_USER=$(logname 2>/dev/null || echo "${USER:-root}")
    fi
    local _ADMIN_HOME
    _ADMIN_HOME=$(getent passwd "$_ADMIN_USER" 2>/dev/null | cut -d: -f6)
    _ADMIN_HOME="${_ADMIN_HOME:-/root}"

    if [[ -n "${SSH_ADMIN_KEY:-}" ]]; then
        # Strip embedded newlines that could inject extra lines into authorized_keys
        SSH_ADMIN_KEY="$(printf '%s' "$SSH_ADMIN_KEY" | tr -d '\n\r')"
        [[ "$SSH_ADMIN_KEY" =~ ^(ssh-|ecdsa-|sk-) ]] || \
            die "Invalid SSH key format: SSH_ADMIN_KEY must start with ssh-, ecdsa-, or sk-"
        mkdir -p "$_ADMIN_HOME/.ssh"
        chmod 700 "$_ADMIN_HOME/.ssh"
        touch "$_ADMIN_HOME/.ssh/authorized_keys"
        chmod 600 "$_ADMIN_HOME/.ssh/authorized_keys"
        if ! grep -qF "$SSH_ADMIN_KEY" "$_ADMIN_HOME/.ssh/authorized_keys" 2>/dev/null; then
            printf '%s\n' "$SSH_ADMIN_KEY" >> "$_ADMIN_HOME/.ssh/authorized_keys"
        fi
        chown -R "$_ADMIN_USER:$_ADMIN_USER" "$_ADMIN_HOME/.ssh"
        grep -q "PasswordAuthentication" /etc/ssh/sshd_config.d/99-travel-router.conf 2>/dev/null || \
            echo "PasswordAuthentication no" >> /etc/ssh/sshd_config.d/99-travel-router.conf
        ok "SSH public key added for $_ADMIN_USER; password auth disabled"
    else
        ok "No SSH key provided — password auth remains enabled"
        ok "Add later: echo '<pubkey>' >> ~/.ssh/authorized_keys"
    fi

    run_or_dry systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
    ok "sshd restarted with hardened config"

    # ── Unattended security updates ──────────────────────────────────────────────
    section "Unattended security updates"

    if [[ "${ENABLE_AUTO_UPDATES:-0}" = "1" ]]; then
        install_file config/50unattended-upgrades         /etc/apt/apt.conf.d/50unattended-upgrades 644
        install_file config/20auto-upgrades               /etc/apt/apt.conf.d/20auto-upgrades 644
        install_file config/99-travel-router-notify.conf  /etc/apt/apt.conf.d/99-travel-router-notify 644
        run_or_dry systemctl enable --now unattended-upgrades 2>/dev/null || true
        ok "Auto security updates enabled (reboot at 03:30 when required)"
    else
        ok "Auto security updates disabled (set ENABLE_AUTO_UPDATES=1 to activate)"
    fi
}
