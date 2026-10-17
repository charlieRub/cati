"""Explorador de archivos sobre la búsqueda: carpetas, filtros, búsquedas guardadas, selección
múltiple con acciones en lote, duplicados y uso del espacio.

`Explorer` es una mezcla de `Cati`: usa su lista de resultados, su vista previa y su estado.
Una carpeta (`scope`) y una vista (`view`) deciden qué se muestra cuando la caja está vacía;
si escribes, la misma carpeta y los mismos filtros acotan la búsqueda.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from . import batch, browse, disk, preview, saved, usage
from .config import load, save
from .widgets import button, label, short_path

SEARCH_G, FOLDER_G, HOME_G, DRIVE_G = '\U000F0349', '\U000F024B', '\U000F02DC', '\U000F02CA'
CLOCK_G, HISTORY_G, FIRE_G, DUPES_G, CHART_G = '\U000F0150', '\U000F02DA', '\U000F0238', '\U000F01BC', '\U000F0129'
STAR_G, STAR_OFF_G, BOOKMARK_G = '\U000F04CE', '\U000F04D2', '\U000F00C3'
UP_G, CLOSE_G, MENU_G, CHEVRON_G = '\U000F005D', '\U000F0156', '\U000F035C', '\U000F0142'
BACK_G, FORWARD_G = '\U000F0141', '\U000F0142'
EXPAND_G, COLLAPSE_G = '\U000F0293', '\U000F0294'
MARK_ON, MARK_OFF = '\U000F0133', '\U000F0130'
MOVE_G, COPY_G, ZIP_G, RENAME_G, TRASH_G, UNDO_G = '\U000F0255', '\U000F018F', '\U000F05C4', '\U000F0CB6', '\U000F0A7A', '\U000F054C'

VIEWS = {'opened': ('Abiertos hace poco', HISTORY_G), 'frequent': ('Más usados', FIRE_G),
         'dups': ('Duplicados', DUPES_G), 'space': ('Espacio en disco', CHART_G)}
EMPTY = {'folder': ('Carpeta vacía', 'No hay elementos visibles aquí.'),
         'opened': ('Todavía no hay historial', 'Aquí aparecerán los archivos que abras desde Cati.'),
         'frequent': ('Todavía no hay historial', 'Aquí aparecerán los archivos que más abras desde Cati.'),
         'dups': ('Sin duplicados', 'No hay dos archivos idénticos de más de 4 KB en esta zona.'),
         'space': ('Sin datos de espacio', 'Esta carpeta está vacía o no se puede leer.')}
SORT_LABEL = browse.SORTS
TITLES = {'explorer': 'Cati', 'search': 'Cati · Buscar'}


def plural(count, one, many): return f'{count} {one if count == 1 else many}'


def explorer_css(c):
    return f"""
    .sidebar {{ background: {c['darker_background']}; border-right: 1px solid {c['color8']}; padding: 8px 0 8px 0; }}
    .side-head {{ color: {c['muted']}; font-size: 11px; letter-spacing: 1px; padding: 14px 18px 4px 18px; }}
    button.side-item {{ padding: 6px 14px 6px 16px; border-left: 3px solid transparent; color: {c['foreground']}; font-size: 13px; }}
    button.side-item:hover {{ background: {c['lighter_background']}; }}
    button.side-item.current {{ background: {c['selection']}; border-left-color: {c['accent']}; color: {c['accent']}; }}
    .side-glyph {{ color: {c['muted']}; font-size: 15px; min-width: 18px; }}
    button.side-item.current .side-glyph {{ color: {c['accent']}; }}
    button.side-remove {{ color: {c['muted']}; padding: 6px 10px; opacity: 0; }}
    box.side-row:hover button.side-remove {{ opacity: 1; }}
    button.side-remove:hover {{ color: {c['red']}; }}
    .side-note {{ color: {c['muted']}; font-size: 12px; padding: 4px 18px; }}

    .refine-bar {{ padding: 6px 20px; border-bottom: 1px solid {c['color8']}; background: {c['background']}; }}
    menubutton.refine > button, button.refine {{ all: unset; padding: 3px 10px; margin-right: 8px; color: {c['muted']};
      border: 1px solid {c['color8']}; font-size: 12px; }}
    menubutton.refine > button:hover, button.refine:hover {{ color: {c['foreground']}; }}
    menubutton.refine.on > button, button.refine.on {{ color: {c['accent']}; border-color: {c['accent']}; background: alpha({c['accent']}, .08); }}
    button.refine.star {{ color: {c['yellow']}; border-color: alpha({c['yellow']}, .5); }}
    button.refine.star:hover {{ background: alpha({c['yellow']}, .10); }}
    button.tool.flat {{ margin-left: 0; margin-right: 6px; font-size: 15px; padding: 2px 8px; }}
    popover.choice contents {{ background: {c['lighter_background']}; border: 1px solid {c['color8']}; border-radius: 0; padding: 4px 0; }}
    button.pop-item {{ all: unset; padding: 6px 16px; color: {c['foreground']}; font-size: 12px; min-width: 150px; }}
    button.pop-item:hover {{ background: {c['selection']}; }}
    button.pop-item.current {{ color: {c['accent']}; }}

    .crumbs {{ padding: 4px 14px; border-bottom: 1px solid {c['color8']}; background: {c['darker_background']}; }}
    button.crumb {{ all: unset; padding: 3px 7px; color: {c['muted']}; font-size: 13px; }}
    button.crumb:hover {{ color: {c['accent']}; background: {c['lighter_background']}; }}
    button.crumb.here {{ color: {c['foreground']}; font-weight: bold; }}
    .crumb-sep {{ color: {c['color8']}; font-size: 12px; }}
    button.nav {{ all: unset; padding: 3px 8px; color: {c['muted']}; font-size: 15px; }}
    button.nav:hover {{ color: {c['accent']}; }}
    button.nav:disabled {{ opacity: .3; }}

    .results > row.group-head {{ padding: 10px 16px 4px 17px; border-left: 3px solid transparent; background: transparent; }}
    .group-title {{ color: {c['yellow']}; font-size: 12px; }}
    .marker {{ color: {c['muted']}; font-size: 16px; opacity: 0; min-width: 22px; margin-right: 2px; }}
    .results > row:hover .marker {{ opacity: .55; }}
    .results.selecting .marker {{ opacity: .7; }}
    .results > row.marked .marker {{ color: {c['accent']}; opacity: 1; }}
    .results > row.marked {{ background: alpha({c['accent']}, .07); }}
    levelbar.usage trough {{ min-height: 4px; background: {c['lighter_background']}; }}
    levelbar.usage block.filled {{ background: {c['accent']}; min-height: 4px; }}

    .batch {{ padding: 8px 14px; border-top: 1px solid {c['color8']}; background: {c['lighter_background']}; }}
    .batch-info {{ color: {c['accent']}; font-size: 12px; font-weight: bold; }}
    .batch button.action {{ margin-right: 6px; margin-left: 0; padding: 3px 9px; }}
    .batch button.action.danger:hover {{ border-color: {c['red']}; color: {c['red']}; }}

    .cati-dialog entry {{ background: {c['darker_background']}; color: {c['foreground']}; border: 1px solid {c['color8']};
      padding: 6px 8px; min-height: 26px; caret-color: {c['accent']}; }}
    .cati-dialog entry:focus {{ border-color: {c['accent']}; }}
    .cati-dialog .plan {{ background: {c['darker_background']}; }}
    .cati-dialog .plan > row {{ padding: 3px 10px; }}
    .cati-dialog .bad {{ color: {c['red']}; }}
    .cati-dialog .skip {{ color: {c['muted']}; }}
    .cati-dialog button.chip {{ padding: 3px 10px; margin-right: 6px; color: {c['muted']}; border: 1px solid {c['color8']}; font-size: 12px; }}
    .cati-dialog button.chip:checked {{ color: {c['accent']}; border-color: {c['accent']}; background: alpha({c['accent']}, .08); }}
    """


class Explorer:
    # ------------------------------------------------------------------ estado
    def init_explorer(self):
        self.mode = 'explorer'       # explorer | search (ver set_mode)
        self.scope = None            # carpeta en la que se navega y se busca
        self.view = None             # None | opened | frequent | dups | space
        self.heading = None          # título de las vistas que no son una búsqueda
        self.refine = {'period': None, 'size': None}
        self.refiners = {}
        self.marked = {}             # ruta → resultado, en el orden en que se marcaron
        self.anchor = None
        self.trail, self.trail_next = [], []
        self.sort, self.hidden = 'name', False
        self.dups_cache, self.dup_groups = {}, []
        self.view_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cati-view')
        for name, callback in (('rename', self.rename), ('move', self.move_to), ('trash', self.trash),
                               ('copy', self.copy_to), ('zip', self.compress)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_, call=callback: call())
            self.add_action(action)

    # ----------------------------------------------------------------- piezas
    def build_sidebar(self, body):
        self.sidebar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT, transition_duration=140,
                                    reveal_child=bool(load().get('sidebar', True)))
        self.sidebar.set_hexpand(False)   # sus botones se expanden: sin esto se come el ancho sobrante a pantalla completa
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=['sidebar'])
        column.set_size_request(216, -1)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(self.side)
        column.append(scroll)
        self.sidebar.set_child(column)
        body.append(self.sidebar)
        self.side_items = {}
        self.fill_sidebar()

    def fill_sidebar(self):
        self.clear(self.side)
        self.side_items = {}
        self.side_places = {}

        def item(key, glyph, text, callback, tooltip=None, remove=None):
            content = Gtk.Box(spacing=10)
            content.append(label(glyph, 'side-glyph'))
            content.append(label(text, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            entry = button(content, 'side-item', lambda *_: callback(), tooltip)
            entry.set_hexpand(True)
            self.side_items[key] = entry
            if not remove:
                self.side.append(entry)
                return
            row = Gtk.Box(css_classes=['side-row'])
            row.append(entry)
            row.append(button(CLOSE_G, 'side-remove', lambda *_: remove(), 'Quitar'))
            self.side.append(row)

        item(('view', 'modified'), SEARCH_G, 'Inicio', lambda: self.navigate(None), 'Buscar en todo el equipo')
        self.side.append(label('LUGARES', 'side-head'))
        for place in browse.places(load()):
            glyph = {'home': HOME_G, 'drive': DRIVE_G}.get(place['icon'], FOLDER_G)
            self.side_places[place['path']] = place
            item(('place', place['path']), glyph, place['label'], lambda p=place['path']: self.navigate(p), short_path(place['path']))
        self.side.append(label('VISTAS', 'side-head'))
        for name, (text, glyph) in VIEWS.items():
            item(('view', name), glyph, text, lambda n=name: self.navigate(self.scope if n in {'dups', 'space'} else None, n))
        self.side.append(label('BÚSQUEDAS GUARDADAS', 'side-head'))
        entries = saved.load()
        for entry in entries:
            item(('saved', entry['id']), BOOKMARK_G, entry['name'], lambda e=entry: self.apply_saved(e),
                 entry['query'] or 'Solo filtros', remove=lambda e=entry: (saved.remove(e['id']), self.fill_sidebar()))
        if not entries: self.side.append(label('Guarda una búsqueda con ★ y vuelve a ella en un clic.', 'side-note', wrap=True))
        self.mark_sidebar()

    def mark_sidebar(self):
        key = ('view', 'modified')
        if self.view: key = ('view', self.view)
        elif self.scope:
            near = [p for p in self.side_places if self.scope == p or self.scope.startswith(p.rstrip('/') + '/')]
            key = ('place', max(near, key=len)) if near else None
        for name, widget in self.side_items.items():
            (widget.add_css_class if name == key else widget.remove_css_class)('current')

    def toggle_sidebar(self):
        shown = not self.sidebar.get_reveal_child()
        self.sidebar.set_reveal_child(shown)
        if self.mode == 'explorer':   # en el modo búsqueda se puede asomar sin cambiar la preferencia
            config = load(); config['sidebar'] = shown; save(config)

    def set_mode(self, mode):
        """«explorer»: ventana maximizada con barra lateral; «search»: ventana flotante solo para buscar.

        El título distingue los modos para las reglas del gestor de ventanas (Hyprland, en Omarchy, no
        deja que las aplicaciones se maximicen solas); en el resto de escritorios se pide con GTK.
        """
        self.mode = mode
        explorer = mode == 'explorer'
        self.window.set_title(TITLES[mode])
        self.sidebar.set_reveal_child(explorer and bool(load().get('sidebar', True)))
        self.refine_bar.set_visible(explorer)
        if not explorer and (self.scope or self.view or self.refine_active()):
            self.refine = {'period': None, 'size': None}
            self.sync_refine()
            self.navigate(None)
        if explorer: self.window.maximize()
        else: self.window.unmaximize()
        self.mode_button.set_label(COLLAPSE_G if explorer else EXPAND_G)
        self.mode_button.set_tooltip_text('Pasar a búsqueda rápida (F11)' if explorer else 'Abrir el explorador a pantalla completa (F11)')

    def toggle_mode(self):
        self.open_mode('search' if self.mode == 'explorer' else 'explorer')

    def build_refine(self, main):
        bar = Gtk.Box(css_classes=['refine-bar'])
        bar.append(button(MENU_G, 'tool flat', lambda *_: self.toggle_sidebar(), 'Mostrar u ocultar la barra lateral (Ctrl+B)'))
        bar.append(self.choice('period', 'Fecha', browse.PERIOD_LABEL))
        bar.append(self.choice('size', 'Tamaño', browse.SIZE_LABEL))
        bar.append(Gtk.Box(hexpand=True))
        self.save_button = button(f'{STAR_OFF_G}  Guardar búsqueda', 'refine star', self.save_search, 'Guarda esta búsqueda y sus filtros (Ctrl+S)')
        bar.append(self.save_button)
        self.refine_bar = bar
        main.append(bar)

    def choice(self, key, title, options):
        popover = Gtk.Popover(css_classes=['choice'], has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover.set_child(box)
        text = label(title)
        menu = Gtk.MenuButton(popover=popover, css_classes=['refine'])
        menu.set_child(text)
        items = {}

        def apply():
            value = self.refine[key]
            text.set_text(f'{title}: {options[value]}' if value else title)
            (menu.add_css_class if value else menu.remove_css_class)('on')
            for option, widget in items.items(): (widget.add_css_class if option == value else widget.remove_css_class)('current')

        def pick(value):
            self.refine[key] = value
            apply()
            popover.popdown()
            self.refine_changed()
        for value, name in [(None, 'Cualquiera'), *options.items()]:
            items[value] = button(name, 'pop-item', lambda *_, v=value: pick(v))
            box.append(items[value])
        self.refiners[key] = apply
        apply()
        return menu

    def sync_refine(self):
        for apply in self.refiners.values(): apply()

    def build_crumbs(self, main):
        self.crumb_bar = Gtk.Box(css_classes=['crumbs'], visible=False)
        self.nav_back = button(BACK_G, 'nav', lambda *_: self.back(), 'Atrás (Alt+←)')
        self.nav_forward = button(FORWARD_G, 'nav', lambda *_: self.forward(), 'Adelante (Alt+→)')
        self.nav_up = button(UP_G, 'nav', lambda *_: self.parent(), 'Carpeta superior (Alt+↑ o Retroceso)')
        for widget in (self.nav_back, self.nav_forward, self.nav_up): self.crumb_bar.append(widget)
        self.crumb_path = Gtk.Box()
        self.crumb_scroll = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                               hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.crumb_scroll.set_child(self.crumb_path)
        self.crumb_bar.append(self.crumb_scroll)
        sort = Gtk.Popover(css_classes=['choice'], has_arrow=False)
        sort_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sort.set_child(sort_box)
        self.sort_text = label('Orden: Nombre')
        self.sort_button = Gtk.MenuButton(popover=sort, css_classes=['refine'])
        self.sort_button.set_child(self.sort_text)
        for key, name in SORT_LABEL.items():
            sort_box.append(button(name, 'pop-item', lambda *_, k=key: (setattr(self, 'sort', k), self.sort_text.set_text(f'Orden: {SORT_LABEL[k]}'),
                                                                    sort.popdown(), self.refresh_view())))
        self.crumb_bar.append(self.sort_button)
        self.hidden_button = button('Ocultos', 'refine', lambda b: (setattr(self, 'hidden', b.get_active()), self.refresh_view()),
                                    'Mostrar archivos y carpetas ocultos', toggle=True)
        self.crumb_bar.append(self.hidden_button)
        self.crumb_bar.append(button(f'{CLOSE_G}  Salir', 'refine', lambda *_: self.navigate(None), 'Volver a buscar en todo el equipo'))
        main.append(self.crumb_bar)

    def build_batch(self, left):
        self.batch_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=['batch'], visible=False)
        top = Gtk.Box()
        self.batch_info = label('', 'batch-info', hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        top.append(self.batch_info)
        self.clear_button = button(CLOSE_G, 'action', lambda *_: self.clear_marks(), 'Quitar la selección (Esc)')
        top.append(self.clear_button)
        self.batch_bar.append(top)
        self.spare_button = button('Marcar copias sobrantes', 'action primary', lambda *_: self.mark_spares(),
                                   'Marca todas menos la más antigua de cada grupo')
        self.spare_button.set_halign(Gtk.Align.START)
        self.batch_bar.append(self.spare_button)
        actions = Gtk.Box()
        self.batch_actions = [actions]
        for glyph, text, call, tip, css in ((MOVE_G, 'Mover', self.move_to, 'Mover a otra carpeta (Ctrl+M)', ''),
                                            (COPY_G, 'Copiar', self.copy_to, 'Copiar a otra carpeta (Ctrl+Mayús+M)', ''),
                                            (ZIP_G, 'ZIP', self.compress, 'Comprimir en un ZIP (Ctrl+E)', ''),
                                            (RENAME_G, 'Renombrar', self.rename, 'Renombrar (F2)', ''),
                                            (TRASH_G, 'Papelera', self.trash, 'Enviar a la papelera (Ctrl+Supr)', 'danger')):
            actions.append(button(f'{glyph}  {text}', f'action {css}'.strip(), lambda *_, c=call: c(), tip))
        self.batch_bar.append(actions)
        left.append(self.batch_bar)

    def explorer_ready(self):
        """Tras construir la ventana: recuerda el texto de ayuda de la caja de búsqueda."""
        self.default_placeholder = self.entry.get_placeholder_text()

    # ------------------------------------------------------------ navegación
    def navigate(self, scope, view=None, push=True):
        if view in {'space'} and not scope: scope = str(Path.home())
        if view in {'opened', 'frequent'}: scope = None
        state = (self.scope, self.view)
        if push and state != (scope, view):
            self.trail.append(state)
            del self.trail[:-60]
            self.trail_next.clear()
        self.scope, self.view = scope, view
        self.marked.clear()
        self.category = 'all'
        self.generation += 1
        if self.entry.get_text(): self.entry.set_text('')   # vaciar la caja dispara changed → refresh_view
        else: self.refresh_view()
        self.entry.grab_focus()

    def back(self):
        if not self.trail: return
        self.trail_next.append((self.scope, self.view))
        scope, view = self.trail.pop()
        self.navigate(scope, view, push=False)

    def forward(self):
        if not self.trail_next: return
        self.trail.append((self.scope, self.view))
        scope, view = self.trail_next.pop()
        self.navigate(scope, view, push=False)

    def parent(self):
        if not self.scope or self.scope == '/': return
        self.navigate(str(Path(self.scope).parent), self.view if self.view in {'space', 'dups'} else None)

    def refine_active(self):
        return bool(self.refine['period'] or self.refine['size'])

    def query_filters(self):
        filters = {'folder': self.scope, 'period': self.refine['period'], 'size': self.refine['size']}
        return {k: v for k, v in filters.items() if v}

    def refine_changed(self):
        self.marked.clear()
        if self.entry.get_text().strip() or self.query_image: self.search_now()
        else: self.refresh_view()

    def refresh_view(self):
        """Qué mostrar con la caja vacía (o tras un cambio): carpeta, vista, filtros o lo más reciente."""
        self.update_chrome()
        if self.entry.get_text().strip() or self.query_image:
            self.search_now(); return
        if self.view in {'opened', 'frequent'}: self.show_used()
        elif self.view == 'dups': self.start_dups()
        elif self.view == 'space': self.start_space()
        elif self.refine_active(): self.search_now()
        elif self.scope: self.show_folder()
        else: self.show_recent()

    def update_chrome(self):
        self.update_crumbs()
        self.mark_sidebar()
        self.update_batch()
        name = Path(self.scope).name or self.scope if self.scope else None
        self.entry.set_placeholder_text(f'Buscar en «{name}»…' if name and self.view != 'space' else self.default_placeholder)
        self.nav_back.set_sensitive(bool(self.trail))
        self.nav_forward.set_sensitive(bool(self.trail_next))
        self.nav_up.set_sensitive(bool(self.scope) and self.scope != '/')
        listing = self.scope and not self.view
        self.sort_button.set_visible(bool(listing))
        self.hidden_button.set_visible(bool(listing) or self.view == 'space')
        searching = bool(self.entry.get_text().strip() or self.query_image)
        self.save_button.set_visible(searching or self.refine_active() or bool(self.scope and not self.view))

    def update_crumbs(self):
        self.crumb_bar.set_visible(bool(self.scope))
        self.clear(self.crumb_path)
        if not self.scope: return
        trail = browse.crumbs(self.scope)
        for index, (text, path) in enumerate(trail):
            if index: self.crumb_path.append(label(CHEVRON_G, 'crumb-sep'))
            crumb = button(text, 'crumb here' if index == len(trail) - 1 else 'crumb',
                           lambda *_, p=path: self.navigate(p, self.view if self.view in {'space', 'dups'} else None), path)
            self.crumb_path.append(crumb)
        adjustment = self.crumb_scroll.get_hadjustment()
        GLib.idle_add(lambda: (adjustment.set_value(adjustment.get_upper()), False)[1])

    # --------------------------------------------------------------- vistas
    def off_thread(self, work, done, generation):
        def run():
            try: value = work()
            except Exception as exc: value = exc
            GLib.idle_add(done, value, generation)
        threading.Thread(target=run, daemon=True, name='cati-view').start()

    def render_view(self, hits, heading, generation, chips=True):
        if generation != self.generation: return False
        self.busy(False)
        self.results_intent = None
        self.hits = hits
        self.heading = heading
        self.filters.set_visible((bool(chips) and bool(hits)) or self.undo is not None)
        counts = {}
        for hit in hits: counts[hit['kind']] = counts.get(hit['kind'], 0) + 1
        if not chips or self.category not in counts: self.category = 'all'
        self.build_chips(hits if chips else [])
        self.clear(self.tags)
        if self.undo is not None: self.tags.append(button(f'{UNDO_G}  Deshacer (Ctrl+Z)', 'undo', lambda *_: self.undo_action()))
        self.timing.set_text('')
        self.apply_category()
        return False

    def build_chips(self, hits):
        from .gui import GLYPH, LABEL, ORDER  # tipos de archivo: mismos glifos y nombres que la búsqueda
        counts = {}
        for hit in hits: counts[hit['kind']] = counts.get(hit['kind'], 0) + 1
        self.clear(self.chips)
        for key in ORDER:
            amount = len(hits) if key == 'all' else counts.get(key)
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

    def show_folder(self):
        self.generation += 1
        folder, sort, hidden = self.scope, self.sort, self.hidden
        self.busy(False)

        def work(): return browse.listing(folder, sort, hidden)

        def done(value, generation):
            if generation != self.generation: return False
            if isinstance(value, Exception):
                self.hits = []
                self.heading = None
                self.fill([], '', [])
                self.empty_preview('No se puede abrir esta carpeta', str(value))
                return False
            name = (Path(folder).name or '/').upper()
            self.render_view(value, f'{name} · {plural(len(value), "ELEMENTO", "ELEMENTOS")}', generation)
            self.empty_note = EMPTY['folder']
            return False
        self.off_thread(work, done, self.generation)

    def show_used(self):
        self.generation += 1
        view = self.view

        def done(value, generation):
            if isinstance(value, Exception): self.problem(str(value)); return False
            self.empty_note = EMPTY[view]
            return self.render_view(value, 'ABIERTOS HACE POCO' if view == 'opened' else 'MÁS USADOS', generation)
        self.off_thread(lambda: browse.used_hits('recent' if view == 'opened' else 'frequent'), done, self.generation)

    def start_space(self):
        self.generation += 1
        folder, hidden = self.scope, self.hidden
        self.busy(True)
        self.timing.set_text('Midiendo…')

        def work():
            rows, total = disk.usage(folder, hidden=hidden)
            hits = []
            for path, is_dir, size, count in rows[:300]:
                share = size / total if total else 0
                hits.append(browse.make_hit(path, [f'{share * 100:.0f} %' if share >= .005 else '< 1 %'], kind='folder' if is_dir else None,
                                            size=size, extra={'bar': share, 'files': count}))
            return hits, total

        def done(value, generation):
            if generation != self.generation: return False
            if isinstance(value, Exception): self.problem(str(value), generation); return False
            hits, total = value
            self.empty_note = EMPTY['space']
            name = (Path(folder).name or '/').upper()
            self.render_view(hits, f'ESPACIO EN {name} · {preview.human_size(total)}', generation, chips=False)
            self.timing.set_text('medido con el índice de Cati')
            self.filters.set_visible(True)
            return False
        self.off_thread(work, done, self.generation)

    def start_dups(self):
        self.generation += 1
        scope = self.scope
        cached = self.dups_cache.get(scope)
        if cached and time.monotonic() - cached[0] < 300:
            self.render_dups(cached[1], self.generation); return
        generation = self.generation
        self.busy(True)
        self.filters.set_visible(True)
        self.clear(self.tags)
        self.section.set_text('BUSCANDO DUPLICADOS…')
        self.clear(self.results)
        self.clear(self.chips)
        self.hits = []
        self.current = None
        self.empty_preview('Buscando duplicados', 'Comparo tamaños y, solo si coinciden, el contenido: no leo lo que ya es distinto.')

        def progress(done, total):
            GLib.idle_add(lambda: (self.timing.set_text(f'{done} de {total} tamaños') if generation == self.generation else None, False)[1])

        def work(): return disk.duplicates(folder=scope, stop=lambda: generation != self.generation, progress=progress)

        def done(value, generation_):
            if generation_ != self.generation: return False
            if isinstance(value, Exception): self.problem(str(value), generation_); return False
            self.dups_cache[scope] = (time.monotonic(), value)
            self.render_dups(value, generation_)
            return False
        self.off_thread(work, done, generation)

    def render_dups(self, groups, generation):
        self.dup_groups = groups
        hits = []
        for number, group in enumerate(groups[:100], 1):
            copies = len(group['paths'])
            hits.append({'header': f"{plural(copies, 'copia', 'copias')} · {preview.human_size(group['size'])} cada una · "
                                   f"{preview.human_size(group['size'] * (copies - 1))} recuperables",
                         'path': f'#{number}', 'kind': 'header', 'name': ''})
            for index, path in enumerate(group['paths']):
                hits.append(browse.make_hit(path, ['la más antigua' if index == 0 else 'copia'], size=group['size']))
        total = disk.reclaimable(groups)
        self.empty_note = EMPTY['dups']
        title = f'DUPLICADOS · {plural(len(groups), "GRUPO", "GRUPOS")} · {preview.human_size(total)} RECUPERABLES' if groups else 'DUPLICADOS'
        self.render_view(hits, title, generation, chips=False)
        self.update_batch()

    def mark_spares(self):
        real = {h['path']: h for h in self.hits if 'header' not in h}
        for group in self.dup_groups[:100]:
            for path in group['paths'][1:]:
                if path in real: self.marked[path] = real[path]
        self.paint_marks()

    # --------------------------------------------------------- filas y marcas
    def decorate_row(self, row, layout, hit):
        marker = label(MARK_OFF, 'marker', valign=Gtk.Align.CENTER)
        layout.prepend(marker)
        row.marker = marker
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect('pressed', lambda gesture, *_: self.row_pressed(gesture, hit))
        row.add_controller(click)
        toggle = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        toggle.connect('pressed', lambda gesture, *_: (self.toggle_mark(hit), gesture.set_state(Gtk.EventSequenceState.CLAIMED)))
        marker.add_controller(toggle)
        if hit['path'] in self.marked:
            marker.set_text(MARK_ON)
            row.add_css_class('marked')

    def row_pressed(self, gesture, hit):
        state = gesture.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.toggle_mark(hit)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        elif state & Gdk.ModifierType.SHIFT_MASK:
            self.mark_range(hit)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def header_row(self, hit):
        row = Gtk.ListBoxRow(selectable=False, activatable=False, css_classes=['group-head'])
        row.set_child(label(hit['header'], 'group-title', ellipsize=Pango.EllipsizeMode.END))
        return row

    def real_rows(self):
        rows, child = [], self.results.get_first_child()
        while child:
            if getattr(child, 'hit', None): rows.append(child)
            child = child.get_next_sibling()
        return rows

    def toggle_mark(self, hit, on=None):
        if not hit or 'header' in hit: return
        path = hit['path']
        if on is None: on = path not in self.marked
        if on: self.marked[path] = hit
        else: self.marked.pop(path, None)
        self.anchor = path
        self.paint_marks()

    def mark_range(self, hit):
        rows = self.real_rows()
        paths = [r.hit['path'] for r in rows]
        start = self.anchor if self.anchor in paths else (self.current['path'] if self.current and self.current['path'] in paths else hit['path'])
        a, b = sorted((paths.index(start), paths.index(hit['path'])))
        for row in rows[a:b + 1]: self.marked[row.hit['path']] = row.hit
        self.paint_marks()

    def mark_all(self):
        for row in self.real_rows(): self.marked[row.hit['path']] = row.hit
        self.paint_marks()

    def clear_marks(self):
        self.marked.clear()
        self.paint_marks()

    def paint_marks(self):
        for row in self.real_rows():
            on = row.hit['path'] in self.marked
            row.marker.set_text(MARK_ON if on else MARK_OFF)
            (row.add_css_class if on else row.remove_css_class)('marked')
        (self.results.add_css_class if self.marked else self.results.remove_css_class)('selecting')
        self.update_batch()

    def update_batch(self):
        count = len(self.marked)
        spare = self.view == 'dups' and bool(self.dup_groups) and not count
        self.batch_bar.set_visible(bool(count) or spare)
        self.spare_button.set_visible(spare)
        for widget in self.batch_actions + [self.clear_button]: widget.set_visible(bool(count))
        if count:
            size = sum(h['size'] for h in self.marked.values() if h['kind'] != 'folder')
            self.batch_info.set_text(f'{plural(count, "seleccionado", "seleccionados")}' + (f' · {preview.human_size(size)}' if size else ''))
        elif spare:
            self.batch_info.set_text(f'{preview.human_size(disk.reclaimable(self.dup_groups))} recuperables')

    def targets(self):
        """Lo marcado o, si no hay nada marcado, el resultado seleccionado."""
        if self.marked: return list(self.marked.values())
        return [self.current] if self.current and 'header' not in self.current else []

    def drag_files(self, hit):
        """Arrastrar una fila marcada lleva toda la selección; si no, solo ese archivo."""
        paths = list(self.marked) if hit['path'] in self.marked else [hit['path']]
        return Gdk.FileList.new_from_array([Gio.File.new_for_path(p) for p in paths])

    def copy_paths(self, hits):
        files = Gdk.FileList.new_from_array([Gio.File.new_for_path(h['path']) for h in hits])
        text = '\n'.join(h['path'] for h in hits)
        self.window.get_clipboard().set_content(Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_value(files),
            Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8', GLib.Bytes.new(text.encode()))]))
        self.timing.set_text('Ruta copiada' if len(hits) == 1 else f'{len(hits)} rutas copiadas')

    # -------------------------------------------------- búsquedas guardadas
    def save_search(self, *_):
        query = self.entry.get_text().strip()
        filters = self.query_filters()
        kind = self.category if self.category != 'all' else None
        if not (query or filters or kind):
            self.timing.set_text('Escribe algo o aplica un filtro para guardar la búsqueda'); return
        dialog = self.dialog('Guardar búsqueda', 460)
        box = dialog.get_child()
        box.append(label('Nombre', 'hint'))
        entry = Gtk.Entry(text=saved.suggest_name(query, {**filters, 'kind': kind}))
        box.append(entry)
        summary = [f'«{query}»'] if query else []
        if filters.get('folder'): summary.append('en ' + short_path(filters['folder']))
        if filters.get('period'): summary.append(browse.PERIOD_LABEL[filters['period']].lower())
        if filters.get('size'): summary.append(browse.SIZE_LABEL[filters['size']].lower())
        box.append(label(' · '.join(summary) or 'Solo filtros', 'hint', wrap=True))

        def apply(*_):
            saved.add(entry.get_text(), query, {**filters, 'kind': kind})
            dialog.destroy()
            self.fill_sidebar()
            self.timing.set_text('Búsqueda guardada en la barra lateral')
        entry.connect('activate', apply)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        row.append(button('Cancelar', 'action', lambda *_: dialog.destroy()))
        row.append(button('Guardar', 'action primary', apply))
        box.append(row)
        dialog.present()
        entry.grab_focus()
        entry.select_region(0, -1)

    def apply_saved(self, entry):
        filters = entry.get('filters', {})
        self.trail.append((self.scope, self.view))
        self.trail_next.clear()
        self.view = None
        self.scope = filters.get('folder') if filters.get('folder') and Path(filters['folder']).is_dir() else None
        self.refine.update(period=filters.get('period'), size=filters.get('size'))
        self.sync_refine()
        self.category = filters.get('kind') or 'all'
        self.marked.clear()
        self.update_chrome()
        if self.entry.get_text().strip() == entry['query']:
            self.generation += 1
            self.search_now()
        else:
            self.entry.set_text(entry['query'])
        if not entry['query']: self.refresh_view()
        self.entry.grab_focus()

    # -------------------------------------------------------- diálogos y lotes
    def dialog(self, title, width=520):
        dialog = Gtk.Window(title=title, transient_for=self.window, modal=True, decorated=False)
        dialog.add_css_class('cati-dialog')
        dialog.set_default_size(width, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=['panel'])
        box.append(label(title, 'heading'))
        dialog.set_child(box)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', lambda c, k, *_: (dialog.destroy(), True)[1] if k == Gdk.KEY_Escape else False)
        dialog.add_controller(keys)
        return dialog

    def pick_folder(self, title, accept, start, callback):
        chooser = Gtk.FileDialog(title=title, accept_label=accept)
        if start and Path(start).is_dir(): chooser.set_initial_folder(Gio.File.new_for_path(str(start)))

        def finished(chooser, result):
            try: folder = chooser.select_folder_finish(result)
            except GLib.Error: return
            if folder and folder.get_path(): callback(folder.get_path())
        chooser.select_folder(self.window, None, finished)

    def run_batch(self, work, text='Trabajando…'):
        self.busy(True)
        self.timing.set_text(text)

        def run():
            try: out = work()
            except Exception as exc: out = batch.Outcome(errors=[('', str(exc))], message='No se pudo completar')
            GLib.idle_add(self.finish, out)
        threading.Thread(target=run, daemon=True, name='cati-batch').start()

    def finish(self, out):
        self.busy(False)
        self.marked.clear()
        self.dups_cache.clear()
        note = ''
        if out.errors:
            path, why = out.errors[0]
            note = f' · {plural(len(out.errors), "problema", "problemas")}: {Path(path).name + ": " if path else ""}{why}'
        if out.undo: self.after_change(out.message + note, out.undo, seconds=20)
        else:
            self.timing.set_text((out.message if not out.errors else '') + note)
            self.refresh_view()
        return False

    def move_to(self):
        hits = self.targets()
        if not hits: return
        paths = [h['path'] for h in hits]
        name = Path(paths[0]).name if len(paths) == 1 else plural(len(paths), 'elemento', 'elementos')
        self.pick_folder(f'Mover «{name}» a…', 'Mover aquí', Path(paths[0]).parent,
                         lambda folder: self.run_batch(lambda: batch.move(paths, folder), 'Moviendo…'))

    def copy_to(self):
        hits = self.targets()
        if not hits: return
        paths = [h['path'] for h in hits]
        name = Path(paths[0]).name if len(paths) == 1 else plural(len(paths), 'elemento', 'elementos')
        self.pick_folder(f'Copiar «{name}» a…', 'Copiar aquí', Path(paths[0]).parent,
                         lambda folder: self.run_batch(lambda: batch.copy(paths, folder), 'Copiando…'))

    def compress(self):
        hits = self.targets()
        if not hits: return
        paths = [h['path'] for h in hits]
        self.run_batch(lambda: batch.compress(paths), 'Comprimiendo…')

    def trash(self):
        hits = self.targets()
        if not hits: return
        paths = [h['path'] for h in hits]
        if self.view == 'dups':
            # Copies are checked again just before deletion: one may have been edited since the scan.
            originals = {p: group['paths'][0] for group in self.dup_groups for p in group['paths'][1:]}
            def work():
                kept = [p for p in paths if p not in originals or disk.still_identical([originals[p], p])]
                out = batch.trash(kept)
                out.errors += [(p, 'ya no es idéntico al original') for p in paths if p not in kept]
                return out
            self.run_batch(work, 'Comprobando y enviando a la papelera…')
            return
        self.run_batch(lambda: batch.trash(paths), 'Enviando a la papelera…')

    def rename(self):
        hits = self.targets()
        if not hits: return
        if len(hits) > 1: self.rename_many([h['path'] for h in hits]); return
        path = Path(hits[0]['path'])
        dialog = self.dialog('Renombrar', 520)
        box = dialog.get_child()
        entry = Gtk.Entry(text=path.name, width_chars=48)
        box.append(entry)
        problem = label('', 'problem', wrap=True)
        box.append(problem)

        def apply(*_):
            name = entry.get_text().strip()
            if batch.bad_name(name): problem.set_text('Nombre no válido: sin «/», ni «.» o «..», y de 255 bytes como mucho.'); return
            if name != path.name and os.path.lexists(path.with_name(name)): problem.set_text('Ya existe un archivo con ese nombre.'); return
            out = batch.rename([(path, name, None)])
            if out.errors: problem.set_text(out.errors[0][1]); return
            dialog.destroy()
            out.message = f'Renombrado a {name}'
            self.finish(out)
        entry.connect('activate', apply)
        dialog.present()
        stem = len(path.stem) if path.suffix and not path.is_dir() else len(path.name)
        entry.grab_focus()
        entry.select_region(0, stem)

    def rename_many(self, paths):
        """Renombrar en lote con vista previa: lo que vas a ver es lo que se aplica."""
        dialog = self.dialog(f'Renombrar {plural(len(paths), "elemento", "elementos")}', 640)
        box = dialog.get_child()
        state = {'mode': 'replace', 'case': 'lower'}
        modes = Gtk.Box()
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        first_label, second_label = label('Buscar', 'hint'), label('Reemplazar por', 'hint')
        first, second = Gtk.Entry(hexpand=True), Gtk.Entry(hexpand=True)
        start = Gtk.SpinButton.new_with_range(0, 100000, 1)
        start.set_value(1)
        start_row = Gtk.Box(spacing=8)
        start_row.append(label('Empezar en', 'hint'))
        start_row.append(start)
        cases = Gtk.Box()
        for key, name in (('lower', 'minúsculas'), ('upper', 'MAYÚSCULAS'), ('title', 'Título'), ('capital', 'Primera')):
            chip = button(name, 'chip', toggle=True)
            chip.key = key
            chip.set_active(key == state['case'])
            cases.append(chip)
        plan_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['plan'])
        scroll = Gtk.ScrolledWindow(min_content_height=200, max_content_height=260, propagate_natural_height=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(plan_list)
        status = label('', 'hint')
        apply_button = button('Renombrar', 'action primary')
        current = {'plan': []}

        def refresh(*_):
            mode = state['mode']
            a = state['case'] if mode == 'case' else first.get_text()
            b = second.get_text()
            current['plan'] = batch.plan_rename(paths, mode, a, b, int(start.get_value()))
            self.clear(plan_list)
            ready = 0
            for path, name, problem in current['plan']:
                text = GLib.markup_escape_text(f'{path.name}  →  {name}')
                row = label('', 'bad' if problem and problem != 'sin cambios' else 'skip' if problem else '', ellipsize=Pango.EllipsizeMode.MIDDLE)
                row.set_markup(text + (f'   <i>({problem})</i>' if problem else ''))
                plan_list.append(row)
                ready += problem is None
            status.set_text(f'{plural(ready, "se renombrará", "se renombrarán")} de {len(paths)}')
            apply_button.set_sensitive(ready > 0)

        def set_mode(mode):
            state['mode'] = mode
            texts = {'replace': ('Buscar', 'Reemplazar por', '', ''), 'number': ('Plantilla: {n} número, {name} nombre', '', 'Archivo {n:02}', ''),
                     'affix': ('Prefijo', 'Sufijo', '', ''), 'case': ('', '', '', '')}[mode]
            first_label.set_text(texts[0]); second_label.set_text(texts[1])
            first.set_text(texts[2]); second.set_text(texts[3])
            first_label.set_visible(mode != 'case'); first.set_visible(mode != 'case')
            second_label.set_visible(mode in {'replace', 'affix'}); second.set_visible(mode in {'replace', 'affix'})
            start_row.set_visible(mode == 'number'); cases.set_visible(mode == 'case')
            for chip in chip_buttons: chip.set_active(chip.mode == mode)
            refresh()
        chip_buttons = []
        for mode, name in batch.MODES.items():
            chip = button(name, 'chip', toggle=True)
            chip.mode = mode
            chip.connect('toggled', lambda c: c.get_active() and state['mode'] != c.mode and set_mode(c.mode))
            chip_buttons.append(chip)
            modes.append(chip)
        child = cases.get_first_child()
        while child:
            child.connect('toggled', lambda c: (c.get_active() and (state.update(case=c.key), [x.set_active(x is c) for x in cases_buttons], refresh())))
            child = child.get_next_sibling()
        cases_buttons = []
        child = cases.get_first_child()
        while child: cases_buttons.append(child); child = child.get_next_sibling()
        for entry in (first, second): entry.connect('changed', refresh)
        start.connect('value-changed', refresh)
        for widget in (modes, first_label, first, second_label, second, start_row, cases, scroll, status): fields.append(widget)
        box.append(fields)

        def apply(*_):
            plan = current['plan']
            dialog.destroy()
            self.run_batch(lambda: batch.rename(plan), 'Renombrando…')
        apply_button.connect('clicked', apply)
        footer = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        footer.append(button('Cancelar', 'action', lambda *_: dialog.destroy()))
        footer.append(apply_button)
        box.append(footer)
        set_mode('replace')
        dialog.present()
        first.grab_focus()

    # ------------------------------------------------------------- deshacer
    def after_change(self, message, undo, seconds=10):
        """Refresca la vista y ofrece deshacer unos segundos."""
        self.undo = (message, undo)
        self.filters.set_visible(True)
        self.timing.set_text(message)
        self.clear(self.tags)
        self.tags.append(button(f'{UNDO_G}  Deshacer (Ctrl+Z)', 'undo', lambda *_: self.undo_action()))
        GLib.timeout_add_seconds(seconds, self.expire_undo, self.undo)
        self.shown = None
        GLib.timeout_add(400, lambda: (self.refresh_view(), False)[1])

    def expire_undo(self, token):
        if self.undo is token:
            self.undo = None
            self.clear(self.tags)
            if not self.entry.get_text().strip() and not self.scope and not self.view: self.filters.set_visible(False)
        return False

    def undo_action(self):
        if not self.undo: return
        message, undo = self.undo
        self.undo = None
        self.clear(self.tags)
        self.dups_cache.clear()
        try:
            undo()
            self.timing.set_text('Deshecho')
        except Exception as exc:
            self.timing.set_text(f'No se pudo deshacer: {exc}')
        GLib.timeout_add(400, lambda: (self.refresh_view(), False)[1])
