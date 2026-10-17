#!/usr/bin/env bash
# Instala Cati para el usuario actual. Opciones: ./install.sh --help
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
# GTK's Python bindings come from the distribution: prefer a Python that already has them
# over pyenv/conda/uv interpreters earlier in PATH.
python=""
for candidate in /usr/bin/python3 /usr/local/bin/python3 $(type -ap python3 2>/dev/null || true); do
  [ -x "$candidate" ] || continue
  "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || continue
  [ -z "$python" ] && python=$candidate
  if "$candidate" -c 'import gi' 2>/dev/null; then python=$candidate; break; fi
done
if [ -z "$python" ]; then
  echo "Cati necesita Python 3.11 o posterior (paquete python3)." >&2
  exit 1
fi
exec "$python" scripts/setup.py "$@"
