.PHONY: init build setup pair up down logs status doctor check

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
	uv run --locked python scripts/check_mcp.py
