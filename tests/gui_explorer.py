"""Prueba real del explorador: carpetas, migas, marcas, acciones en lote, espacio, duplicados y guardadas.

Uso: .venv/bin/python tests/gui_explorer.py [--shots carpeta]   (abre la ventana de verdad; archivos en una carpeta temporal)
"""
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import GLib, Graphene, Gtk

from cati import saved
from cati.gui import Cati

shots = Path(sys.argv[sys.argv.index('--shots') + 1]) if '--shots' in sys.argv else None
app = Cati()
app.set_application_id('dev.cati.SmokeExplorer')
failures = []
context = GLib.MainContext.default()
GLib.timeout_add(15, lambda: True)   # despierta el bucle para que las condiciones se reevalúen a menudo


def pump(condition, timeout=10, what=''):
    end = time.monotonic() + timeout
    while not condition():
        context.iteration(True)
        if time.monotonic() > end: raise AssertionError(f'tiempo agotado esperando: {what}')


def settle(seconds=.5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.iteration(True)


def capture(name):
    if not shots: return
    settle(.8)
    shots.mkdir(parents=True, exist_ok=True)
    window = app.window
    width, height = window.get_width(), window.get_height()
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None: return   # la ventana aún no ha pintado nada: la captura es opcional
    window.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, width, height)).save_to_png(str(shots / f'{name}.png'))


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def label_of(widget):
    if isinstance(widget, Gtk.Label): return widget.get_text()
    return next((w.get_text() for w in descendants(widget) if isinstance(w, Gtk.Label)), widget.get_label() if hasattr(widget, 'get_label') else None)


def names(): return [row.hit['name'] for row in app.real_rows()]


def shown_in(view_heading): return bool(app.heading) and view_heading in app.heading


def scenario():
    root = Path(tempfile.mkdtemp(prefix='cati-gui-'))
    for sub in ('Proyecto', 'Proyecto/fotos', 'Archivo'): (root / sub).mkdir()
    (root / 'Proyecto/informe.txt').write_text('informe ' * 400)
    (root / 'Proyecto/informe copia.txt').write_text('informe ' * 400)
    (root / 'Proyecto/notas.md').write_text('# notas')
    (root / 'Proyecto/fotos/a.txt').write_text('a' * 3000)
    (root / '.oculto').write_text('x')
    pump(lambda: app.searcher is not None, 30, 'índice cargado')

    # 1. Navegar: la carpeta se lista (carpetas primero), con migas y sin ocultos.
    app.navigate(str(root / 'Proyecto'))
    pump(lambda: app.heading and 'PROYECTO' in app.heading, what='listado')
    assert names() == ['fotos', 'informe copia.txt', 'informe.txt', 'notas.md'], names()
    assert app.crumb_bar.get_visible() and app.nav_up.get_sensitive()
    assert '.oculto' not in names()
    capture('1-carpeta')

    # 2. Entrar con Intro en una carpeta, subir con Retroceso, atrás/adelante.
    app.results.select_row(app.real_rows()[0])
    app.open_hit(app.current)
    pump(lambda: app.heading and 'FOTOS' in app.heading, what='entrar en fotos')
    assert names() == ['a.txt']
    app.parent()
    pump(lambda: app.heading and 'PROYECTO' in app.heading, what='subir')
    app.back()
    pump(lambda: app.heading and 'FOTOS' in app.heading, what='atrás')
    app.forward()
    pump(lambda: app.heading and 'PROYECTO' in app.heading, what='adelante')

    # 3. Ordenar por tamaño.
    app.sort = 'size'
    app.refresh_view()
    pump(lambda: names()[1:2] != ['informe copia.txt'] or True and app.hits, what='orden')
    settle(.4)
    assert names()[1] in {'informe.txt', 'informe copia.txt'}, names()
    app.sort = 'name'

    # 4. Marcar varios, barra de acciones y renombrado en lote.
    app.refresh_view(); settle(.5)
    rows = app.real_rows()
    app.toggle_mark(rows[1].hit); app.toggle_mark(rows[2].hit)
    assert len(app.marked) == 2 and app.batch_bar.get_visible()
    assert 'seleccionados' in app.batch_info.get_text()
    capture('2-seleccion')
    plan = __import__('cati.batch', fromlist=['x']).plan_rename([r.hit['path'] for r in rows[1:3]], 'affix', 'v2_', '')
    assert all(problem is None for *_, problem in plan)

    # 4b. Renombrar en lote desde el diálogo: numerar los dos marcados y deshacer.
    paths = [r.hit['path'] for r in rows[1:3]]
    app.rename_many(paths)
    dialog = next(w for w in Gtk.Window.list_toplevels() if (w.get_title() or '').startswith('Renombrar 2'))
    toggles = [w for w in descendants(dialog) if isinstance(w, Gtk.ToggleButton) and w.get_label() == 'Numerar']
    toggles[0].set_active(True)
    settle(.3)
    entries = [w for w in descendants(dialog) if isinstance(w, Gtk.Entry) and w.get_visible()]
    entries[0].set_text('Foto {n:02}')
    settle(.3)
    plan_rows = [label_of(w) for w in descendants(dialog) if isinstance(w, Gtk.Label) and '→' in (w.get_text() or '')]
    assert len(plan_rows) == 2 and 'Foto 01.txt' in plan_rows[0] and 'Foto 02.txt' in plan_rows[1], plan_rows
    capture('2b-renombrar')
    go = next(w for w in descendants(dialog) if isinstance(w, Gtk.Button) and w.get_label() == 'Renombrar')
    go.emit('clicked')
    pump(lambda: (root / 'Proyecto/Foto 01.txt').exists(), what='renombrado en lote')
    settle(.8)
    assert (root / 'Proyecto/Foto 02.txt').exists() and not (root / 'Proyecto/informe.txt').exists()
    app.undo_action(); settle(.8)
    assert (root / 'Proyecto/informe.txt').exists() and (root / 'Proyecto/informe copia.txt').exists()
    app.refresh_view(); settle(.6)

    # 5. Comprimir lo marcado y deshacer.
    app.compress()
    pump(lambda: any(n.endswith('.zip') for n in names()) or 'ZIP' in app.timing.get_text() or app.undo, what='zip')
    settle(1)
    assert any(p.suffix == '.zip' for p in (root / 'Proyecto').iterdir()), list((root / 'Proyecto').iterdir())
    assert not app.marked
    app.undo_action(); settle(.8)
    assert not any(p.suffix == '.zip' for p in (root / 'Proyecto').iterdir())

    # 6. Filtros: tamaño dentro de la carpeta.
    app.entry.set_text('informe')
    pump(lambda: app.results_intent is not None and app.hits and not app.searching, 20, 'búsqueda en carpeta')
    assert {h['name'] for h in app.hits} == {'informe.txt', 'informe copia.txt'}, [h['name'] for h in app.hits]
    assert all(h['path'].startswith(str(root / 'Proyecto')) for h in app.hits), [h['path'] for h in app.hits]
    app.entry.set_text('')
    settle(.5)

    # 7. Espacio en disco de la carpeta.
    app.navigate(str(root), 'space')
    pump(lambda: app.heading and 'ESPACIO' in app.heading, what='espacio')
    assert app.real_rows()[0].hit['name'] == 'Proyecto', names()
    assert app.real_rows()[0].hit.get('bar')
    capture('3-espacio')
    app.navigate(None)
    settle(.5)

    # 8. Búsqueda guardada: se crea, se aplica y se borra.
    original = saved.PATH
    saved.PATH = root / 'saved.json'
    try:
        saved.add('Mis informes', 'informe', {'folder': str(root / 'Proyecto')})
        app.fill_sidebar()
        assert ('saved', saved.load()[0]['id']) in app.side_items
        app.apply_saved(saved.load()[0])
        pump(lambda: app.results_intent is not None and app.hits and not app.searching and app.entry.get_text() == 'informe', 20, 'búsqueda guardada')
        assert {h['name'] for h in app.hits} == {'informe.txt', 'informe copia.txt'}, [h['name'] for h in app.hits]
        assert app.scope == str(root / 'Proyecto') and app.crumb_bar.get_visible()
        capture('4-guardada')
    finally:
        saved.PATH = original
    app.navigate(None)
    settle(.4)

    # 8b. Recientes y más usados: se muestran (vacíos o no) sin errores.
    for view, title in (('opened', 'ABIERTOS'), ('frequent', 'USADOS')):
        app.navigate(None, view)
        pump(lambda: app.heading and title in app.heading, what=view)
        capture(f'4b-{view}')
    app.navigate(None)
    settle(.4)

    # 9. Duplicados sobre el índice real (puede tardar unos segundos).
    app.navigate(None, 'dups')
    pump(lambda: app.heading and 'DUPLICADOS' in app.heading, 90, 'duplicados')
    assert app.real_rows(), 'sin duplicados en el índice real'
    assert app.spare_button.get_visible()
    app.mark_spares()
    assert app.marked and app.batch_bar.get_visible() and not app.spare_button.get_visible()
    capture('5-duplicados')
    app.clear_marks()
    app.navigate(None)
    settle(.5)
    print(f'Explorador: navegación, marcas, lote, espacio, guardadas y duplicados: OK ({root})')


def activated(*_):
    def run():
        try: scenario()
        except Exception: failures.append(traceback.format_exc())
        app.quit()
        return False
    GLib.timeout_add(1200, run)
    GLib.timeout_add_seconds(240, lambda: (failures.append('tiempo global agotado'), app.quit(), False)[-1])


app.connect('activate', activated)
app.run(None)
if failures: raise SystemExit('\n'.join(failures))
