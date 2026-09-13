#!/bin/sh
set -eu

mkdir -p /app/uploads
chown -R appuser:appuser /app/uploads

exec su-exec appuser "$@"
