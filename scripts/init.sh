#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p data backups
if [ ! -e .env ]; then
    {
        sed -n '/^HERMES_IMAGE=/p' .env.example
        printf 'HERMES_UID=%s\n' "$(id -u)"
        printf 'HERMES_GID=%s\n' "$(id -g)"
    } > .env
fi
for file in config.yaml SOUL.md; do
    if [ ! -e "data/$file" ]; then
        cp "config/$file" "data/$file"
    fi
done
printf '%s\n' 'Initialized local settings and runtime templates; existing files preserved.'
