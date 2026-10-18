"""Inloggen via een headless browser.

SSH stuurt je na het invullen van je e-mailadres door naar sso.room.nl (ROOM-account,
Zitadel). Daar vul je je wachtwoord in, waarna je terugkomt op sshxl.nl/liap-login
en de sessiecookies worden gezet. Die cookies slaan we op voor de httpx-client.

De selectors voor sso.room.nl zijn de standaard Zitadel-velden en nog niet live
getest. Mislukt het, dan staat er een screenshot in data/login-fout.png.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

EMAIL_VELD = "input[type=email], input[name*=mail i], input#loginName"
WACHTWOORD_VELD = "input[type=password], input#password"
VERSTUUR = "button[type=submit], button#submit-button"


def log_in(email: str, wachtwoord: str, cookie_file: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="nl-NL")
        try:
            page.goto("https://www.sshxl.nl/nl/inloggen", wait_until="networkidle")
            _weiger_cookiebanner(page)
            page.fill(EMAIL_VELD, email)
            page.click(VERSTUUR)

            page.wait_for_url("**sso.room.nl/**", timeout=30_000)
            # Zitadel vraagt soms eerst opnieuw om de loginnaam.
            if page.locator("input#loginName").count():
                page.fill("input#loginName", email)
                page.click(VERSTUUR)
            page.wait_for_selector(WACHTWOORD_VELD, timeout=30_000)
            page.fill(WACHTWOORD_VELD, wachtwoord)
            page.click(VERSTUUR)

            page.wait_for_url("https://www.sshxl.nl/**", timeout=60_000)
            page.wait_for_load_state("networkidle")
            cookies = [c for c in page.context.cookies() if "sshxl.nl" in c["domain"]]
            if not cookies:
                raise RuntimeError("Geen sshxl-cookies na inloggen")
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            cookie_file.write_text(json.dumps(cookies))
            cookie_file.chmod(0o600)
            log.info("Ingelogd, %d cookies opgeslagen", len(cookies))
        except Exception:
            page.screenshot(path=str(cookie_file.parent / "login-fout.png"))
            raise
        finally:
            browser.close()


def _weiger_cookiebanner(page) -> None:
    knop = page.locator("#CybotCookiebotDialogBodyButtonDecline")
    if knop.count():
        knop.click()
