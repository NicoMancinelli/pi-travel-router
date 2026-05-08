#!/bin/bash
# install/08-security.sh — SSH hardening, 2FA, auto-updates
# Defines run_security(). Source this file; do not execute directly.

run_security() {
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

    # ── SSH 2FA (TOTP) ───────────────────────────────────────────────────────────
    section "SSH 2FA (TOTP)"

    install_file scripts/setup-2fa.sh /usr/local/bin/setup-2fa.sh 755

    if [[ "${ENABLE_2FA:-0}" = "1" ]]; then
        run_or_dry env DEBIAN_FRONTEND=noninteractive apt-get install -y libpam-google-authenticator 2>/dev/null || true
        install_file config/sshd-2fa.conf /etc/ssh/sshd_config.d/98-travel-router-2fa.conf 644
        if ! grep -q "pam_google_authenticator" /etc/pam.d/sshd 2>/dev/null; then
            printf "auth required pam_google_authenticator.so nullok\n" >> /etc/pam.d/sshd
        fi
        run_or_dry systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
        ok "SSH 2FA enabled — run: sudo -u \$(logname) setup-2fa.sh to configure TOTP"
        # Warn for users who have not yet configured TOTP
        while IFS=: read -r _u _ _uid _ _ _home _shell; do
            [[ "$_uid" -lt 1000 && "$_u" != "root" ]] && continue
            [[ "$_shell" == */false || "$_shell" == */nologin ]] && continue
            [[ -f "$_home/.google_authenticator" ]] && continue
            warn "2FA not configured for user $_u — run: sudo -u $_u setup-2fa.sh"
        done < /etc/passwd
    else
        ok "SSH 2FA disabled (set ENABLE_2FA=1 then run setup-2fa.sh)"
    fi

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

    # ── Scheduled AP disable ─────────────────────────────────────────────────────
    section "Scheduled AP disable"

    if [[ "${ENABLE_AP_SCHEDULE:-0}" = "1" ]]; then
        mkdir -p /etc/systemd/system/ap-disable.timer.d
        cat > /etc/systemd/system/ap-disable.timer.d/time.conf << EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* ${AP_DISABLE_TIME:-02:00}:00
EOF
        mkdir -p /etc/systemd/system/ap-enable.timer.d
        cat > /etc/systemd/system/ap-enable.timer.d/time.conf << EOF
[Timer]
OnCalendar=
OnCalendar=*-*-* ${AP_ENABLE_TIME:-07:00}:00
EOF
        systemctl daemon-reload
        run_or_dry systemctl enable ap-disable.timer ap-enable.timer 2>/dev/null || true
        ok "AP schedule enabled: disable at ${AP_DISABLE_TIME:-02:00}, re-enable at ${AP_ENABLE_TIME:-07:00}"
    else
        systemctl disable ap-disable.timer ap-enable.timer 2>/dev/null || true
        ok "AP schedule disabled (set ENABLE_AP_SCHEDULE=1 to activate)"
    fi
}
