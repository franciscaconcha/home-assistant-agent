.PHONY: init build setup pair up down logs status doctor check yoto-login yoto-configure ocado-login ocado-configure ocado-check ocado-order-check ocado-enable-editing

init:
	sh scripts/init.sh

build:
	docker compose build --pull

setup:
	docker compose run --rm --no-deps hermes setup

pair:
	docker compose run --rm --no-deps hermes whatsapp

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs --follow --tail=100 hermes

status:
	docker compose ps

doctor:
	docker compose exec hermes hermes doctor

check:
	uv run --locked python -m unittest discover -s tests
	uv run --locked python scripts/check_mcp.py

# Temporary login container: callback exposed on droplet loopback only, via SSH tunnel.
yoto-login:
	docker compose run --rm --no-deps -p 127.0.0.1:8787:8787 hermes /opt/household-tools/.venv/bin/household-yoto-login --bind 0.0.0.0

yoto-configure:
	docker compose run --rm --no-deps hermes /opt/household-tools/.venv/bin/household-yoto-configure

# Run on the Mac: manual browser login, with the session stored in ignored data/.
ocado-login:
	uv run --locked --extra ocado-login python -m playwright install chromium
	HERMES_HOME="$(CURDIR)/data" uv run --locked --extra ocado-login household-ocado-login

# Run on the droplet after copying data/credentials/ocado.json from the Mac.
ocado-configure:
	docker compose run --rm --no-deps hermes /opt/household-tools/.venv/bin/household-ocado-configure

ocado-check:
	docker compose run --rm --no-deps hermes /opt/household-tools/.venv/bin/household-ocado-check

ocado-order-check:
	docker compose run --rm --no-deps hermes /opt/household-tools/.venv/bin/household-ocado-order-check

ocado-enable-editing:
	docker compose run --rm --no-deps hermes /opt/household-tools/.venv/bin/household-ocado-enable-editing
