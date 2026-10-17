"""Arranque de los servicios de fondo con systemd de usuario o, si no existe, como procesos propios."""
import os
import shutil
import signal
import subprocess
import sys
import time
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


def ours(pid, name):
    """¿Es `pid` de verdad «cati <name>»? Un pidfile viejo puede apuntar a un PID ya reutilizado."""
    try: args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError: return False
    return b'cati' in args and name.encode() in args


def running(name):
    try:
        pid = int(pidfile(name).read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid if ours(pid, name) else None


def claim(name):
    """El propio proceso anota su PID fuera de systemd (autoarranque XDG) y lo retira al salir."""
    if os.getenv('INVOCATION_ID'): return lambda: None
    pidfile(name).write_text(str(os.getpid()))
    def release():
        try:
            if int(pidfile(name).read_text()) == os.getpid(): pidfile(name).unlink()
        except (OSError, ValueError): pass
    return release


def systemctl(*args, timeout=120):
    result = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'systemctl {" ".join(args)}: {result.stderr.strip() or "falló"} (consulta journalctl --user -u {args[-1]})')


def start(name):
    if systemd():
        systemctl('start', UNITS[name])
        return
    if running(name): return
    project = Path(__file__).resolve().parent.parent
    python = project / '.venv/bin/python'
    with (DATA / f'{name}.log').open('ab') as log:  # the child keeps its own copy of the descriptor
        process = subprocess.Popen([str(python if python.exists() else sys.executable), '-m', 'cati', name], stdout=log, stderr=log,
                                   stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(project))
    pidfile(name).write_text(str(process.pid))


def stop(name, wait=20):
    if systemd():
        subprocess.run(['systemctl', '--user', 'stop', UNITS[name]], capture_output=True, timeout=60)
        return
    pid = running(name)
    if pid:
        os.kill(pid, signal.SIGTERM)
        # Wait for it to let go of the index lock, or a restart would find it still held.
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and Path(f'/proc/{pid}').exists() and ours(pid, name):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]: break  # our own child: reap it instead of leaving a zombie
            except ChildProcessError: pass
            time.sleep(.1)
        else:
            if Path(f'/proc/{pid}').exists() and ours(pid, name): os.kill(pid, signal.SIGKILL)
    pidfile(name).unlink(missing_ok=True)


def restart(name):
    """Aplica ajustes nuevos: reinicia el servicio solo si ya estaba en marcha."""
    if systemd():
        systemctl('try-restart', UNITS[name])
    elif running(name):
        stop(name); start(name)


def start_watch(): start('watch')
def stop_watch(): stop('watch')


def restart_all():
    for name in ('watch', 'engine'): stop(name)
    for name in ('engine', 'watch'): start(name)
