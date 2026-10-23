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


EDIT_GUIDANCE = """\n\n<!-- ocado-order-editing -->
For Ocado order edits, use ocado_plan_order_changes first. Show the delivery and exact
before/after quantities and wait for explicit user confirmation before calling
ocado_apply_order_changes with confirmed=true. A draft is not a saved order: tell the
user to review and save/check out on Ocado's website. Never retry an uncertain or partial
edit automatically. Never merge or discard another trolley. Adding new products is
not supported by this tool.\n"""


def configure(enable_order_edits: bool = False) -> None:
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
        added = ["household_status", "ocado_basket", "ocado_next_order", "ocado_plan_order_changes"]
        if enable_order_edits:
            added.append("ocado_apply_order_changes")
            entry["env"]["OCADO_ORDER_EDITING_ENABLED"] = "true"
        if "include" in tools:
            tools["include"] = list(dict.fromkeys(tools["include"] + added))
        elif not tools:
            tools["include"] = added
        entry.setdefault("sampling", {"enabled": False})
        backup = path.with_name(f"config.before-ocado-{time.time_ns()}.yaml")
        backup.touch(mode=0o600, exist_ok=False)
        backup.write_text(original)
        yoto.write_private(path, config)
        if enable_order_edits:
            soul = yoto.home() / "SOUL.md"
            previous = soul.read_text() if soul.exists() else ""
            if "<!-- ocado-order-editing -->" not in previous:
                if soul.exists():
                    backup_soul = soul.with_name(f"SOUL.before-ocado-{time.time_ns()}.md")
                    backup_soul.touch(mode=0o600, exist_ok=False)
                    backup_soul.write_text(previous)
                soul.touch(mode=0o600, exist_ok=True)
                soul.write_text(previous + EDIT_GUIDANCE)
        print("Ocado read and preview tools enabled; existing Yoto, model and WhatsApp settings preserved.")
        if enable_order_edits:
            print("Confirmed draft order edits enabled. Final review and saving/checking out remain on Ocado's website.")
    except (OSError, ValueError, TypeError):
        raise SystemExit("Ocado configuration failed. Check the copied session and config.yaml; no credentials were printed.") from None


def enable_editing() -> None:
    configure(enable_order_edits=True)


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


def order_check() -> None:
    try:
        result = ocado.next_order()
        order = result["order"]
        if order is None:
            print("No upcoming Reserved order was returned by Ocado.")
            return
        print("Next Reserved order read succeeded.")
        print("Delivery:", order["delivery"]["start"], "to", order["delivery"]["end"])
        print("Editable:", order["is_editable"])
        print("Guaranteed editable by:", order["guaranteed_editable_by"])
        print("Order item lines:", len(order["items"]) if order["items"] is not None else "unavailable")
        if "warning" in order:
            print("Warning:", order["warning"])
    except ocado.OcadoError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
