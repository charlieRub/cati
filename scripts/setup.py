#!/usr/bin/env python3
"""Instalador de Cati para el usuario actual (no requiere root salvo para --deps)."""
import argparse
import hashlib
import io
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LLAMA_TAG = 'b11461'
LLAMA_COMMIT = '4d756bc72bf00a4aacf410ae15a2d315f3db400d'
MODEL_REPO = 'https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve'
MODELS = [('embeddinggemma-2-Q8_0.gguf', '2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135'),
          ('mmproj-embeddinggemma-2-Q8_0.gguf', 'c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f')]
NERD_SYMBOLS = 'https://github.com/ryanoasis/nerd-fonts/releases/download/v3.4.0/NerdFontsSymbolsOnly.tar.xz'
SYSTEMD = Path(os.getenv('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd/user'


def step(text):
    print(f'\n\033[1;36m›\033[0m \033[1m{text}\033[0m', flush=True)


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def dependencies(install):
    from cati import doctor
    step('Comprobando dependencias del sistema')
    missing = doctor.report()
    if missing and install:
        command = doctor.install_command(missing + [n for n, ok in doctor.check().items() if not ok and n in {'gstreamer', 'browser'}])
        if not command: raise SystemExit('No reconozco tu distribución; instala los paquetes indicados arriba.')
        print(f'\nEjecutando: {command}')
        run(command.split())
        missing = [n for n, ok in doctor.check().items() if not ok and doctor.REQUIREMENTS[n][1]]
    if missing:
        raise SystemExit('\nFaltan dependencias obligatorias. Instálalas (o usa ./install.sh --deps) y vuelve a ejecutar el instalador.')


def environment():
    step('Entorno de Python')
    venv = ROOT / '.venv'
    if not (venv / 'bin/python').exists():
        # System site packages: PyGObject and GTK come from the distribution.
        run([sys.executable, '-m', 'venv', '--system-site-packages', str(venv)])
    python = str(venv / 'bin/python')
    wanted = [('numpy', 'numpy>=2,<3'), ('PIL', 'Pillow>=11,<14'), ('inotify_simple', 'inotify-simple>=2,<3'),
              ('rapidocr', 'rapidocr>=3.10,<4'), ('onnxruntime', 'onnxruntime>=1.20,<2')]
    missing = [spec for module, spec in wanted if subprocess.run([python, '-c', f'import {module}'], capture_output=True).returncode]
    if missing: run([python, '-m', 'pip', 'install', '--quiet', *missing])
    # PP-OCR models (≈30 MB) are fetched now rather than during the first indexing job.
    if subprocess.run([python, '-c', 'from cati.readers import rapid; import sys; sys.exit(rapid() is None)'],
                      cwd=ROOT, capture_output=True).returncode:
        print('  · OCR PP-OCRv6 no disponible en esta arquitectura; se usará Tesseract.')
    return python


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def models():
    from cati.config import DATA, MODEL_REVISION
    step('Modelo local EmbeddingGemma 2 (≈865 MB, se descarga una vez)')
    folder = DATA / 'models'
    folder.mkdir(parents=True, exist_ok=True)
    for name, checksum in MODELS:
        path = folder / name
        if path.exists() and digest(path) == checksum:
            print(f'  ✓ {name}'); continue
        partial = path.with_suffix('.part')
        with urllib.request.urlopen(f'{MODEL_REPO}/{MODEL_REVISION}/{name}', timeout=120) as response, partial.open('wb') as out:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            while block := response.read(8 * 1024 * 1024):
                out.write(block)
                done += len(block)
                if total: print(f'\r  {name}: {done * 100 // total}%', end='', flush=True)
        print()
        if digest(partial) != checksum:
            partial.unlink()
            raise SystemExit(f'La suma de verificación de {name} no coincide; vuelve a intentarlo.')
        partial.replace(path)


def built(backend):
    binary = ROOT / f'build/{backend}/bin/llama-server'
    if not binary.exists(): return False
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True)
    return LLAMA_COMMIT[:7] in (version.stdout + version.stderr)


def engine(gpu):
    from cati import doctor
    from cati.config import load, save
    step('Motor de embeddings (llama.cpp)')
    source = ROOT / 'build/llama.cpp'
    backends = ['runtime']
    if gpu == 'vulkan' or (gpu == 'auto' and (doctor.vulkan_ok() or built('vulkan'))): backends.append('vulkan')
    elif gpu == 'auto': print('  · Sin Vulkan: se usará la CPU (instala los extras de Vulkan para acelerar con la GPU).')
    if not all(built(b) for b in backends):
        if not source.exists():
            run(['git', 'clone', '--quiet', '--depth', '1', '--branch', LLAMA_TAG, 'https://github.com/ggml-org/llama.cpp', str(source)])
        if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != LLAMA_COMMIT:
            raise SystemExit('La copia de llama.cpp no coincide con la versión probada.')
        patch = ROOT / 'patches/llama-no-logits.patch'
        if subprocess.run(['git', '-C', str(source), 'apply', '--check', str(patch)], capture_output=True).returncode == 0:
            run(['git', '-C', str(source), 'apply', str(patch)])
    jobs = str(max(1, min(os.cpu_count() or 2, 8)))
    for backend in backends:
        if built(backend):
            print(f'  ✓ {backend}'); continue
        flags = ['-DGGML_VULKAN=ON'] if backend == 'vulkan' else ['-DGGML_NATIVE=ON']
        build = ROOT / f'build/{backend}'
        print(f'  Compilando {backend} (unos minutos)…', flush=True)
        run(['cmake', '-S', str(source), '-B', str(build), '-DCMAKE_BUILD_TYPE=Release', '-DLLAMA_BUILD_SERVER=ON',
             '-DLLAMA_BUILD_TOOLS=ON', '-DLLAMA_BUILD_TESTS=OFF', '-DLLAMA_BUILD_EXAMPLES=OFF', '-DLLAMA_OPENSSL=OFF',
             '-DLLAMA_CURL=OFF', *flags], stdout=subprocess.DEVNULL)
        run(['cmake', '--build', str(build), '--target', 'llama-server', '-j', jobs], stdout=subprocess.DEVNULL)
    config = load()
    config['gpu_layers'] = 999 if 'vulkan' in backends and built('vulkan') else 0
    save(config)


def fonts():
    """Los iconos de la interfaz usan glifos Nerd Font; si no hay ninguna, añade la de solo símbolos."""
    listing = subprocess.run(['fc-list', ':', 'family'], capture_output=True, text=True).stdout if shutil.which('fc-list') else ''
    if 'Nerd Font' in listing: return
    step('Iconos de la interfaz (Symbols Nerd Font)')
    target = Path(os.getenv('XDG_DATA_HOME', Path.home() / '.local/share')) / 'fonts/cati'
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(NERD_SYMBOLS, timeout=60) as response:
        archive = tarfile.open(fileobj=io.BytesIO(response.read()), mode='r:xz')
    for member in archive.getmembers():
        if member.isfile() and member.name.endswith('.ttf') and '/' not in member.name.strip('./'):
            (target / member.name).write_bytes(archive.extractfile(member).read())
    if shutil.which('fc-cache'): run(['fc-cache', '-f', str(target)], stdout=subprocess.DEVNULL)


def has_systemd():
    if not shutil.which('systemctl'): return False
    state = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True).stdout.strip()
    return state not in {'', 'offline'}


def services():
    from cati.desktop import LAUNCHER, AUTOSTART
    step('Servicios en segundo plano')
    if not has_systemd():
        watch = AUTOSTART.with_name('dev.cati.Cati-watch.desktop')
        watch.parent.mkdir(parents=True, exist_ok=True)
        watch.write_text(f'[Desktop Entry]\nType=Application\nName=Cati (vigilancia)\nExec={LAUNCHER} watch\nNoDisplay=true\n')
        from cati.service import start
        start('watch')
        print('  ✓ Sin systemd: la vigilancia arranca con la sesión (autoarranque XDG).')
        return
    executable = str(LAUNCHER).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    units = {
        'cati-engine.service': f'''[Unit]
Description=Cati · modelo local de embeddings
[Service]
Type=simple
ExecStart="{executable}" engine
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
''',
        'cati-index.service': f'''[Unit]
Description=Cati · vigilancia e indexación de archivos
Wants=cati-engine.service
After=cati-engine.service
[Service]
Type=simple
ExecStart="{executable}" watch
Restart=on-failure
RestartSec=5
Nice=10
IOSchedulingClass=idle
UMask=0077
NoNewPrivileges=true
TimeoutStopSec=60
[Install]
WantedBy=default.target
'''}
    SYSTEMD.mkdir(parents=True, exist_ok=True)
    for name, content in units.items(): (SYSTEMD / name).write_text(content)
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', 'cati-index.service'], capture_output=True)
    run(['systemctl', '--user', 'restart', 'cati-engine.service', 'cati-index.service'], capture_output=True)
    print('  ✓ cati-index.service activo; el motor se carga cuando hace falta y se libera tras 15 min sin uso.')


def install(args):
    if sys.version_info < (3, 11): raise SystemExit('Cati necesita Python 3.11 o posterior.')
    dependencies(args.deps)
    environment()
    from cati.config import DATA, prepare
    prepare()
    key = DATA / 'engine.key'
    if not key.exists():
        key.write_text(secrets.token_urlsafe(32) + '\n'); key.chmod(0o600)
    models()
    engine(args.gpu)
    fonts()
    step('Integración con el escritorio')
    from cati.desktop import integrate
    for note in integrate(shortcut=not args.no_shortcut, binding=args.shortcut): print(f'  · {note}')
    services()
    print('\n\033[1;32m✓ Cati está instalado.\033[0m  Ábrelo con tu atajo, desde el lanzador o con «cati».')
    print('  El primer análisis de tus archivos sigue en segundo plano; los nombres se pueden buscar desde ya.')


def uninstall(args):
    from cati.desktop import AUTOSTART, remove
    step('Desinstalando Cati')
    if shutil.which('systemctl'):
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'cati-index.service', 'cati-engine.service'], capture_output=True)
        for name in ('cati-engine.service', 'cati-index.service'): (SYSTEMD / name).unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    from cati.service import stop
    for name in ('watch', 'engine'): stop(name)
    subprocess.run(['pkill', '-f', 'python -m cati gui'], capture_output=True)
    AUTOSTART.with_name('dev.cati.Cati-watch.desktop').unlink(missing_ok=True)
    remove()
    if args.purge:
        from cati.config import CONFIG, DATA
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(CONFIG.parent, ignore_errors=True)
        print('  ✓ Índice, modelos y ajustes borrados.')
    else:
        print('  · El índice y los modelos se conservan; usa --purge para borrarlos.')
    print('\n✓ Cati se ha desinstalado. Puedes borrar esta carpeta.')


def main():
    parser = argparse.ArgumentParser(prog='install.sh', description='Instala Cati para el usuario actual.')
    parser.add_argument('--deps', action='store_true', help='instala con sudo los paquetes del sistema que falten')
    parser.add_argument('--gpu', choices=['auto', 'vulkan', 'cpu'], default='auto', help='aceleración del motor (por defecto: auto)')
    parser.add_argument('--shortcut', help='atajo, p. ej. "SUPER ALT, U" (Hyprland) o "<Super><Alt>u" (GNOME)')
    parser.add_argument('--no-shortcut', action='store_true', help='no configurar atajo de teclado')
    parser.add_argument('--uninstall', action='store_true', help='retira Cati del sistema')
    parser.add_argument('--purge', action='store_true', help='con --uninstall, borra también índice, modelos y ajustes')
    args = parser.parse_args()
    if sys.platform != 'linux': raise SystemExit('Cati funciona en Linux.')
    uninstall(args) if args.uninstall else install(args)


if __name__ == '__main__':
    main()
