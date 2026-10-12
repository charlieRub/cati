"""Comprobación de dependencias y nombres de paquete por distribución."""
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# requisito → (descripción, obligatorio, {familia: paquetes})
REQUIREMENTS = {
    'gtk': ('GTK 4.12+ y PyGObject', True, {
        'arch': 'python-gobject gtk4 gtksourceview5', 'debian': 'python3-gi gir1.2-gtk-4.0 gir1.2-gtksource-5',
        'fedora': 'python3-gobject gtk4 gtksourceview5', 'suse': 'python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-GtkSource-5'}),
    'gstreamer': ('Reproducción de vídeo y audio en la vista previa', False, {
        'arch': 'gst-plugins-good gst-libav', 'debian': 'gstreamer1.0-plugins-good gstreamer1.0-libav',
        'fedora': 'gstreamer1-plugins-good gstreamer1-plugin-libav', 'suse': 'gstreamer-plugins-good gstreamer-plugins-libav'}),
    'build': ('Compilador C++, CMake y git (para el motor local)', True, {
        'arch': 'base-devel cmake git', 'debian': 'build-essential cmake git', 'fedora': 'gcc-c++ make cmake git',
        'suse': 'gcc-c++ make cmake git'}),
    'ffmpeg': ('FFmpeg (vídeo y audio)', True, {
        'arch': 'ffmpeg', 'debian': 'ffmpeg', 'fedora': 'ffmpeg-free', 'suse': 'ffmpeg'}),
    'poppler': ('Poppler (PDF)', True, {
        'arch': 'poppler', 'debian': 'poppler-utils', 'fedora': 'poppler-utils', 'suse': 'poppler-tools'}),
    'tesseract': ('Tesseract OCR (texto en imágenes y escaneos)', True, {
        'arch': 'tesseract tesseract-data-eng tesseract-data-spa', 'debian': 'tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa',
        'fedora': 'tesseract tesseract-langpack-eng tesseract-langpack-spa', 'suse': 'tesseract-ocr tesseract-ocr-traineddata-english tesseract-ocr-traineddata-spanish'}),
    'python': ('NumPy y Pillow', True, {
        'arch': 'python-numpy python-pillow', 'debian': 'python3-numpy python3-pil python3-venv',
        'fedora': 'python3-numpy python3-pillow', 'suse': 'python3-numpy python3-Pillow'}),
    'tools': ('xdg-open y file', True, {
        'arch': 'xdg-utils file', 'debian': 'xdg-utils file', 'fedora': 'xdg-utils file', 'suse': 'xdg-utils file'}),
    'vulkan': ('Aceleración por GPU con Vulkan', False, {
        'arch': 'vulkan-headers vulkan-icd-loader shaderc', 'debian': 'libvulkan-dev glslc', 'fedora': 'vulkan-headers vulkan-loader-devel glslc',
        'suse': 'vulkan-devel shaderc'}),
    'metadata': ('ExifTool para ver todos los metadatos (EXIF, XMP, IPTC, GPS, ID3…)', False, {
        'arch': 'perl-image-exiftool', 'debian': 'libimage-exiftool-perl', 'fedora': 'perl-Image-ExifTool', 'suse': 'exiftool'}),
    'browser': ('Chromium para la vista previa de páginas HTML', False, {
        'arch': 'chromium', 'debian': 'chromium', 'fedora': 'chromium', 'suse': 'chromium'}),
}
INSTALL = {'arch': 'sudo pacman -S --needed', 'debian': 'sudo apt install', 'fedora': 'sudo dnf install', 'suse': 'sudo zypper install'}


def family():
    """Familia de paquetes según /etc/os-release (ID e ID_LIKE)."""
    try:
        info = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    except OSError:
        return None
    ids = (info.get('ID', '') + ' ' + info.get('ID_LIKE', '')).replace('"', '').split()
    for name, aliases in {'arch': {'arch', 'manjaro', 'endeavouros', 'cachyos', 'garuda'},
                          'debian': {'debian', 'ubuntu', 'linuxmint', 'pop', 'elementary', 'zorin'},
                          'fedora': {'fedora', 'rhel', 'centos', 'nobara'},
                          'suse': {'suse', 'opensuse', 'opensuse-tumbleweed', 'opensuse-leap'}}.items():
        if aliases & set(ids): return name
    return None


def gtk_ok():
    probe = ('import gi; gi.require_version("Gtk","4.0"); gi.require_version("GtkSource","5"); '
             'from gi.repository import Gtk, GtkSource; import sys; '
             'sys.exit(0 if (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12) else 1)')
    return subprocess.run([sys.executable, '-c', probe], capture_output=True).returncode == 0


def gstreamer_ok():
    probe = 'import gi; gi.require_version("Gst","1.0"); from gi.repository import Gst; Gst.init(None); import sys; sys.exit(0 if Gst.ElementFactory.find("avdec_h264") else 1)'
    return subprocess.run([sys.executable, '-c', probe], capture_output=True).returncode == 0


def python_ok():
    return subprocess.run([sys.executable, '-c', 'import numpy, PIL'], capture_output=True).returncode == 0


def vulkan_ok():
    headers = any(Path(p).exists() for p in ('/usr/include/vulkan/vulkan.h', '/usr/local/include/vulkan/vulkan.h'))
    return headers and bool(shutil.which('glslc'))


def check():
    """Devuelve {requisito: True/False}."""
    have = shutil.which
    return {
        'gtk': gtk_ok(),
        'gstreamer': gstreamer_ok(),
        'build': all(have(c) for c in ('cmake', 'git')) and bool(have('c++') or have('g++') or have('clang++')),
        'ffmpeg': bool(have('ffmpeg') and have('ffprobe')),
        'poppler': all(have(c) for c in ('pdftotext', 'pdftoppm', 'pdfinfo')),
        'tesseract': bool(have('tesseract')),
        'python': python_ok(),
        'tools': bool(have('xdg-open') and have('file')),
        'vulkan': vulkan_ok(),
        'metadata': bool(have('exiftool')),
        'browser': bool(have('chromium') or have('chromium-browser') or have('google-chrome-stable')),
    }


def install_command(missing, distro=None):
    distro = distro or family()
    if distro is None: return None
    packages = ' '.join(REQUIREMENTS[name][2][distro] for name in missing)
    return f'{INSTALL[distro]} {packages}' if packages else None


def report(optional=True):
    """Imprime el diagnóstico; devuelve la lista de requisitos obligatorios que faltan."""
    status = check()
    print(f'Sistema: {platform.freedesktop_os_release().get("PRETTY_NAME", "Linux") if hasattr(platform, "freedesktop_os_release") else "Linux"}'
          f', Python {platform.python_version()}')
    for name, (description, required, _) in REQUIREMENTS.items():
        if not required and not optional: continue
        mark = '✓' if status[name] else ('✗' if required else '-')
        print(f'  {mark} {description}' + ('' if status[name] or required else '  (opcional)'))
    missing = [n for n, ok in status.items() if not ok and REQUIREMENTS[n][1]]
    extras = [n for n, ok in status.items() if not ok and not REQUIREMENTS[n][1]]
    for label, names in (('Para instalar lo que falta', missing), ('Extras recomendados', extras)):
        if names:
            command = install_command(names)
            print(f'\n{label}:\n  {command}' if command else f'\n{label}: ' + ', '.join(REQUIREMENTS[n][0] for n in names))
    return missing
