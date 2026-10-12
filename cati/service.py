"""Arranque de los servicios de fondo con systemd de usuario o, si no existe, como procesos propios."""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import DATA

UNITS = {'engine': 'cati-engine.service', 'watch': 'cati-index.service'}


def systemd():
    """True si hay sesión de systemd de usuario y las unidades de Cati están instaladas."""
    if not shutil.which('systemctl'): return False
    unit = Path.home() / '.config/systemd/user' / UNITS['watch']
    try:
        state = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return unit.exists() and state.stdout.strip() not in {'', 'offline'}


def pidfile(name):
    return DATA / f'{name}.pid'


def running(name):
    try:
        pid = int(pidfile(name).read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(name):
    if systemd():
        subprocess.run(['systemctl', '--user', 'start', UNITS[name]], check=True, capture_output=True, timeout=120)
        return
    if running(name): return
    log = (DATA / f'{name}.log').open('ab')
    project = Path(__file__).resolve().parent.parent
    python = project / '.venv/bin/python'
    process = subprocess.Popen([str(python if python.exists() else sys.executable), '-m', 'cati', name], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               start_new_session=True, cwd=str(project))
    pidfile(name).write_text(str(process.pid))


def stop(name):
    if systemd():
        subprocess.run(['systemctl', '--user', 'stop', UNITS[name]], capture_output=True, timeout=60)
        return
    pid = running(name)
    if pid: os.kill(pid, signal.SIGTERM)
    pidfile(name).unlink(missing_ok=True)


def start_watch(): start('watch')
def stop_watch(): stop('watch')


def restart_all():
    for name in ('watch', 'engine'): stop(name)
    for name in ('engine', 'watch'): start(name)
