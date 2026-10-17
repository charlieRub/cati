"""Prueba real de la interfaz: busca, selecciona, abre la vista previa y guarda una captura.

Uso: .venv/bin/python tests/gui_smoke.py "consulta"   (usa tu índice; no roba el foco)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import GLib, Graphene, Gtk

from cati.gui import Cati

query = sys.argv[1] if len(sys.argv) > 1 else 'documentos'
app = Cati()
app.set_application_id('dev.cati.SmokeTest')
failures = []


def capture():
    window = app.window
    width, height = window.get_width(), window.get_height()
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None: return None   # la ventana aún no ha pintado nada: la captura es opcional
    texture = window.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width, height))
    path = Path(tempfile.gettempdir()) / 'cati-smoke.png'
    texture.save_to_png(str(path))
    return path


def activated(*_):
    def search():
        app.entry.set_text(query)
        GLib.timeout_add(4000, check)
        return False

    def check():
        try:
            assert app.hits, 'sin resultados'
            assert app.results.get_selected_row() is not None, 'sin selección'
            app.move(1)
            assert app.current is not None, 'la selección no actualiza el resultado actual'
            print(f'{len(app.hits)} resultados · captura en {capture()}')
        except Exception as exc:
            failures.append(str(exc))
        app.quit()
        return False
    GLib.timeout_add(1500, search)
    GLib.timeout_add_seconds(30, lambda: (failures.append('tiempo agotado'), app.quit(), False)[-1])


app.connect('activate', activated)
app.run(None)
if failures: raise SystemExit('; '.join(failures))
print('Interfaz: búsqueda, resultados, teclado y vista previa: OK')
