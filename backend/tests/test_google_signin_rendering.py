"""Tests for Этап 3 — Google Identity Services button HTML rendering.

Verifies that the `_google_signin_block()` helper in main.py correctly
gates the GIS button on `GOOGLE_AUTH_ENABLED`, substitutes it into the
`{{GOOGLE_SIGNIN_BLOCK}}` placeholder in both activate.html and
client.html, and that the rendered HTML is well-formed:
  * absent when the flag is off (no leftover `{{GOOGLE_SIGNIN_BLOCK}}`
    leaks into served HTML — placeholder is replaced with empty string).
  * present when the flag is on, with the configured client_id, the
    callback name `handleGoogleCredential`, and the script source URL.
  * the chain into `/api/devices/claim/complete` for the activate.html
    flow when `?code=…` is present.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def google_enabled(monkeypatch):
    """Same shape as the existing google_enabled fixture in test_google_auth.py
    — flip the env + ensure client_id is non-empty so is_google_auth_enabled()
    and google_client_id() read the right values via `_google_signin_block()`."""
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "1")
    monkeypatch.setenv(
        "GOOGLE_CLIENT_ID",
        "test-client-rendering.apps.googleusercontent.com")
    yield


@pytest.fixture(autouse=True)
def _reset_render_cache():
    """Wipe the module-level _RENDER_CACHE between tests so feature-flag
    flips in one test don't bleed into the next."""
    from litetube import main as main_mod
    main_mod._RENDER_CACHE.clear()
    yield
    main_mod._RENDER_CACHE.clear()


# ---------------------------------------------------------------------------
# When the feature is OFF (default)
# ---------------------------------------------------------------------------

class TestSigninBlockAbsentWhenDisabled:
    """Conftest's default is GOOGLE_AUTH_ENABLED=0. Verify the placeholder
    is replaced with empty string (no leftover literal leaks through)."""

    @pytest.mark.asyncio
    async def test_activate_html_omits_google_block(self, test_client):
        r = await test_client.get("/activate")
        assert r.status_code == 200
        body = r.text
        assert "g_id_onload" not in body
        assert "g_id_signin" not in body
        assert "accounts.google.com/gsi/client" not in body
        assert "handleGoogleCredential" not in body
        # The literal placeholder must NOT appear in served HTML.
        assert "{{GOOGLE_SIGNIN_BLOCK}}" not in body

    @pytest.mark.asyncio
    async def test_client_html_omits_google_block(self, test_client):
        r = await test_client.get("/")
        assert r.status_code == 200
        body = r.text
        assert "g_id_onload" not in body
        assert "g_id_signin" not in body
        assert "accounts.google.com/gsi/client" not in body
        # The literal placeholder must NOT appear in served HTML.
        assert "{{GOOGLE_SIGNIN_BLOCK}}" not in body


# ---------------------------------------------------------------------------
# When the feature is ON
# ---------------------------------------------------------------------------

class TestSigninBlockPresentWhenEnabled:
    """With GOOGLE_AUTH_ENABLED=1 and a non-empty GOOGLE_CLIENT_ID, the
    rendered HTML must include the GIS script + onload div + button,
    with the configured client id baked into data-client_id."""

    @pytest.mark.asyncio
    async def test_activate_html_includes_google_block_with_client_id(
            self, test_client, google_enabled):
        r = await test_client.get("/activate")
        assert r.status_code == 200
        body = r.text
        # GIS plumbing present.
        assert 'src="https://accounts.google.com/gsi/client"' in body
        assert 'id="g_id_onload"' in body
        assert 'class="g_id_signin"' in body
        # Client id rendered correctly.
        assert (
            'data-client_id="test-client-rendering.apps.googleusercontent.com"'
            in body)
        # Callback name matches the script tag below.
        assert 'data-callback="handleGoogleCredential"' in body
        # Theme + size match the spec'd values.
        assert 'data-theme="dark"' in body
        assert 'data-size="large"' in body
        # JS handler references both the auth endpoint AND the device-claim chain.
        assert '/api/auth/google' in body
        assert '/api/devices/claim/complete' in body
        assert 'handleGoogleCredential(response)' in body
        assert "{{GOOGLE_SIGNIN_BLOCK}}" not in body

    @pytest.mark.asyncio
    async def test_client_html_includes_google_block(
            self, test_client, google_enabled):
        r = await test_client.get("/")
        assert r.status_code == 200
        body = r.text
        assert 'src="https://accounts.google.com/gsi/client"' in body
        assert 'id="g_id_onload"' in body
        assert (
            'data-client_id="test-client-rendering.apps.googleusercontent.com"'
            in body)
        assert 'data-callback="handleGoogleCredential"' in body
        assert '/api/auth/google' in body
        assert "{{GOOGLE_SIGNIN_BLOCK}}" not in body

    @pytest.mark.asyncio
    async def test_block_includes_oauth_divider_css_class(self, test_client, google_enabled):
        """The wrapper class `.oauth-divider` is added once per page; the
        CSS rule itself lives in the static file. Here we only check that
        the rendered HTML has the divider markup (visual styling is static)."""
        r = await test_client.get("/activate")
        body = r.text
        assert '<div class="oauth-divider' in body


# ---------------------------------------------------------------------------
# Cache invalidation on flag flip
# ---------------------------------------------------------------------------

class TestSigninBlockCacheInvalidation:
    """If the operator flips GOOGLE_AUTH_ENABLED without process restart,
    the next request must render the new state. Cache key includes flag
    state and client id to make this work."""

    @pytest.mark.asyncio
    async def test_flag_flip_re_renders_without_restart(
            self, test_client, monkeypatch):
        # 1. Default: disabled.
        r1 = await test_client.get("/activate")
        assert "g_id_onload" not in r1.text

        # 2. Flip the flag on without a process restart.
        monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "1")
        monkeypatch.setenv(
            "GOOGLE_CLIENT_ID", "flip-test.apps.googleusercontent.com")
        r2 = await test_client.get("/activate")
        assert "g_id_onload" in r2.text
        assert (
            'data-client_id="flip-test.apps.googleusercontent.com"' in r2.text)

        # 3. Flip back off.
        monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "0")
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        r3 = await test_client.get("/activate")
        assert "g_id_onload" not in r3.text


# ---------------------------------------------------------------------------
# Sanity: existing flows still work
# ---------------------------------------------------------------------------

class TestExistingFlowsUnchanged:
    """The placeholder substitution must not regress existing flows."""

    @pytest.mark.asyncio
    async def test_activate_still_renders_code_input(self, test_client):
        r = await test_client.get("/activate")
        assert r.status_code == 200
        assert 'id="code-input"' in r.text
        assert 'class="code-digits"' in r.text

    @pytest.mark.asyncio
    async def test_client_still_renders_login_form(self, test_client):
        r = await test_client.get("/")
        assert r.status_code == 200
        assert 'id="form-login"' in r.text
        assert 'id="form-signup"' in r.text
        assert 'id="login-email"' in r.text

    @pytest.mark.asyncio
    async def test_health_endpoint_still_responds(self, test_client):
        r = await test_client.get("/health")
        assert r.status_code == 200
        # Version is whatever __version__ says at test time — just check it's present.
        assert "version" in r.json()
        assert r.json()["status"] == "ok"
