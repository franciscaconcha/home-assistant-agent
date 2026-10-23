"""Operator-only browser login, session validation, and MCP configuration."""

import sys
import time
from urllib.parse import urlparse

import yaml

from household_tools.integrations import ocado, yoto


def login() -> None:
    # Browser dependency is optional; the droplet only needs httpx for reads.
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError:
        raise SystemExit("Run make ocado-login on your Mac to install the browser login extra.") from None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                names = {}

                def capture_products(response):
                    url = urlparse(response.url)
                    if url.hostname != "www.ocado.com" or url.path != ocado.PRODUCTS or response.status != 200:
                        return
                    try:
                        value = response.json()
                        if isinstance(value, dict):
                            names.update(ocado.product_names(value.get("products")))
                    except (ValueError, Error):
                        pass  # No raw response contents or browser session data are logged.

                context.on("response", capture_products)
                page = context.new_page()
                page.goto("https://www.ocado.com/login", wait_until="domcontentloaded", timeout=60000)
                print("Sign in directly in the browser window, then open your current trolley.")
                print("Refresh the trolley page once so its product names are loaded for this sync.")
                print("Your password is not collected or saved by this helper.")
                input("Once your trolley is visible, press Enter here to save its session: ")
                # Flush browser callbacks and collect names embedded in the currently visible page.
                # No additional product API calls are issued by this helper.
                names.update(ocado.page_product_names(page.content()))
                session = ocado.validated_session({"cookies": context.cookies(),
                    "user_agent": page.evaluate("navigator.userAgent"), "captured_at": time.time(),
                    "product_names": names})
                yoto.write_private(ocado.session_path(), session)
                print(f"Session saved privately to {ocado.session_path()}")
                print(f"Saved names for {len(session['product_names'])} products viewed in the browser.")
                if not session["product_names"]:
                    print("No names were captured. The trolley tool will report unknown names until a browser sync captures them.")
                print("Copy only this file to the droplet. This is not yet a live droplet connection test.")
            finally:
                browser.close()
    except (ocado.OcadoError, Error, OSError, EOFError):
        raise SystemExit("Ocado login/session capture did not finish. Check the browser, then retry make ocado-login. No password or cookie contents were printed.") from None


def configure() -> None:
    try:
        ocado.load_session()  # Validates structure, never prints the session.
        path = yoto.home() / "config.yaml"
        original = path.read_text()
        config = yaml.safe_load(original) or {}
        entry = config.setdefault("mcp_servers", {}).setdefault("household", {})
        entry.setdefault("command", "/opt/household-tools/.venv/bin/household-tools")
        entry.setdefault("env", {})["HERMES_HOME"] = str(yoto.home())
        entry["timeout"] = max(entry.get("timeout", 90), 120)
        tools = entry.setdefault("tools", {})
        if "include" in tools:
            tools["include"] = list(dict.fromkeys(tools["include"] + ["household_status", "ocado_basket"]))
        elif not tools:
            tools["include"] = ["household_status", "ocado_basket"]
        entry.setdefault("sampling", {"enabled": False})
        backup = path.with_name(f"config.before-ocado-{time.time_ns()}.yaml")
        backup.touch(mode=0o600, exist_ok=False)
        backup.write_text(original)
        yoto.write_private(path, config)
        print("Ocado trolley tool enabled; existing Yoto, model and WhatsApp settings preserved.")
    except (OSError, ValueError, TypeError):
        raise SystemExit("Ocado configuration failed. Check the copied session and config.yaml; no credentials were printed.") from None


def check() -> None:
    try:
        result = ocado.basket()
        # Useful for checking the connection without dumping private shopping data.
        print(f"Ocado trolley read succeeded: {len(result['items'])} item lines.")
        print(f"Named item lines: {sum(item['name'] is not None for item in result['items'])} (from Mac browser snapshot).")
        if "warning" in result:
            print("Warning: " + result["warning"])
    except ocado.OcadoError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
