"""Integración con el escritorio: lanzador, icono, arranque residente y atajo de teclado.

Hyprland (incluido Omarchy) recibe reglas de ventana, atajo y arranque; GNOME, un
atajo personalizado; el resto, el autoarranque XDG y la entrada de aplicación.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import APP_ID, PROJECT

HOME = Path.home()
DATA_HOME = Path(os.getenv('XDG_DATA_HOME', HOME / '.local/share'))
CONFIG_HOME = Path(os.getenv('XDG_CONFIG_HOME', HOME / '.config'))
LAUNCHER = HOME / '.local/bin/cati'
DESKTOP = DATA_HOME / 'applications' / f'{APP_ID}.desktop'
ICON = DATA_HOME / 'icons/hicolor/scalable/apps' / f'{APP_ID}.svg'
AUTOSTART = CONFIG_HOME / 'autostart' / f'{APP_ID}.desktop'
HYPR = CONFIG_HOME / 'hypr'
HYPR_FILE = HYPR / 'cati.conf'
SOURCE_LINE = 'source = ~/.config/hypr/cati.conf'
GNOME_PATH = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/cati/'
GNOME_VOICE = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/cati-voice/'


def environment():
    desktop = os.getenv('XDG_CURRENT_DESKTOP', '').lower()
    if os.getenv('HYPRLAND_INSTANCE_SIGNATURE') or 'hyprland' in desktop: return 'hyprland'
    if 'gnome' in desktop: return 'gnome'
    if 'kde' in desktop: return 'kde'
    return desktop or 'other'


def omarchy():
    return (CONFIG_HOME / 'omarchy').is_dir()


def install_entry():
    """Lanzador en ~/.local/bin, icono y entrada de aplicación (Walker, GNOME, KDE…)."""
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    source = PROJECT / 'scripts/cati'
    if LAUNCHER.is_symlink() or not LAUNCHER.exists():
        LAUNCHER.unlink(missing_ok=True)
        LAUNCHER.symlink_to(source)
    elif LAUNCHER.resolve() != source.resolve():
        raise RuntimeError(f'Ya existe otro programa en {LAUNCHER}')
    ICON.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROJECT / 'assets/cati.svg', ICON)
    DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP.write_text(f'''[Desktop Entry]
Type=Application
Name=Cati
GenericName=Buscador de archivos
Comment=Encuentra cualquier archivo describiéndolo con tus palabras
Exec={LAUNCHER} gui
Icon={APP_ID}
Terminal=false
Categories=Utility;FileTools;
Keywords=search;find;files;buscar;archivos;documentos;fotos;
StartupNotify=false
StartupWMClass={APP_ID}
X-KDE-Shortcuts=Meta+Alt+U
Actions=voice;

[Desktop Action voice]
Name=Buscar por voz
Exec={LAUNCHER} voice
''')
    for tool in (['update-desktop-database', str(DESKTOP.parent)], ['gtk-update-icon-cache', '-q', '-t', str(DATA_HOME / 'icons/hicolor')]):
        if shutil.which(tool[0]): subprocess.run(tool, capture_output=True)


def install_autostart():
    """Cati queda residente al iniciar sesión: abrirlo después es instantáneo."""
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(f'''[Desktop Entry]
Type=Application
Name=Cati (en segundo plano)
Exec={LAUNCHER} gui --background
Icon={APP_ID}
NoDisplay=true
X-GNOME-Autostart-enabled=true
''')


def hyprctl(*args):
    return subprocess.run(['hyprctl', *args], capture_output=True, text=True, timeout=10)


def hyprland_version():
    try:
        tag = json.loads(hyprctl('version', '-j').stdout).get('tag', '')
        return tuple(int(x) for x in re.findall(r'\d+', tag)[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)


def install_hyprland(binding='SUPER ALT, U'):
    if not (HYPR / 'hyprland.conf').exists(): return 'No se encontró ~/.config/hypr/hyprland.conf'
    mods, key = [part.strip() for part in binding.split(',', 1)]
    try:
        binds = json.loads(hyprctl('binds', '-j').stdout or '[]')
        wanted = set(mods.upper().split())
        names = {'SUPER': 64, 'ALT': 8, 'CTRL': 4, 'CONTROL': 4, 'SHIFT': 1}
        mask = sum(names.get(m, 0) for m in wanted)
        taken = [b for b in binds if b.get('modmask') == mask and b.get('key', '').lower() in {key.lower(), 'v'} and 'cati' not in b.get('arg', '')]
        if taken: return f'{binding} ya está ocupado por «{taken[0].get("description") or taken[0].get("arg")}»; elige otro con --shortcut'
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    pattern = APP_ID.replace('.', r'\.')
    if hyprland_version() >= (0, 53):
        rules = '\n'.join(f'windowrule = {rule}, match:class ^{pattern}$' for rule in ('float on', 'center on', 'size 1240 780', 'opacity 1.0 override'))
    else:
        rules = '\n'.join(f'windowrulev2 = {rule}, class:^({pattern})$' for rule in ('float', 'center', 'size 1240 780', 'opacity 1.0 override 1.0 override'))
    previous = HYPR_FILE.read_text() if HYPR_FILE.exists() else None
    HYPR_FILE.write_text(f'''# Cati: ventana flotante centrada, atajo y arranque residente
{rules}
bindd = {mods}, {key}, Cati: buscar archivos, exec, {LAUNCHER} gui
bindd = {mods}, V, Cati: buscar por voz, exec, {LAUNCHER} voice
exec-once = {LAUNCHER} gui --background
''')
    main = HYPR / 'hyprland.conf'
    text = main.read_text()
    if SOURCE_LINE not in text:
        shutil.copy2(main, main.with_name(f'hyprland.conf.bak-cati-{int(time.time())}'))
        main.write_text(text.rstrip('\n') + f'\n\n# Cati\n{SOURCE_LINE}\n')
    if os.getenv('HYPRLAND_INSTANCE_SIGNATURE'):
        hyprctl('reload')
        errors = hyprctl('configerrors').stdout.strip()
        if errors and 'cati.conf' in errors:
            if previous is None: HYPR_FILE.write_text('# Cati: configuración retirada por errores\n')
            else: HYPR_FILE.write_text(previous)
            hyprctl('reload')
            return f'Hyprland rechazó la configuración y se revirtió: {errors}'
    return None


def install_gnome(binding='<Super><Alt>u'):
    if not shutil.which('gsettings'): return 'gsettings no está disponible'
    schema = 'org.gnome.settings-daemon.plugins.media-keys'
    current = subprocess.run(['gsettings', 'get', schema, 'custom-keybindings'], capture_output=True, text=True).stdout.strip()
    paths = re.findall(r"'([^']+)'", current)
    entries = ((GNOME_PATH, 'Cati', 'gui', binding), (GNOME_VOICE, 'Cati: buscar por voz', 'voice', '<Super><Alt>v'))
    for path, *_ in entries:
        if path not in paths: paths.append(path)
    subprocess.run(['gsettings', 'set', schema, 'custom-keybindings', str(paths)], check=True)
    for path, name, verb, keys in entries:
        item = f'{schema}.custom-keybinding:{path}'
        for field, value in (('name', name), ('command', f'{LAUNCHER} {verb}'), ('binding', keys)):
            subprocess.run(['gsettings', 'set', item, field, value], check=True)
    return None


def integrate(shortcut=True, binding=None):
    """Instala la entrada de aplicación y lo que corresponda al escritorio actual; devuelve avisos."""
    notes = []
    install_entry()
    session = environment()
    if session == 'hyprland':
        if shortcut:
            problem = install_hyprland(binding or 'SUPER ALT, U')
            notes.append(problem or 'Hyprland: Super+Alt+U abre Cati y Super+Alt+V busca por voz; se queda residente al iniciar sesión.')
            if omarchy(): notes.append('Omarchy: Cati usa los colores de tu tema y aparece en Walker.')
    else:
        install_autostart()
        if shortcut and session == 'gnome':
            problem = install_gnome(binding or '<Super><Alt>u')
            notes.append(problem or 'GNOME: Super+Alt+U abre Cati y Super+Alt+V busca por voz.')
        elif shortcut and session == 'kde':
            notes.append('KDE: Meta+Alt+U queda propuesto en la entrada de Cati; confírmalo en Preferencias › Atajos si no se activa.')
        elif shortcut:
            notes.append('Asigna un atajo de tu escritorio al comando «cati gui».')
    return notes


def remove():
    """Deshace la integración sin tocar el índice ni los modelos."""
    for path in (DESKTOP, ICON, AUTOSTART, HYPR_FILE):
        path.unlink(missing_ok=True)
    if LAUNCHER.is_symlink(): LAUNCHER.unlink()
    main = HYPR / 'hyprland.conf'
    if main.exists() and SOURCE_LINE in main.read_text():
        main.write_text(main.read_text().replace(f'\n# Cati\n{SOURCE_LINE}\n', '\n').replace(SOURCE_LINE + '\n', ''))
        if os.getenv('HYPRLAND_INSTANCE_SIGNATURE'): hyprctl('reload')
    if environment() == 'gnome' and shutil.which('gsettings'):
        schema = 'org.gnome.settings-daemon.plugins.media-keys'
        current = subprocess.run(['gsettings', 'get', schema, 'custom-keybindings'], capture_output=True, text=True).stdout
        paths = [p for p in re.findall(r"'([^']+)'", current) if p not in {GNOME_PATH, GNOME_VOICE}]
        subprocess.run(['gsettings', 'set', schema, 'custom-keybindings', str(paths)], capture_output=True)
