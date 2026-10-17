"""Piezas mínimas de GTK compartidas por la ventana y el explorador."""
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gtk


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


def short_path(path):
    home = str(Path.home())
    path = str(path)
    return '~' + path[len(home):] if path == home or path.startswith(home.rstrip('/') + '/') else path


class Squeezer(Gtk.Widget):
    """Muestra la primera variante que cabe en el ancho disponible (de la más completa a la más compacta).

    Pide el ancho mínimo de la más compacta, así que nunca obliga a la ventana a crecer.
    """
    __gtype_name__ = 'CatiSqueezer'

    def __init__(self, variants, **props):
        super().__init__(**props)
        self.variants = variants
        for child in variants: child.set_parent(self)

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return self.variants[-1].measure(orientation, -1)[0], self.variants[0].measure(orientation, -1)[1], -1, -1
        sizes = [child.measure(orientation, -1)[:2] for child in self.variants]
        return max(s[0] for s in sizes), max(s[1] for s in sizes), -1, -1

    def do_size_allocate(self, width, height, baseline):
        chosen = next((c for c in self.variants if c.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= width), self.variants[-1])
        for child in self.variants:
            if child is not chosen and child.get_child_visible(): child.set_child_visible(False)
        if not chosen.get_child_visible(): chosen.set_child_visible(True)
        chosen.allocate(max(width, chosen.measure(Gtk.Orientation.HORIZONTAL, -1)[0]), height, baseline, None)

    def do_dispose(self):
        for child in self.variants: child.unparent()
        self.variants = []
        Gtk.Widget.do_dispose(self)
