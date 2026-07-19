#!/bin/sh
# Git credential helper: serves the per-project GitHub token mounted by cld.
# Reads the file on every call, so a re-minted token is picked up live.
[ "$1" = "get" ] || exit 0
TOKEN_FILE="$HOME/.git-creds/token"
[ -f "$TOKEN_FILE" ] || exit 0
echo "username=x-access-token"
echo "password=$(cat "$TOKEN_FILE")"
