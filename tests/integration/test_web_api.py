"""
Integration tests for the Flask web API (web/app.py).

The app is started once per session by the `api_server` fixture in conftest.py.
All tests share that session-scoped server instance.

Auth model:
  - Requests from 192.168.4.x (AP subnet) are allowed without a token.
  - All other requests need a valid Bearer token.
  - Write endpoints (POST/DELETE) always require a token regardless of source IP.
"""

from __future__ import annotations

import pytest
import requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_with_valid_token_returns_200(self, api_server):
        """Authenticated GET /api/status should return 200 with expected keys."""
        resp = requests.get(
            f"{api_server['base_url']}/api/status",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "uplink" in data
        assert "system" in data
        assert "vpn" in data

    def test_status_unauthenticated_non_ap_returns_401(self, api_server):
        """GET /api/status without token from non-AP IP should return 401."""
        resp = requests.get(
            f"{api_server['base_url']}/api/status",
            timeout=5,
        )
        assert resp.status_code == 401

    def test_status_unauthenticated_from_ap_subnet(self, api_server, monkeypatch):
        """GET /api/status from AP subnet (192.168.4.x) needs no token.

        We can't easily spoof the source IP of a real TCP connection, so we
        test the _is_ap_client() logic via the Flask test client instead.
        """
        app_module = api_server["app_module"]
        with app_module.app.test_client() as client:
            # Spoof REMOTE_ADDR to be in the AP subnet
            resp = client.get(
                "/api/status",
                environ_base={"REMOTE_ADDR": "192.168.4.10"},
            )
        assert resp.status_code == 200

    def test_status_with_wrong_token_returns_401(self, api_server):
        """GET /api/status with a bad token should return 401."""
        resp = requests.get(
            f"{api_server['base_url']}/api/status",
            headers={"Authorization": "Bearer wrongtoken"},
            timeout=5,
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_config_get_returns_known_keys(self, api_server):
        """GET /api/config should return the keys from the defaults file."""
        resp = requests.get(
            f"{api_server['base_url']}/api/config",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ENABLE_HOTSPOT" in data
        assert "ENABLE_WIREGUARD" in data

    def test_config_write_invalid_bool_value_returns_400(self, api_server):
        """POST /api/config with ENABLE_HOTSPOT='banana' should return 400."""
        resp = requests.post(
            f"{api_server['base_url']}/api/config",
            json={"ENABLE_HOTSPOT": "banana"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data

    def test_config_write_valid_bool_value_returns_200(self, api_server):
        """POST /api/config with ENABLE_HOTSPOT='1' should return 200."""
        resp = requests.post(
            f"{api_server['base_url']}/api/config",
            json={"ENABLE_HOTSPOT": "1"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert "ENABLE_HOTSPOT" in data.get("updated", [])

    def test_config_write_unknown_key_returns_400(self, api_server):
        """POST /api/config with a key not in defaults file should return 400."""
        resp = requests.post(
            f"{api_server['base_url']}/api/config",
            json={"TOTALLY_UNKNOWN_KEY": "1"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 400

    def test_config_write_without_auth_returns_401(self, api_server):
        """POST /api/config without auth should return 401."""
        resp = requests.post(
            f"{api_server['base_url']}/api/config",
            json={"ENABLE_HOTSPOT": "1"},
            timeout=5,
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/vpn/wireguard/peer
# ---------------------------------------------------------------------------

class TestWireGuardPeer:
    _VALID_KEY = "A" * 43 + "="

    def test_add_peer_with_bad_pubkey_returns_400(self, api_server):
        """POST /api/vpn/wireguard/peer with invalid public_key returns 400."""
        resp = requests.post(
            f"{api_server['base_url']}/api/vpn/wireguard/peer",
            json={"public_key": "notavalidkey", "allowed_ips": "10.0.0.2/32"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_add_peer_with_valid_key_returns_200(self, api_server):
        """POST /api/vpn/wireguard/peer with a valid key returns 200."""
        resp = requests.post(
            f"{api_server['base_url']}/api/vpn/wireguard/peer",
            json={"public_key": self._VALID_KEY, "allowed_ips": "10.99.0.2/32"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True

    def test_delete_nonexistent_peer_returns_404(self, api_server):
        """DELETE /api/vpn/wireguard/peer/<key> for unknown key returns 404."""
        nonexistent_key = "B" * 43 + "="
        resp = requests.delete(
            f"{api_server['base_url']}/api/vpn/wireguard/peer/{nonexistent_key}",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 404

    def test_delete_peer_with_invalid_key_returns_400(self, api_server):
        """DELETE /api/vpn/wireguard/peer/<bad_key> returns 400."""
        resp = requests.delete(
            f"{api_server['base_url']}/api/vpn/wireguard/peer/notakey",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 400

    def test_add_then_delete_peer_roundtrip(self, api_server):
        """Add a peer then delete it; verify it's gone (404 on second delete)."""
        unique_key = "C" * 43 + "="

        add_resp = requests.post(
            f"{api_server['base_url']}/api/vpn/wireguard/peer",
            json={"public_key": unique_key, "allowed_ips": "10.88.0.2/32"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert add_resp.status_code == 200

        del_resp = requests.delete(
            f"{api_server['base_url']}/api/vpn/wireguard/peer/{unique_key}",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert del_resp.status_code == 200

        del_again = requests.delete(
            f"{api_server['base_url']}/api/vpn/wireguard/peer/{unique_key}",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert del_again.status_code == 404


# ---------------------------------------------------------------------------
# /api/privacy/profile
# ---------------------------------------------------------------------------

class TestPrivacyProfile:
    def test_get_profile_returns_active_and_profiles_keys(self, api_server):
        """GET /api/privacy/profile returns JSON with 'active' and 'profiles'."""
        resp = requests.get(
            f"{api_server['base_url']}/api/privacy/profile",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "profiles" in data
        assert isinstance(data["profiles"], list)
        assert len(data["profiles"]) > 0

    def test_set_valid_profile_returns_200(self, api_server):
        """POST /api/privacy/profile with a valid name returns 200."""
        resp = requests.post(
            f"{api_server['base_url']}/api/privacy/profile",
            json={"profile": "private"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("active") == "private"

    def test_set_invalid_profile_returns_400(self, api_server):
        """POST /api/privacy/profile with unknown name returns 400."""
        resp = requests.post(
            f"{api_server['base_url']}/api/privacy/profile",
            json={"profile": "invalid"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_profile_persists_after_set(self, api_server):
        """After setting a profile, GET returns the updated active profile."""
        requests.post(
            f"{api_server['base_url']}/api/privacy/profile",
            json={"profile": "paranoid"},
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        resp = requests.get(
            f"{api_server['base_url']}/api/privacy/profile",
            headers=auth_headers(api_server["token"]),
            timeout=5,
        )
        assert resp.status_code == 200
        assert resp.json()["active"] == "paranoid"

    def test_set_profile_without_auth_returns_401(self, api_server):
        """POST /api/privacy/profile without auth returns 401."""
        resp = requests.post(
            f"{api_server['base_url']}/api/privacy/profile",
            json={"profile": "standard"},
            timeout=5,
        )
        assert resp.status_code == 401
