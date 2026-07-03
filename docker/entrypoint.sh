#!/bin/sh
set -e

case "$1" in
  api)
    exec verdict serve --host 0.0.0.0 --port 8400
    ;;
  worker)
    exec verdict worker
    ;;
  *)
    exec "$@"
    ;;
esac
