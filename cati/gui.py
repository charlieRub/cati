"""Cati: interfaz GTK4 con lista a la izquierda y vista previa a la derecha.

Toma los colores del tema de Omarchy cuando existe; en cualquier otro escritorio
usa la paleta propia de Cati.

El proceso queda residente al cerrar la ventana: volver a abrirla es inmediato y
el índice en memoria sigue caliente. Las búsquedas van en dos fases: nombre y
palabras al instante, significado en cuanto el modelo responde.
"""
import json
import re
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Gsk', '4.0')
gi.require_version('Graphene', '1.0')
gi.require_version('GtkSource', '5')
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, GtkSource, Pango

from . import preview, properties
from .config import APP_ID, DATA, PROJECT, load, save
from .query import normalize, stem
from .search import searcher
from .store import opened

GLYPH = {'folder': '\U000F024B', 'document': '\U000F0219', 'pdf': '\U000F0226', 'image': '\U000F02E9',
         'video': '\U000F0567', 'audio': '\U000F0388', 'sheet': '\U000F021B', 'slides': '\U000F0227',
         'code': '\U000F0169', 'archive': '\U000F05C4', 'other': '\U000F0214', 'all': '\U000F0570'}
EXT_GLYPH = {'.html': '\U000F031D', '.htm': '\U000F031D', '.md': '\U000F0354', '.markdown': '\U000F0354'}
LABEL = {'all': 'Todo', 'folder': 'Carpetas', 'document': 'Documentos', 'pdf': 'PDF', 'image': 'Imágenes',
         'video': 'Vídeo', 'audio': 'Audio', 'sheet': 'Hojas', 'slides': 'Presentaciones', 'code': 'Código',
         'archive': 'Comprimidos', 'other': 'Otros'}
SINGULAR = {'folder': 'Carpeta', 'document': 'Documento', 'pdf': 'PDF', 'image': 'Imagen', 'video': 'Vídeo',
            'audio': 'Audio', 'sheet': 'Hoja de cálculo', 'slides': 'Presentación', 'code': 'Código',
            'archive': 'Comprimido', 'other': 'Archivo'}
ORDER = ['all', 'folder', 'document', 'pdf', 'image', 'video', 'audio', 'sheet', 'slides', 'code', 'archive', 'other']
SUGGESTIONS = ['documento html de pirámides', 'fotos de esta semana', 'pdfs en descargas', 'carpeta de egipto',
               'vídeos de más de 1 GB']
SEARCH, COG, CAMERA, EXACT = '\U000F0349', '\U000F0493', '\U000F0100', '\U000F0B34'
OPEN, FOLDER_OPEN, COPY, LEFT, RIGHT = '\U000F03CC', '\U000F0770', '\U000F018F', '\U000F0141', '\U000F0142'
INFO = '\U000F02FD'
TEXT_PREVIEW = {'document', 'code'}
MEDIA_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.mp3', '.wav', '.m4a', '.flac', '.ogg', '.opus', '.aac'}


CATI_PALETTE = {'background': '#15171c', 'foreground': '#e4e7ee', 'accent': '#5fd4c4', 'selection': '#262b36',
                'lighter_background': '#1e222a', 'darker_background': '#101216', 'muted': '#7d8597',
                'color8': '#343a47', 'red': '#ff7a90', 'green': '#5fd4c4', 'yellow': '#ffd479', 'blue': '#7b93ff',
                'magenta': '#c09bff', 'cyan': '#8fe3ff', 'orange': '#ffaf7a'}


def theme_colors(choice='auto'):
    """Paleta de Cati, o la del tema activo de Omarchy si existe y no se ha desactivado."""
    colors = dict(CATI_PALETTE)
    if choice == 'cati': return colors
    try:
        theme = tomllib.loads((Path.home() / '.config/omarchy/current/theme/colors.toml').read_text())
    except (OSError, ValueError):
        return colors
    colors.update({k: v for k, v in theme.items() if isinstance(v, str) and v.startswith('#')})
    if 'orange' not in theme and 'color3' in theme: colors['orange'] = theme['color3']
    return colors


def kind_color(colors):
    return {'folder': colors['blue'], 'document': colors['foreground'], 'pdf': colors['red'], 'image': colors['magenta'],
            'video': colors['orange'], 'audio': colors['green'], 'sheet': colors['green'], 'slides': colors['orange'],
            'code': colors['yellow'], 'archive': colors['muted'], 'other': colors['muted'], 'all': colors['accent']}


def css(c):
    return f"""
    window.cati, window.cati-dialog {{ background: {c['background']}; color: {c['foreground']};
      font-family: 'JetBrainsMono Nerd Font', 'Symbols Nerd Font Mono', 'Symbols Nerd Font', 'JetBrains Mono', monospace; font-size: 13px; }}
    .cati button, .cati-dialog button, .cati entry {{ all: unset; }}
    .cati scrollbar slider {{ background: {c['color8']}; min-width: 4px; min-height: 4px; border-radius: 0; }}
    .cati scrollbar {{ background: transparent; }}
    .cati paned > separator {{ background: {c['color8']}; min-width: 1px; }}

    .searchbar {{ padding: 16px 20px 14px 20px; background: {c['darker_background']}; border-bottom: 1px solid {c['color8']}; }}
    .prompt {{ color: {c['accent']}; font-size: 22px; margin-right: 14px; }}
    entry.query {{ font-size: 21px; color: {c['foreground']}; caret-color: {c['accent']}; min-height: 34px; }}
    entry.query placeholder {{ color: {c['muted']}; }}
    entry.query selection {{ background: {c['accent']}; color: {c['background']}; }}
    button.tool {{ color: {c['muted']}; padding: 4px 9px; margin-left: 4px; font-size: 16px; border: 1px solid transparent; }}
    button.tool:hover {{ color: {c['foreground']}; border-color: {c['color8']}; }}
    button.tool:checked {{ color: {c['background']}; background: {c['accent']}; border-color: {c['accent']}; }}
    .spinner {{ color: {c['accent']}; margin-right: 8px; }}

    .filters {{ padding: 9px 20px; border-bottom: 1px solid {c['color8']}; background: {c['background']}; }}
    button.chip {{ padding: 3px 10px; margin-right: 6px; color: {c['muted']}; border: 1px solid {c['color8']}; font-size: 12px; }}
    button.chip:hover {{ color: {c['foreground']}; }}
    button.chip:checked {{ color: {c['accent']}; border-color: {c['accent']}; background: alpha({c['accent']}, .08); }}
    button.chip .count {{ margin-left: 7px; opacity: .65; }}
    .tag {{ color: {c['magenta']}; font-size: 12px; margin-left: 10px; }}
    button.suggestion {{ padding: 3px 10px; margin-right: 6px; color: {c['foreground']}; border: 1px dashed {c['color8']}; font-size: 12px; }}
    button.suggestion:hover {{ border-color: {c['accent']}; color: {c['accent']}; }}
    .hint {{ color: {c['muted']}; font-size: 12px; }}

    .results {{ background: transparent; }}
    .results > row {{ padding: 9px 16px 9px 14px; border-left: 3px solid transparent; outline: none; }}
    .results > row:hover {{ background: {c['lighter_background']}; }}
    .results > row:selected {{ background: {c['selection']}; border-left-color: {c['accent']}; }}
    .section {{ color: {c['muted']}; font-size: 11px; letter-spacing: 1px; padding: 12px 18px 4px 18px; }}
    .glyph {{ font-size: 24px; min-width: 52px; min-height: 52px; background: {c['lighter_background']}; }}
    .thumb {{ min-width: 52px; min-height: 52px; background: {c['darker_background']}; }}
    .name {{ font-weight: bold; font-size: 14px; }}
    .meta {{ color: {c['muted']}; font-size: 11px; }}
    .excerpt {{ color: alpha({c['foreground']}, .78); font-size: 12px; }}
    .why {{ color: {c['muted']}; font-size: 10px; }}
    .copies {{ color: {c['magenta']}; font-size: 10px; margin-left: 6px; }}

    .preview {{ background: {c['darker_background']}; }}
    .preview-head {{ padding: 18px 22px 12px 22px; }}
    .preview-glyph {{ font-size: 30px; margin-right: 14px; }}
    .preview-title {{ font-size: 17px; font-weight: bold; }}
    .preview-path {{ color: {c['muted']}; font-size: 11px; }}
    .facts {{ padding: 0 22px 12px 22px; }}
    .fact {{ color: {c['foreground']}; font-size: 11px; padding: 2px 8px; margin-right: 6px; background: {c['lighter_background']}; }}
    .quote {{ margin: 0 22px 12px 22px; padding: 8px 12px; border-left: 2px solid {c['magenta']};
              background: alpha({c['magenta']}, .06); font-size: 12px; }}
    .canvas {{ background: {c['background']}; margin: 0 22px; border: 1px solid {c['color8']}; }}
    .canvas textview, .canvas textview text {{ background: {c['background']}; }}
    .page {{ background: white; }}
    .actions {{ padding: 12px 22px 16px 22px; }}
    button.action {{ padding: 5px 12px; margin-right: 8px; border: 1px solid {c['color8']}; color: {c['foreground']}; font-size: 12px; }}
    button.action:hover {{ border-color: {c['accent']}; color: {c['accent']}; }}
    button.action:checked {{ border-color: {c['accent']}; color: {c['accent']}; background: alpha({c['accent']}, .10); }}
    button.action.primary {{ background: {c['accent']}; color: {c['background']}; border-color: {c['accent']}; }}
    button.action .key {{ color: inherit; opacity: .6; margin-left: 8px; font-size: 10px; }}
    .pager {{ color: {c['muted']}; font-size: 12px; }}
    button.switch {{ padding: 2px 10px; color: {c['muted']}; border: 1px solid {c['color8']}; font-size: 11px; margin-left: 6px; }}
    button.switch:checked {{ color: {c['accent']}; border-color: {c['accent']}; }}
    .prop-section {{ color: {c['accent']}; font-size: 11px; letter-spacing: 1px; margin: 16px 0 6px 2px; }}
    .props {{ background: transparent; }}
    expander.prop-group {{ margin: 4px 0; }}
    expander.prop-group title {{ color: {c['foreground']}; font-size: 12px; padding: 6px 4px; }}
    expander.prop-group title:hover {{ color: {c['accent']}; }}
    .props > row {{ padding: 6px 10px; border-bottom: 1px solid alpha({c['color8']}, .5); }}
    .props > row:hover {{ background: {c['lighter_background']}; }}
    .prop-key {{ color: {c['muted']}; font-size: 12px; }}
    .prop-value {{ color: {c['foreground']}; font-size: 12px; }}
    .big-glyph {{ font-size: 96px; color: {c['color8']}; }}
    .empty {{ color: {c['muted']}; font-size: 13px; }}
    .entry-row {{ padding: 3px 10px; }}
    .entry-row:hover {{ background: {c['lighter_background']}; }}
    .grid-thumb {{ background: {c['background']}; }}

    .footer {{ padding: 7px 20px; border-top: 1px solid {c['color8']}; background: {c['darker_background']}; font-size: 11px; color: {c['muted']}; }}
    .brand {{ color: {c['accent']}; font-weight: bold; letter-spacing: 2px; margin-right: 14px; }}
    .dot {{ color: {c['green']}; margin-right: 8px; }}
    .dot.busy {{ color: {c['yellow']}; }}
    .dot.off {{ color: {c['muted']}; }}
    .kbd {{ color: {c['foreground']}; padding: 0 5px; border: 1px solid {c['color8']}; margin: 0 4px 0 12px; font-size: 10px; }}

    .cati-dialog .panel {{ padding: 26px; }}
    .cati-dialog .heading {{ font-size: 18px; font-weight: bold; color: {c['accent']}; }}
    .cati-dialog .root {{ padding: 6px 10px; background: {c['lighter_background']}; }}
    .cati-dialog checkbutton {{ color: {c['foreground']}; }}
    .cati-dialog button.action {{ padding: 6px 14px; border: 1px solid {c['color8']}; color: {c['foreground']}; }}
    .cati-dialog button.action:hover {{ border-color: {c['accent']}; color: {c['accent']}; }}
    .cati-dialog button.action.primary {{ background: {c['accent']}; color: {c['background']}; border-color: {c['accent']}; }}
    .cati-dialog .problem {{ color: {c['red']}; }}
    """


def source_scheme(c):
    """Esquema de GtkSourceView con los colores del tema; se regenera si el tema cambia."""
    folder = DATA / 'styles'
    folder.mkdir(parents=True, exist_ok=True)
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<style-scheme id="cati-theme" name="Cati" version="1.0">
  <style name="text" foreground="{c['foreground']}" background="{c['background']}"/>
  <style name="selection" foreground="{c['background']}" background="{c['accent']}"/>
  <style name="cursor" foreground="{c['accent']}"/>
  <style name="current-line" background="{c['lighter_background']}"/>
  <style name="line-numbers" foreground="{c['color8']}" background="{c['background']}"/>
  <style name="search-match" foreground="{c['background']}" background="{c['yellow']}" bold="true"/>
  <style name="bracket-match" foreground="{c['accent']}" bold="true"/>
  <style name="def:comment" foreground="{c['muted']}" italic="true"/>
  <style name="def:string" foreground="{c['green']}"/>
  <style name="def:keyword" foreground="{c['magenta']}" bold="true"/>
  <style name="def:statement" foreground="{c['magenta']}"/>
  <style name="def:type" foreground="{c['blue']}"/>
  <style name="def:function" foreground="{c['cyan']}"/>
  <style name="def:identifier" foreground="{c['foreground']}"/>
  <style name="def:number" foreground="{c['orange']}"/>
  <style name="def:constant" foreground="{c['orange']}"/>
  <style name="def:preprocessor" foreground="{c['yellow']}"/>
  <style name="def:special-char" foreground="{c['orange']}"/>
  <style name="def:heading" foreground="{c['accent']}" bold="true"/>
  <style name="def:emphasis" italic="true"/>
  <style name="def:strong-emphasis" bold="true"/>
  <style name="def:link-text" foreground="{c['blue']}" underline="single"/>
  <style name="def:net-address" foreground="{c['blue']}" underline="single"/>
  <style name="xml:element-name" foreground="{c['red']}"/>
  <style name="xml:attribute-name" foreground="{c['orange']}"/>
</style-scheme>
'''
    (folder / 'cati-theme.xml').write_text(xml)
    manager = GtkSource.StyleSchemeManager.get_default()
    if str(folder) not in manager.get_search_path(): manager.append_search_path(str(folder))
    manager.force_rescan()
    return manager.get_scheme('cati-theme')


def label(text='', css=None, **props):
    props.setdefault('xalign', 0)
    item = Gtk.Label(label=text, **props)
    for name in (css or '').split(): item.add_css_class(name)
    return item


def button(text, css, callback=None, tooltip=None, toggle=False):
    widget = Gtk.ToggleButton() if toggle else Gtk.Button()
    if isinstance(text, Gtk.Widget): widget.set_child(text)
    else: widget.set_label(text)
    for name in css.split(): widget.add_css_class(name)
    if tooltip: widget.set_tooltip_text(tooltip)
    if callback: widget.connect('toggled' if toggle else 'clicked', callback)
    widget.set_cursor(Gdk.Cursor.new_from_name('pointer'))
    return widget


def accent_pattern(word):
    groups = {'a': 'aáàäâ', 'e': 'eéèëê', 'i': 'iíìïî', 'o': 'oóòöô', 'u': 'uúùüû', 'n': 'nñ', 'c': 'cç'}
    return ''.join(f'[{groups[ch]}]' if ch in groups else re.escape(ch) for ch in word)


def mark(text, stems, color):
    """Resalta las raíces buscadas sin distinguir tildes ni mayúsculas."""
    if not stems or not text: return GLib.markup_escape_text(text)
    folded, owner = [], []
    for position, char in enumerate(text):
        for piece in normalize(char):
            folded.append(piece); owner.append(position)
    folded = ''.join(folded)
    spans = []
    for root in stems:
        for match in re.finditer(re.escape(root), folded):
            spans.append((owner[match.start()], owner[match.end() - 1] + 1))
    if not spans: return GLib.markup_escape_text(text)
    spans.sort()
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], end)
        else: merged.append([start, end])
    out, cursor = [], 0
    for start, end in merged:
        out.append(GLib.markup_escape_text(text[cursor:start]))
        out.append(f'<span foreground="{color}" weight="bold">{GLib.markup_escape_text(text[start:end])}</span>')
        cursor = end
    out.append(GLib.markup_escape_text(text[cursor:]))
    return ''.join(out)


def excerpt_markup(text, stems, color):
    text = ' '.join(text.split())
    if '«' in text:
        escaped = GLib.markup_escape_text(text)
        return escaped.replace('«', f'<span foreground="{color}" weight="bold">').replace('»', '</span>')
    return mark(text, stems, color)


def short_path(path):
    home = str(Path.home())
    path = str(path)
    return '~' + path[len(home):] if path.startswith(home) else path


def open_path(path, kind=None, loc=None, folder=False):
    path = Path(path)
    if not path.exists(): raise RuntimeError('El archivo ya no existe')
    if folder:
        # Ask the file manager to select the file when it supports it.
        try:
            Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, 'org.freedesktop.FileManager1',
                                           '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1', None
                                           ).call_sync('ShowItems', GLib.Variant('(ass)', ([path.as_uri()], '')),
                                                       Gio.DBusCallFlags.NONE, 2000, None)
            return
        except GLib.Error:
            subprocess.Popen(['xdg-open', str(path if path.is_dir() else path.parent)])
            return
    if kind in {'audio', 'video'} and loc and shutil_which('mpv'):
        subprocess.Popen(['mpv', f'--start={loc}', '--', str(path)])
    elif kind == 'pdf' and loc and shutil_which('evince'):
        subprocess.Popen(['evince', f'--page-label={int(loc)}', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def shutil_which(name):
    import shutil
    return shutil.which(name)


class Cati(Gtk.Application):
    def __init__(self, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background = background
        self.window = None
        self.searcher = None
        self.search_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cati-search')
        self.preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='cati-preview')
        self.thumb_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='cati-thumb')
        self.generation = 0
        self.preview_generation = 0
        self.timers = {}
        self.hits = []
        self.results_intent = None
        self.category = 'all'
        self.query_image = None
        self.media = None
        self.textures = {}
        self.current = None
        self.searching = False
        self.stats = {}
        self.connect('activate', self.activate)
        self.connect('shutdown', self.shutdown_pools)

    def shutdown_pools(self, *_):
        for pool in (self.search_pool, self.preview_pool, self.thumb_pool): pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ ventana
    def activate(self, *_):
        if self.window:
            self.show()
            return
        self.hold()  # stays resident after the window is hidden
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme()
        self.build()
        self.search_pool.submit(self.load_searcher)
        GLib.timeout_add_seconds(2, self.poll)
        self.poll()
        if not self.background: self.show()
        self.background = False

    def apply_theme(self):
        """Relee la paleta: un cambio de tema en Omarchy se ve al volver a abrir Cati."""
        colors = theme_colors(load().get('theme', 'auto'))
        if colors == getattr(self, 'colors', None): return
        self.colors = colors
        self.kind_colors = kind_color(colors)
        if hasattr(self.provider, 'load_from_string'): self.provider.load_from_string(css(colors))
        else: self.provider.load_from_data(css(colors).encode())
        self.scheme = source_scheme(colors)

    def load_searcher(self):
        try:
            self.searcher = searcher()
            self.searcher.refresh()
            GLib.idle_add(self.search_now if self.entry.get_text().strip() else self.show_recent)
            # Loading at login would hold the model in memory for nothing: warm it when the window opens.
            if self.window.get_visible(): self.searcher.warm()
            else: self.searcher.refresh()
        except Exception as exc:
            GLib.idle_add(self.problem, str(exc))

    def show(self):
        self.apply_theme()
        self.window.present()
        if self.searcher: self.search_pool.submit(self.searcher.warm)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)
        if not self.entry.get_text().strip() and not self.query_image and self.searcher: self.show_recent()

    def hide(self, *_):
        self.stop_media()
        self.window.set_visible(False)
        return True

    def build(self):
        self.window = Gtk.ApplicationWindow(application=self, title='Cati')
        self.window.add_css_class('cati')
        self.window.set_decorated(False)
        self.window.set_default_size(1240, 780)
        self.window.connect('close-request', self.hide)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_child(root)

        bar = Gtk.Box(spacing=0, css_classes=['searchbar'])
        bar.append(label(SEARCH, 'prompt'))
        self.entry = Gtk.Entry(hexpand=True, placeholder_text='Describe lo que buscas: «documento html de pirámides», «fotos de ayer»…')
        self.entry.add_css_class('query')
        self.entry.connect('changed', self.changed)
        bar.append(self.entry)
        self.spinner = label('', 'spinner')
        bar.append(self.spinner)
        self.exact = button(EXACT, 'tool', lambda *_: self.search_now(), 'Solo palabras exactas (sin modelo)', toggle=True)
        bar.append(self.exact)
        bar.append(button(CAMERA, 'tool', self.choose_image, 'Buscar imágenes parecidas a una foto (o arrástrala aquí)'))
        bar.append(button(COG, 'tool', self.settings, 'Carpetas y ajustes (Ctrl+,)'))
        root.append(bar)

        self.filters = Gtk.Box(css_classes=['filters'])
        self.chips = Gtk.Box()
        self.filters.append(self.chips)
        self.tags = Gtk.Box(hexpand=True)
        self.filters.append(self.tags)
        self.timing = label('', 'hint')
        self.filters.append(self.timing)
        root.append(self.filters)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, wide_handle=False)
        self.paned.set_position(520)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.set_size_request(380, -1)
        self.section = label('', 'section')
        left.append(self.section)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False, css_classes=['results'])
        self.results.connect('row-activated', lambda _, row: self.open_hit(row.hit))
        self.results.connect('row-selected', self.selected)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.results)
        self.result_scroll = scroll
        left.append(scroll)
        self.paned.set_start_child(left)
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=['preview'])
        self.preview_box.set_size_request(420, -1)
        self.paned.set_end_child(self.preview_box)
        root.append(self.paned)

        footer = Gtk.Box(css_classes=['footer'])
        footer.append(label('cati', 'brand'))
        self.dot = label('●', 'dot')
        footer.append(self.dot)
        self.status = label('Cargando índice…', hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        footer.append(self.status)
        for key, text in [('↑↓', 'navegar'), ('⏎', 'abrir'), ('Ctrl ⏎', 'carpeta'), ('Ctrl C', 'copiar ruta'), ('Tab', 'tipo'), ('Esc', 'cerrar')]:
            footer.append(label(key, 'kbd'))
            footer.append(label(text))
        root.append(footer)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.key)
        self.window.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect('drop', self.dropped)
        self.window.add_controller(drop)
        self.empty_preview('Hola, soy Cati', 'Dime qué buscas con tus palabras: lo encuentro por nombre, por contenido o por lo que se ve, aunque no recuerdes cómo se llama.')

    # ----------------------------------------------------------- teclado
    def key(self, controller, key, code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if key == Gdk.KEY_Escape:
            if self.query_image: self.set_image(None)
            else: self.hide()
            return True
        if key in {Gdk.KEY_Down, Gdk.KEY_Up}:
            self.move(1 if key == Gdk.KEY_Down else -1)
            return True
        if key in {Gdk.KEY_Page_Down, Gdk.KEY_Page_Up}:
            self.move(8 if key == Gdk.KEY_Page_Down else -8)
            return True
        if key in {Gdk.KEY_Return, Gdk.KEY_KP_Enter} and alt:
            if getattr(self, 'props_button', None) and self.shown: self.props_button.set_active(not self.props_button.get_active())
            return True
        if key in {Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            if self.current: self.open_hit(self.current, folder=ctrl)
            else: self.search_now()
            return True
        if key in {Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab}:
            self.cycle_category(-1 if shift or key == Gdk.KEY_ISO_Left_Tab else 1)
            return True
        if ctrl and key in {Gdk.KEY_c, Gdk.KEY_C} and self.current and not self.entry.get_selection_bounds():
            self.copy_path(self.current)
            return True
        if ctrl and key == Gdk.KEY_comma:
            self.settings()
            return True
        if ctrl and key in {Gdk.KEY_l, Gdk.KEY_L}:
            self.entry.grab_focus(); self.entry.select_region(0, -1)
            return True
        if alt and key in {Gdk.KEY_Left, Gdk.KEY_Right} and getattr(self, 'pager', None):
            self.pager(-1 if key == Gdk.KEY_Left else 1)
            return True
        if not self.entry.has_focus() and not ctrl and not alt:
            unicode = Gdk.keyval_to_unicode(key)
            if unicode and chr(unicode).isprintable():
                self.entry.grab_focus()
                position = self.entry.get_position()
                self.entry.insert_text(chr(unicode), position)
                self.entry.set_position(position + 1)
                return True
        return False

    def move(self, step):
        self.user_moved = True
        row = self.results.get_selected_row()
        index = (row.get_index() + step) if row else 0
        total = 0
        while self.results.get_row_at_index(total): total += 1
        if not total: return
        target = self.results.get_row_at_index(max(0, min(total - 1, index)))
        self.results.select_row(target)
        adjustment = self.result_scroll.get_vadjustment()
        ok, bounds = target.compute_bounds(self.results)
        if ok:
            top, bottom = bounds.get_y(), bounds.get_y() + bounds.get_height()
            if top < adjustment.get_value(): adjustment.set_value(top)
            elif bottom > adjustment.get_value() + adjustment.get_page_size():
                adjustment.set_value(bottom - adjustment.get_page_size())

    # ----------------------------------------------------------- búsqueda
    def schedule(self, name, delay, callback):
        if name in self.timers: GLib.source_remove(self.timers.pop(name))
        def fire():
            self.timers.pop(name, None)
            callback()
            return False
        self.timers[name] = GLib.timeout_add(delay, fire)

    def changed(self, *_):
        self.generation += 1
        self.user_moved = False
        text = self.entry.get_text().strip()
        if not text and not self.query_image:
            for name in list(self.timers): GLib.source_remove(self.timers.pop(name))
            self.show_recent()
            return
        self.schedule('quick', 30, lambda: self.search_now(semantic=False))
        self.schedule('full', 240, self.search_now)

    def search_now(self, *_, semantic=True):
        if self.searcher is None: return
        if semantic:
            if 'full' in self.timers: GLib.source_remove(self.timers.pop('full'))
            self.generation += 1
        generation = self.generation
        query = self.entry.get_text().strip()
        if not query and not self.query_image:
            self.show_recent(); return
        exact = self.exact.get_active()
        semantic = semantic and not exact
        image_path = self.query_image
        if semantic: self.busy(True)

        def work():
            if generation != self.generation: return None
            return self.searcher.run(query, limit=150, lexical=exact, image_path=image_path, semantic=semantic)

        def done(future):
            try: results = future.result()
            except Exception as exc:
                GLib.idle_add(self.problem, str(exc), generation)
                return
            if results is not None: GLib.idle_add(self.render, results, generation, semantic)
        self.search_pool.submit(work).add_done_callback(done)

    def busy(self, active):
        self.searching = active
        self.spinner.set_text('\U000F0772' if active else '')

    def problem(self, text, generation=None):
        if generation is not None and generation != self.generation: return False
        self.busy(False)
        self.section.set_text('')
        self.empty_preview('No se pudo buscar', text)
        return False

    def show_recent(self):
        if self.searcher is None: return False
        self.generation += 1
        generation = self.generation
        def done(future):
            try: hits = future.result()
            except Exception as exc: GLib.idle_add(self.problem, str(exc)); return
            GLib.idle_add(self.render_recent, hits, generation)
        self.search_pool.submit(self.searcher.recent, 40).add_done_callback(done)
        return False

    def render_recent(self, hits, generation):
        if generation != self.generation: return False
        self.busy(False)
        self.results_intent = None
        self.hits = hits
        self.category = 'all'
        self.clear(self.chips); self.clear(self.tags)
        self.chips.append(label('Prueba:', 'hint'))
        self.chips.get_last_child().set_margin_end(10)
        for text in SUGGESTIONS:
            self.chips.append(button(text, 'suggestion', lambda _, value=text: self.entry.set_text(value)))
        self.timing.set_text('')
        self.fill(hits, 'MODIFICADOS RECIENTEMENTE', [])
        return False

    def render(self, results, generation, final):
        if generation != self.generation: return False
        if final: self.busy(False)
        self.results_intent = results.intent
        self.hits = results.hits
        counts = {}
        for hit in self.hits: counts[hit['kind']] = counts.get(hit['kind'], 0) + 1
        if self.category != 'all' and self.category not in counts: self.category = 'all'
        self.clear(self.chips)
        for key in ORDER:
            amount = len(self.hits) if key == 'all' else counts.get(key)
            if not amount: continue
            content = Gtk.Box()
            glyph = label(GLYPH[key])
            glyph.set_margin_end(6)
            content.append(glyph)
            content.append(label(LABEL[key]))
            content.append(label(str(amount), 'count'))
            chip = button(content, 'chip', toggle=True)
            chip.set_active(key == self.category)
            chip.kind = key
            chip.connect('toggled', self.chip_toggled)
            self.chips.append(chip)
        self.clear(self.tags)
        for tag in results.intent.labels:
            self.tags.append(label('\U000F04F9 ' + tag, 'tag'))
        if self.query_image:
            self.tags.append(label(f'\U000F02E9 como {Path(self.query_image).name}', 'tag'))
        phase = 'con significado' if results.semantic else 'por nombre y palabras'
        self.timing.set_text(f'{results.elapsed * 1000:.0f} ms {phase}')
        self.apply_category()
        return False

    def chip_toggled(self, chip):
        if not chip.get_active():
            if chip.kind == self.category: chip.set_active(True)
            return
        self.category = chip.kind
        child = self.chips.get_first_child()
        while child:
            if child is not chip and isinstance(child, Gtk.ToggleButton): child.set_active(False)
            child = child.get_next_sibling()
        self.apply_category()

    def cycle_category(self, step):
        kinds = []
        child = self.chips.get_first_child()
        while child:
            if isinstance(child, Gtk.ToggleButton): kinds.append(child)
            child = child.get_next_sibling()
        if not kinds: return
        index = next((i for i, chip in enumerate(kinds) if chip.kind == self.category), 0)
        kinds[(index + step) % len(kinds)].set_active(True)

    def apply_category(self):
        hits = self.hits if self.category == 'all' else [h for h in self.hits if h['kind'] == self.category]
        stems = [stem(t) for t in (self.results_intent.terms if self.results_intent else [])]
        if not hits:
            title = 'SIN RESULTADOS' if not self.searching else 'BUSCANDO…'
        else:
            title = f"{len(hits)} RESULTADOS" + ('' if self.category == 'all' else f' EN {LABEL[self.category].upper()}')
        self.fill(hits, title, stems)

    def fill(self, hits, title, stems):
        # Keep the selection across the second phase only if the user picked it.
        previous = self.current['path'] if self.current and getattr(self, 'user_moved', False) else None
        self.section.set_text(title)
        self.clear(self.results)
        selected = None
        for index, hit in enumerate(hits[:120]):
            row = self.row(hit, stems)
            self.results.append(row)
            if hit['path'] == previous and selected is None: selected = row
        if not hits:
            self.current = None
            if self.entry.get_text().strip() or self.query_image:
                if not self.searching:
                    self.empty_preview('Nada por aquí', 'Prueba con otras palabras, quita un filtro o espera a que termine el análisis: '
                                       'los nombres ya están todos, el contenido se completa en segundo plano.')
            return
        target = selected or self.results.get_row_at_index(0)
        self.results.select_row(target)
        if selected is None: self.result_scroll.get_vadjustment().set_value(0)

    @staticmethod
    def clear(container):
        while child := container.get_first_child(): container.remove(child)

    # -------------------------------------------------------------- filas
    def row(self, hit, stems):
        row = Gtk.ListBoxRow()
        row.hit = hit
        layout = Gtk.Box(spacing=14)
        visual = self.visual(hit, 52)
        layout.append(visual)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(spacing=6)
        name = label('', 'name', ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        name.set_markup(mark(hit['name'], stems, self.colors['accent']))
        top.append(name)
        if hit['copies']:
            count = len(hit['copies'])
            top.append(label(f"+{count} {'copia' if count == 1 else 'copias'}", 'copies'))
        top.append(label(', '.join(hit['reasons']), 'why', ellipsize=Pango.EllipsizeMode.END, max_width_chars=16))
        text.append(top)
        meta = [f"<span foreground=\"{self.kind_colors[hit['kind']]}\">{SINGULAR[hit['kind']]}</span>"]
        if hit['kind'] != 'folder' and hit['size']: meta.append(preview.human_size(hit['size']))
        if hit['mtime']: meta.append(preview.human_time(hit['mtime'] / 1e9))
        if hit['loc'] is not None:
            if hit['kind'] == 'pdf': meta.append(f"página {int(hit['loc'])}")
            elif hit['kind'] == 'code': meta.append(f"línea {int(hit['loc'])}")
            elif hit['kind'] in {'audio', 'video'}: meta.append(preview.clock(hit['loc']))
        line = Gtk.Box(spacing=14)
        for part in meta:
            piece = label('', 'meta')
            piece.set_markup(part)
            line.append(piece)
        line.append(label(short_path(Path(hit['path']).parent), 'meta', ellipsize=Pango.EllipsizeMode.START, hexpand=True))
        text.append(line)
        if hit['excerpt']:
            snippet = label('', 'excerpt', ellipsize=Pango.EllipsizeMode.END, lines=2, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            snippet.set_markup(excerpt_markup(hit['excerpt'], stems, self.colors['yellow']))
            text.append(snippet)
        layout.append(text)
        row.set_child(layout)
        context = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context.connect('pressed', lambda *_: self.properties_for(row))
        row.add_controller(context)
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect('prepare', lambda *_: Gdk.ContentProvider.new_for_value(
            Gdk.FileList.new_from_array([Gio.File.new_for_path(hit['path'])])))
        row.add_controller(drag)
        return row

    def properties_for(self, row):
        self.user_moved = True
        self.props_mode = True
        if self.results.get_selected_row() is row and self.shown is row.hit:
            self.props_button.set_active(True)
        else:
            self.results.select_row(row)

    def visual(self, hit, size):
        """Miniatura real si existe; si no, el glifo del tipo con su color."""
        ext = Path(hit['path']).suffix.lower()
        glyph = label(EXT_GLYPH.get(ext, GLYPH[hit['kind']]), 'glyph', xalign=.5, valign=Gtk.Align.CENTER)
        glyph.set_markup(f"<span foreground=\"{self.kind_colors[hit['kind']]}\">{EXT_GLYPH.get(ext, GLYPH[hit['kind']])}</span>")
        if hit['kind'] not in {'image', 'video', 'pdf'}: return glyph
        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120,
                          hexpand=False, valign=Gtk.Align.CENTER)
        stack.add_named(glyph, 'glyph')
        picture = Thumb(size)
        stack.add_named(picture, 'thumb')
        key = (hit['path'], hit['mtime'])
        if key in self.textures:
            picture.set_paintable(self.textures[key]); stack.set_visible_child_name('thumb')
            return stack
        generation = self.generation

        def work():
            if generation != self.generation: return None
            source = preview.thumbnail(hit['path'], hit['kind'])
            return Gdk.Texture.new_from_filename(str(source)) if source else None

        def done(future):
            try: texture = future.result()
            except Exception: return
            if texture is None: return
            def apply():
                self.textures[key] = texture
                if len(self.textures) > 600: self.textures.pop(next(iter(self.textures)))
                picture.set_paintable(texture); stack.set_visible_child_name('thumb')
                return False
            GLib.idle_add(apply)
        self.thumb_pool.submit(work).add_done_callback(done)
        return stack

    # -------------------------------------------------------- vista previa
    def selected(self, box, row):
        if row is None: return
        self.current = row.hit
        # The second search phase often reselects the same file: keep its preview (and playback) as is.
        shown = getattr(self, 'shown', None)
        terms = tuple(self.results_intent.terms) if self.results_intent else ()
        if shown and shown['path'] == row.hit['path'] and shown['loc'] == row.hit['loc'] and (
                getattr(self, 'shown_terms', None) == terms or row.hit['kind'] not in TEXT_PREVIEW | {'pdf'}):
            return
        self.schedule('preview', 45, lambda: self.show_preview(row.hit))

    def stop_media(self):
        if self.media is not None:
            try: self.media.pause()
            except Exception: pass
            self.media = None
        self.pager = None

    def frame(self, hit):
        """Cabecera, datos y acciones comunes; devuelve el contenedor del contenido."""
        self.stop_media()
        self.preview_generation += 1
        self.clear(self.preview_box)
        path = Path(hit['path'])
        head = Gtk.Box(css_classes=['preview-head'])
        ext = path.suffix.lower()
        icon = label('', 'preview-glyph', valign=Gtk.Align.START)
        icon.set_markup(f"<span foreground=\"{self.kind_colors[hit['kind']]}\">{EXT_GLYPH.get(ext, GLYPH[hit['kind']])}</span>")
        head.append(icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = label(path.name, 'preview-title', wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        titles.append(title)
        where = label(short_path(path.parent), 'preview-path', ellipsize=Pango.EllipsizeMode.START, selectable=True)
        where.set_tooltip_text(str(path))
        titles.append(where)
        head.append(titles)
        self.preview_box.append(head)
        self.facts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=12, css_classes=['facts'], row_spacing=4)
        facts = [SINGULAR[hit['kind']] + (f' {ext[1:].upper()}' if ext and hit['kind'] not in {'folder', 'pdf'} else '')]
        if hit['kind'] != 'folder' and hit['size']: facts.append(preview.human_size(hit['size']))
        if hit['mtime']: facts.append('modificado ' + preview.human_time(hit['mtime'] / 1e9))
        if hit['indexed']: facts.append('contenido analizado')
        for fact in facts: self.add_fact(fact)
        self.preview_box.append(self.facts)
        if hit['excerpt'] and hit['kind'] not in TEXT_PREVIEW:
            stems = [stem(t) for t in (self.results_intent.terms if self.results_intent else [])]
            quote = label('', 'quote', wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=4, ellipsize=Pango.EllipsizeMode.END)
            where_text = f"Página {int(hit['loc'])}: " if hit['kind'] == 'pdf' and hit['loc'] else (
                f"En {preview.clock(hit['loc'])}: " if hit['kind'] in {'audio', 'video'} and hit['loc'] is not None else '')
            quote.set_markup(f'<b>{GLib.markup_escape_text(where_text)}</b>' + excerpt_markup(hit['excerpt'][:500], stems, self.colors['yellow']))
            self.preview_box.append(quote)
        self.canvas = Gtk.Stack(vexpand=True, css_classes=['canvas'], transition_type=Gtk.StackTransitionType.CROSSFADE,
                                transition_duration=120)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.canvas.add_named(body, 'preview')
        self.props_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.canvas.add_named(self.props_page, 'props')
        self.preview_box.append(self.canvas)
        actions = Gtk.Box(css_classes=['actions'])
        primary = Gtk.Box()
        primary.append(label(f'{OPEN}  Abrir'))
        primary.append(label('⏎', 'key'))
        actions.append(button(primary, 'action primary', lambda *_: self.open_hit(hit)))
        second = Gtk.Box()
        second.append(label(f'{FOLDER_OPEN}  Mostrar en carpeta'))
        second.append(label('Ctrl ⏎', 'key'))
        actions.append(button(second, 'action', lambda *_: self.open_hit(hit, folder=True)))
        third = Gtk.Box()
        third.append(label(f'{COPY}  Copiar ruta'))
        third.append(label('Ctrl C', 'key'))
        actions.append(button(third, 'action', lambda *_: self.copy_path(hit)))
        info = Gtk.Box()
        info.append(label(f'{INFO}  Propiedades'))
        info.append(label('Alt ⏎', 'key'))
        self.props_button = button(info, 'action', self.toggle_properties, toggle=True)
        actions.append(self.props_button)
        self.toolbar = Gtk.Box(hexpand=True, halign=Gtk.Align.END)
        actions.append(self.toolbar)
        self.preview_box.append(actions)
        if hit['copies']:
            more = label(f"También en {len(hit['copies'])} {'ubicación' if len(hit['copies']) == 1 else 'ubicaciones'} más: " + ', '.join(short_path(Path(p).parent) for p in hit['copies'][:3]),
                         'preview-path', ellipsize=Pango.EllipsizeMode.END)
            more.set_margin_start(22); more.set_margin_bottom(12)
            more.set_tooltip_text('\n'.join(hit['copies']))
            self.preview_box.append(more)
        return body

    def add_fact(self, text):
        child = label(text, 'fact')
        self.facts.append(child)

    def empty_preview(self, title, text):
        self.shown = None
        self.stop_media()
        self.preview_generation += 1
        self.clear(self.preview_box)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, vexpand=True, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        box.set_margin_start(40); box.set_margin_end(40)
        logo = PROJECT / 'assets/cati.svg'
        if logo.exists():
            mark = Gtk.Picture.new_for_filename(str(logo))
            mark.set_size_request(112, 112)
            mark.set_can_shrink(False)
            mark.set_halign(Gtk.Align.CENTER)
            box.append(mark)
        else:
            box.append(label(SEARCH, 'big-glyph', xalign=.5))
        box.append(label(title, 'preview-title', xalign=.5, justify=Gtk.Justification.CENTER, wrap=True))
        box.append(label(text, 'empty', xalign=.5, justify=Gtk.Justification.CENTER, wrap=True, max_width_chars=48))
        self.preview_box.append(box)

    def background_job(self, work, apply):
        generation = self.preview_generation

        def done(future):
            try: value = future.result()
            except Exception as exc: value = exc
            GLib.idle_add(lambda: (apply(value) if generation == self.preview_generation else None, False)[1])
        self.preview_pool.submit(work).add_done_callback(done)

    def placeholder(self, body, text='Preparando vista previa…'):
        wait = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        spinner.set_size_request(28, 28)
        wait.append(spinner)
        wait.append(label(text, 'empty', xalign=.5))
        body.append(wait)
        return wait

    def show_preview(self, hit):
        if hit is not self.current: return
        self.shown = hit
        self.shown_terms = tuple(self.results_intent.terms) if self.results_intent else ()
        body = self.frame(hit)
        self.pending_preview = lambda: self.build_preview(body, hit)
        self.props_loaded = False
        if getattr(self, 'props_mode', False):
            self.props_button.set_active(True)  # shows the properties; the preview loads only if asked for
        else:
            self.run_preview()

    def run_preview(self):
        build, self.pending_preview = getattr(self, 'pending_preview', None), None
        if build: build()

    def toggle_properties(self, toggle):
        self.props_mode = toggle.get_active()
        self.canvas.set_visible_child_name('props' if self.props_mode else 'preview')
        self.toolbar.set_visible(not self.props_mode)
        if self.props_mode:
            if not self.props_loaded: self.load_properties(self.shown)
        else:
            self.run_preview()

    def load_properties(self, hit):
        self.props_loaded = True
        self.clear(self.props_page)
        wait = self.placeholder(self.props_page, 'Leyendo propiedades…')
        searcher_ = self.searcher

        def work():
            sections = properties.collect(hit['path'], hit['kind'])
            sections.append(('En Cati', self.index_rows(hit, searcher_)))
            return sections, properties.everything(hit['path'])

        def apply(value):
            self.props_page.remove(wait)
            if isinstance(value, Exception):
                self.props_page.append(label(f'No se pudieron leer: {value}', 'empty', xalign=.5, vexpand=True)); return
            value, extra = value
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content.set_margin_start(16); content.set_margin_end(16); content.set_margin_bottom(16)
            for title, rows in value:
                if not rows: continue
                content.append(label(title.upper(), 'prop-section'))
                listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['props'])
                listing.set_activate_on_single_click(True)
                listing.connect('row-activated', self.copy_property)
                for key, value_text in rows:
                    listing.append(self.property_row(key, str(value_text)))
                if title == 'General' and hit['kind'] != 'folder':
                    listing.append(self.hash_row(hit['path']))
                content.append(listing)
            if extra is None:
                content.append(label('TODOS LOS METADATOS', 'prop-section'))
                content.append(label('Instala ExifTool para ver todos los metadatos (EXIF, XMP, IPTC, GPS, ID3…). '
                                     'Ejecuta «cati doctor» para ver el paquete de tu sistema.', 'prop-key', wrap=True))
            elif extra:
                total = sum(len(rows) for _, rows in extra)
                content.append(label(f'TODOS LOS METADATOS ({total})', 'prop-section'))
                for group, rows in extra:
                    expander = Gtk.Expander(label=f'{group}  ({len(rows)})', css_classes=['prop-group'])
                    listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['props'])
                    listing.set_activate_on_single_click(True)
                    listing.connect('row-activated', self.copy_property)
                    for key, value_text in rows: listing.append(self.property_row(key, value_text))
                    expander.set_child(listing)
                    content.append(expander)
            scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroll.set_child(content)
            self.props_page.append(scroll)
        self.background_job(work, apply)

    def property_row(self, key, value):
        line = Gtk.Box(spacing=16)
        line.append(label(key, 'prop-key', width_chars=18, max_width_chars=18, ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.START))
        line.append(label(value, 'prop-value', hexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        row = Gtk.ListBoxRow(child=line, activatable=True)
        row.key, row.value = key, value
        row.set_tooltip_text('Clic para copiar')
        row.set_cursor(Gdk.Cursor.new_from_name('pointer'))
        return row

    def hash_row(self, path):
        row = self.property_row('SHA-256', 'Clic para calcular')
        row.value = None

        def compute():
            row.get_child().get_last_child().set_text('Calculando…')
            def done(value):
                if isinstance(value, Exception): row.get_child().get_last_child().set_text(f'No disponible: {value}'); return
                row.value = value
                row.get_child().get_last_child().set_text(value)
            self.background_job(lambda: properties.sha256(path), done)
        row.compute = compute
        return row

    def copy_property(self, listing, row):
        if getattr(row, 'value', None) is None:
            if hasattr(row, 'compute'): row.compute()
            return
        self.window.get_clipboard().set_content(Gdk.ContentProvider.new_for_bytes(
            'text/plain;charset=utf-8', GLib.Bytes.new(row.value.encode())))
        self.timing.set_text(f'{row.key} copiado')

    @staticmethod
    def index_rows(hit, searcher_):
        rows = []
        stats = searcher_.file_stats(hit['path']) if searcher_ else None
        if hit['kind'] == 'folder':
            rows.append(('Búsqueda', 'Por nombre y por el contenido de los archivos que guarda'))
        elif stats:
            pieces = {'document': 'texto', 'pdf': 'páginas', 'ocr': 'texto en la imagen', 'image': 'imagen',
                      'pdf-image': 'páginas escaneadas', 'video-frame': 'fotogramas', 'audio': 'audio', 'code': 'código', 'name': 'nombre'}
            kinds = [pieces.get(k, k) for k in stats['kinds']]
            state = 'completo' if stats['complete'] else 'en curso'
            rows.append(('Análisis', f"{state}, {stats['segments']} fragmentos ({', '.join(dict.fromkeys(kinds))})"))
            rows.append(('Analizado', properties.when(stats['indexed'] / 1e9) if stats['indexed'] else None))
        else:
            rows.append(('Análisis', 'Pendiente: de momento se encuentra por su nombre'))
        reasons = hit.get('reasons') or []
        if reasons and reasons != ['reciente']:
            rows.append(('Por qué aparece', reasons[0] if len(reasons) == 1 else ', '.join(reasons[:-1]) + ' y ' + reasons[-1]))
        if hit.get('loc') is not None:
            if hit['kind'] == 'pdf': rows.append(('Coincidencia', f"página {int(hit['loc'])}"))
            elif hit['kind'] in {'audio', 'video'}: rows.append(('Coincidencia', f"en {preview.clock(hit['loc'])}"))
            elif hit['kind'] == 'code': rows.append(('Coincidencia', f"línea {int(hit['loc'])}"))
        for index, path in enumerate(hit.get('copies') or []):
            rows.append(('Copia' if len(hit['copies']) == 1 else f'Copia {index + 1}', path))
        return [r for r in rows if r[1]]

    def build_preview(self, body, hit):
        kind = hit['kind']
        ext = Path(hit['path']).suffix.lower()
        try:
            if kind == 'folder': self.preview_folder(body, hit)
            elif kind == 'image' and ext not in {'.psd', '.xcf', '.raw', '.cr2', '.nef', '.dng', '.kra'}: self.preview_image(body, hit)
            elif kind == 'pdf': self.preview_pdf(body, hit)
            elif kind in {'video', 'audio'} or ext in MEDIA_EXTS: self.preview_media(body, hit)
            elif ext in {'.html', '.htm'}: self.preview_html(body, hit)
            elif kind in TEXT_PREVIEW or kind == 'sheet' and ext in {'.csv', '.tsv'}: self.preview_text(body, hit)
            elif kind == 'archive': self.preview_archive(body, hit)
            else: self.preview_other(body, hit)
        except Exception as exc:
            self.clear(body)
            body.append(label(f'No se pudo mostrar: {exc}', 'empty', xalign=.5, vexpand=True))

    def scrolled_picture(self, body, path):
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(Page(Gdk.Texture.new_from_filename(str(path))))
        body.append(scroll)
        return scroll

    def preview_image(self, body, hit):
        wait = self.placeholder(body)

        def work():
            info = preview.image_info(hit['path'])
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(hit['path'], 2000, 2000, True)
            pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
            return info, Gdk.Texture.new_for_pixbuf(pixbuf)

        def apply(value):
            body.remove(wait)
            if isinstance(value, Exception):
                body.append(label(f'No se pudo abrir la imagen: {value}', 'empty', xalign=.5, vexpand=True)); return
            info, texture = value
            if info.get('resolution'): self.add_fact(info['resolution'])
            picture = Gtk.Picture(paintable=texture, content_fit=Gtk.ContentFit.CONTAIN, vexpand=True, can_shrink=True)
            picture.set_margin_top(10); picture.set_margin_bottom(10); picture.set_margin_start(10); picture.set_margin_end(10)
            body.append(picture)
        self.background_job(work, apply)

    def preview_pdf(self, body, hit):
        state = {'page': int(hit['loc'] or 1), 'pages': None}
        counter = label('', 'pager')
        previous = button(LEFT, 'switch', lambda *_: turn(-1), 'Página anterior (Alt ←)')
        following = button(RIGHT, 'switch', lambda *_: turn(1), 'Página siguiente (Alt →)')
        for widget in (previous, counter, following): self.toolbar.append(widget)
        counter.set_margin_start(8); counter.set_margin_end(2)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        body.append(holder)

        def turn(step):
            if state['pages'] is None: return
            page = max(1, min(state['pages'], state['page'] + step))
            if page != state['page']: state['page'] = page; load()
        self.pager = turn

        def load():
            self.clear(holder)
            wait = self.placeholder(holder, f"Renderizando página {state['page']}…")
            page = state['page']

            def work():
                if state['pages'] is None: state['pages'] = preview.pdf_pages(hit['path'])
                return preview.pdf_page(hit['path'], min(page, state['pages']))

            def apply(value):
                self.clear(holder)
                if isinstance(value, Exception):
                    holder.append(label(f'No se pudo renderizar: {value}', 'empty', xalign=.5, vexpand=True)); return
                counter.set_text(f"{state['page']} / {state['pages']}")
                self.scrolled_picture(holder, value)
            self.background_job(work, apply)
        load()

    def preview_html(self, body, hit):
        stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        body.append(stack)
        rendered = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        stack.add_named(rendered, 'view')
        source = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        stack.add_named(source, 'code')
        view = button('Vista', 'switch', toggle=True)
        code = button('Código', 'switch', toggle=True)
        code.set_group(view)
        view.set_active(True)
        view.connect('toggled', lambda b: stack.set_visible_child_name('view' if b.get_active() else 'code'))
        self.toolbar.append(view); self.toolbar.append(code)
        self.preview_text(source, hit, language='html')
        wait = self.placeholder(rendered, 'Renderizando la página (sin conexión a internet)…')

        def apply(value):
            rendered.remove(wait)
            if isinstance(value, Exception) or value is None:
                rendered.append(label('No se pudo renderizar; mira el código.', 'empty', xalign=.5, vexpand=True))
                code.set_active(True); return
            self.scrolled_picture(rendered, value)
        self.background_job(lambda: preview.html_render(hit['path']), apply)

    def preview_text(self, body, hit, language=None):
        wait = self.placeholder(body)
        stems = [stem(t) for t in (self.results_intent.terms if self.results_intent else [])]

        def apply(value):
            body.remove(wait)
            if isinstance(value, Exception):
                self.preview_other(body, hit); return
            text, truncated = value
            buffer = GtkSource.Buffer()
            manager = GtkSource.LanguageManager.get_default()
            lang = manager.get_language(language) if language else manager.guess_language(hit['path'], None)
            if lang: buffer.set_language(lang)
            buffer.set_style_scheme(self.scheme)
            buffer.set_highlight_matching_brackets(False)
            buffer.set_text(text + ('\n\n… (vista previa recortada)' if truncated else ''))
            view = GtkSource.View(buffer=buffer, editable=False, monospace=True, cursor_visible=False,
                                  left_margin=14, right_margin=14, top_margin=12, bottom_margin=12)
            prose = hit['kind'] == 'document' and Path(hit['path']).suffix.lower() not in {'.json', '.xml', '.yaml', '.yml', '.toml', '.csv', '.tsv', '.log'}
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if prose else Gtk.WrapMode.NONE)
            view.set_show_line_numbers(not prose)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(view)
            body.append(scroll)
            target = None
            if stems:
                settings = GtkSource.SearchSettings(regex_enabled=True, case_sensitive=False, wrap_around=False)
                settings.set_search_text('|'.join(accent_pattern(s) for s in stems))
                context = GtkSource.SearchContext(buffer=buffer, settings=settings)
                context.set_highlight(True)
                view.search_context = context
                found, start, _, _ = context.forward(buffer.get_start_iter())
                if found: target = start.get_line()
            if hit['kind'] == 'code' and hit['loc']: target = int(hit['loc']) - 1
            if target:
                def scroll_to():
                    found, place = buffer.get_iter_at_line(target)
                    if found: view.scroll_to_iter(place, 0.0, True, 0.0, 0.25)
                    return False
                GLib.timeout_add(60, scroll_to)
        self.background_job(lambda: preview.read_text(hit['path']), apply)

    def preview_media(self, body, hit):
        media = Gtk.MediaFile.new_for_filename(hit['path'])
        self.media = media
        start = hit['loc']
        if hit['kind'] == 'video':
            media.set_muted(True)
            video = Gtk.Video(media_stream=media, vexpand=True, autoplay=False)
            if hasattr(video, 'set_graphics_offload'): video.set_graphics_offload(Gtk.GraphicsOffloadEnabled.ENABLED)
            body.append(video)
        else:
            art = label(GLYPH['audio'], 'big-glyph', xalign=.5, vexpand=True)
            body.append(art)
            controls = Gtk.MediaControls(media_stream=media)
            controls.set_margin_start(16); controls.set_margin_end(16); controls.set_margin_bottom(14)
            body.append(controls)

        def prepared(stream, _):
            if not stream.is_prepared() or stream is not self.media: return
            if start and stream.is_seekable(): stream.seek(int(start * 1_000_000))
            if hit['kind'] == 'video': stream.play()
        media.connect('notify::prepared', prepared)

        def apply(info):
            if isinstance(info, Exception) or not info: return
            if info.get('duration'): self.add_fact(preview.clock(info['duration']))
            if info.get('resolution'): self.add_fact(info['resolution'])
            for key in ('title', 'artist', 'album'):
                if info.get(key): self.add_fact(info[key])
        self.background_job(lambda: preview.media_info(hit['path']), apply)

    def preview_folder(self, body, hit):
        wait = self.placeholder(body)

        def apply(value):
            body.remove(wait)
            if isinstance(value, Exception):
                body.append(label(f'No se puede leer la carpeta: {value}', 'empty', xalign=.5, vexpand=True)); return
            folders, files = value['folders'], value['files']
            self.add_fact(f"{folders} {'carpeta' if folders == 1 else 'carpetas'} y {files} {'archivo' if files == 1 else 'archivos'}")
            if value['bytes']: self.add_fact(preview.human_size(value['bytes']))
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            if value['images']:
                grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, min_children_per_line=3,
                                   column_spacing=6, row_spacing=6, homogeneous=True)
                grid.set_margin_start(10); grid.set_margin_end(10); grid.set_margin_top(10); grid.set_margin_bottom(6)
                for image_path in value['images']:
                    picture = Thumb(96)
                    grid.append(picture)
                    self.thumb_pool.submit(self.load_texture, image_path, picture)
                content.append(grid)
            listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['results'])
            for name, is_dir, size, mtime in value['entries']:
                kind = 'folder' if is_dir else preview.category(Path(name).suffix.lower())
                line = Gtk.Box(spacing=10, css_classes=['entry-row'])
                glyph = label('')
                glyph.set_markup(f"<span foreground=\"{self.kind_colors[kind]}\">{GLYPH[kind]}</span>")
                line.append(glyph)
                line.append(label(name, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE))
                line.append(label('' if is_dir else preview.human_size(size), 'meta'))
                line.append(label(preview.human_time(mtime), 'meta'))
                item = Gtk.ListBoxRow(child=line, activatable=True)
                item.target = str(Path(hit['path']) / name)
                listing.append(item)
            listing.set_activate_on_single_click(False)
            listing.connect('row-activated', lambda _, item: subprocess.Popen(['xdg-open', item.target]))
            content.append(listing)
            scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroll.set_child(content)
            body.append(scroll)
        self.background_job(lambda: preview.folder_listing(hit['path']), apply)

    def load_texture(self, path, picture):
        try:
            source = preview.thumbnail(path, 'image', 320)
            texture = Gdk.Texture.new_from_filename(str(source)) if source else None
        except Exception:
            return
        if texture: GLib.idle_add(lambda: (picture.set_paintable(texture), False)[1])

    def preview_archive(self, body, hit):
        wait = self.placeholder(body)

        def apply(value):
            body.remove(wait)
            if isinstance(value, Exception) or value is None:
                self.preview_other(body, hit); return
            self.add_fact(f"{value['count']} elementos")
            text = '\n'.join(f"{name}{'' if size is None else '    ' + preview.human_size(size)}" for name, size in value['entries'])
            buffer = GtkSource.Buffer(text=text)
            buffer.set_style_scheme(self.scheme)
            view = GtkSource.View(buffer=buffer, editable=False, monospace=True, left_margin=14, top_margin=12)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(view)
            body.append(scroll)
        self.background_job(lambda: preview.archive_listing(hit['path']), apply)

    def preview_other(self, body, hit):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, vexpand=True, valign=Gtk.Align.CENTER)
        art = label('', 'big-glyph', xalign=.5)
        art.set_markup(f"<span foreground=\"{self.kind_colors[hit['kind']]}\">{GLYPH[hit['kind']]}</span>")
        box.append(art)
        about = label('', 'empty', xalign=.5, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=50)
        box.append(about)
        body.append(box)
        self.background_job(lambda: preview.describe(hit['path']), lambda value: about.set_text('' if isinstance(value, Exception) else value))

    # ------------------------------------------------------------ acciones
    def open_hit(self, hit, folder=False):
        try:
            open_path(hit['path'], hit['kind'], hit['loc'], folder)
            self.hide()
        except Exception as exc:
            self.empty_preview('No se pudo abrir', str(exc))

    def copy_path(self, hit):
        self.window.get_clipboard().set_content(Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_array([Gio.File.new_for_path(hit['path'])])),
            Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8', GLib.Bytes.new(hit['path'].encode()))]))
        self.timing.set_text('Ruta copiada')

    def choose_image(self, *_):
        dialog = Gtk.FileDialog(title='Imagen de referencia', accept_label='Buscar parecidas')
        images = Gtk.FileFilter(name='Imágenes')
        images.add_pixbuf_formats()
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)

        def finished(dialog, result):
            try: file = dialog.open_finish(result)
            except GLib.Error: return
            if file and file.get_path(): self.set_image(file.get_path())
        dialog.open(self.window, None, finished)

    def dropped(self, target, value, x, y):
        files = value.get_files()
        if not files: return False
        path = files[0].get_path()
        if path and preview.category(Path(path).suffix.lower()) == 'image':
            self.set_image(path)
            return True
        return False

    def set_image(self, path):
        self.query_image = path
        if path:
            self.entry.set_text('')
            self.entry.set_placeholder_text(f'Imágenes parecidas a {Path(path).name} (Esc para quitar)')
            self.search_now()
        else:
            self.entry.set_placeholder_text('Describe lo que buscas: «documento html de pirámides», «fotos de ayer»…')
            self.changed()

    # -------------------------------------------------------------- estado
    def active_index(self):
        """El vigilante escribe su estado cada pocos segundos: funciona con o sin systemd."""
        try:
            state = json.loads((DATA / 'status.json').read_text())
            return bool(state.get('running')) and time.time() - state.get('updated', 0) < 20
        except (OSError, ValueError):
            return False

    def poll(self):
        if not self.window: return False
        if not self.window.get_visible() and self.stats: return True

        def work():
            state = json.loads((DATA / 'status.json').read_text()) if (DATA / 'status.json').exists() else {}
            with opened() as db:
                files = db.execute('SELECT count(*) FROM files WHERE error IS NULL').fetchone()[0]
                names = db.execute('SELECT count(*) FROM catalog').fetchone()[0]
            return state, files, names, self.active_index()

        def done(future):
            try: value = future.result()
            except Exception as exc:
                GLib.idle_add(lambda: (self.status.set_text(f'Estado no disponible: {exc}'), False)[1]); return
            GLib.idle_add(self.update_status, *value)
        threading.Thread(target=lambda: done(_Done(work)), daemon=True).start()
        return True

    def update_status(self, state, files, names, active):
        self.stats = {'files': files, 'names': names}
        fmt = lambda n: f'{n:,}'.replace(',', '.')
        parts = [f'{fmt(names)} elementos por nombre', f'{fmt(files)} con contenido analizado']
        for name in ('dot', 'busy', 'off'): self.dot.remove_css_class(name)
        self.dot.add_css_class('dot')
        if not active:
            self.dot.add_css_class('off')
            parts.append('actualización automática en pausa')
        elif state.get('phase') == 'scanning':
            self.dot.add_css_class('busy'); parts.append('revisando carpetas…')
        elif state.get('queued'):
            self.dot.add_css_class('busy')
            parts.append(f"{fmt(state['queued'])} en cola de análisis")
        else:
            parts.append('todo al día, vigilando cambios')
        if state.get('error'): parts.append(state['error'])
        self.status.set_text(', '.join(parts))
        return False

    # ------------------------------------------------------------- ajustes
    def settings(self, *_):
        config = load()
        dialog = Gtk.Window(title='Ajustes de Cati', transient_for=self.window, modal=True, decorated=False)
        dialog.add_css_class('cati-dialog')
        dialog.set_default_size(620, 480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=['panel'])
        dialog.set_child(box)
        box.append(label(f'{COG}  Carpetas y ajustes', 'heading'))
        whole = Gtk.CheckButton(label='Todo mi equipo: carpeta personal y discos de datos')
        whole.set_active(config.get('scope') == 'computer')
        box.append(whole)
        roots = list(config['folders'])
        root_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(root_list)

        def refresh():
            self.clear(root_list)
            for root in roots:
                line = Gtk.Box(spacing=8, css_classes=['root'])
                line.append(label(f"{GLYPH['folder']}  {short_path(root)}", hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE))
                line.append(button('Quitar', 'action', lambda _, value=root: (roots.remove(value), refresh())))
                root_list.append(line)
        refresh()

        def add(*_):
            chooser = Gtk.FileDialog(title='Añadir carpeta', accept_label='Añadir')
            def finished(chooser, result):
                try: folder = chooser.select_folder_finish(result)
                except GLib.Error: return
                path = folder.get_path() if folder else None
                if path and path not in roots: roots.append(path); refresh()
            chooser.select_folder(dialog, None, finished)
        box.append(button('Añadir carpeta…', 'action', add))
        code = Gtk.CheckButton(label='Analizar código fuente (búscalo con «code:»)')
        code.set_active(config['code'])
        box.append(code)
        media = Gtk.CheckButton(label='Analizar imágenes, audio y vídeo')
        media.set_active(config['media'])
        box.append(media)
        hint = label('Todo se procesa en este equipo. Se excluyen carpetas ocultas, dependencias y archivos de credenciales.', 'hint', wrap=True)
        box.append(hint)
        problem = label('', 'problem', wrap=True)
        box.append(problem)
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.END, vexpand=True, valign=Gtk.Align.END)
        footer.append(button('Cancelar', 'action', lambda *_: dialog.destroy()))
        toggle = button('Pausar análisis' if self.active_index() else 'Reanudar análisis', 'action', lambda *_: (self.toggle_index(), dialog.destroy()))
        footer.append(toggle)

        def apply(*_):
            if not roots and not whole.get_active(): problem.set_text('Añade al menos una carpeta.'); return
            config.update(folders=roots, code=code.get_active(), media=media.get_active(), scope='computer' if whole.get_active() else 'folders')
            save(config)
            from .service import restart_all
            restart_all()
            dialog.destroy()
        footer.append(button('Guardar', 'action primary', apply))
        box.append(footer)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', lambda c, k, *_: (dialog.destroy(), True)[1] if k == Gdk.KEY_Escape else False)
        dialog.add_controller(keys)
        dialog.present()

    def toggle_index(self):
        from .service import start_watch, stop_watch
        stop_watch() if self.active_index() else start_watch()


class Page(Gtk.Widget):
    """Página ajustada al ancho que conserva su proporción y se desplaza en vertical."""
    def __init__(self, texture):
        super().__init__()
        self.texture = texture
        self.add_css_class('page')

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        width, height = self.texture.get_width(), self.texture.get_height()
        if orientation == Gtk.Orientation.HORIZONTAL: return 0, min(width, 900), -1, -1
        size = int((for_size if for_size > 0 else width) * height / width)
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        width = self.get_width()
        height = width * self.texture.get_height() / self.texture.get_width()
        snapshot.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR, Graphene.Rect().init(0, 0, width, height))


class Thumb(Gtk.Widget):
    """Miniatura cuadrada de tamaño fijo: recorta al centro sin deformar ni ensanchar la fila."""
    def __init__(self, size):
        super().__init__()
        self.size = size
        self.texture = None
        self.add_css_class('thumb')

    def set_paintable(self, texture):
        self.texture = texture
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        return self.size, self.size, -1, -1

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if not self.texture: return
        tw, th = self.texture.get_width(), self.texture.get_height()
        scale = max(width / tw, height / th)
        w, h = tw * scale, th * scale
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        snapshot.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR,
                                       Graphene.Rect().init((width - w) / 2, (height - h) / 2, w, h))
        snapshot.pop()


class _Done:
    """Adaptador mínimo de futuro para trabajos en hilos sueltos."""
    def __init__(self, work):
        try: self.value, self.error = work(), None
        except Exception as exc: self.value, self.error = None, exc

    def result(self):
        if self.error: raise self.error
        return self.value


def run(background=False):
    Cati(background).run(None)
