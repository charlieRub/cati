import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .config import DATA, prepare, load, save


def applied():
    """La vigilancia lee los ajustes al arrancar: se reinicia (si estaba en marcha) para aplicarlos."""
    from .service import restart
    try: restart('watch')
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f'Ajustes guardados; reinicia la vigilancia para aplicarlos ({exc})', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="cati", description="Cati: encuentra cualquier archivo describiéndolo")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("explorer", help="explorador de archivos a pantalla completa (Super+Alt+E)")
    sub.add_parser("quick", help="búsqueda rápida en ventana flotante (Super+Alt+B)")
    gui = sub.add_parser("gui", help="igual que «explorer»; con --search, igual que «quick»")
    gui.add_argument("--background", action="store_true", help="arranca residente sin mostrar la ventana")
    gui.add_argument("--search", action="store_true", help="solo la ventana de búsqueda (sin el explorador a pantalla completa)")
    sub.add_parser("engine")
    sub.add_parser("index")
    sub.add_parser("watch")
    sub.add_parser("all")
    sub.add_parser("status")
    sub.add_parser("doctor", help="comprueba las dependencias del sistema")
    speak = sub.add_parser("voice", help="abre Cati escuchando (o transcribe --file)")
    speak.add_argument("--file", type=Path, help="transcribe e interpreta un audio en vez de grabar")
    scan = sub.add_parser("scan")
    scan.add_argument("--code", action="store_true")
    query = sub.add_parser("search")
    query.add_argument("query", nargs='?', default='')
    query.add_argument('--image',type=Path)
    query.add_argument("--lexical", action="store_true")
    query.add_argument("--limit", type=int, default=15)
    folders = sub.add_parser("folders")
    folders.add_argument("paths", nargs="*")
    args = parser.parse_args()
    prepare()
    if args.command in {"explorer", "quick"}:
        from .gui import run
        run(mode="explorer" if args.command == "explorer" else "search")
        return
    if args.command == "voice":
        if args.file:
            from .voice import command, transcribe
            text = transcribe(args.file, persistent=False)
            print(json.dumps({"texto": text, "accion": command(text)}, ensure_ascii=False))
            return
        from .gui import run
        run(voice=True)
        return
    if args.command == "doctor":
        from .doctor import report, service_report
        missing = report()
        service_report()
        raise SystemExit(1 if missing else 0)
    if args.command in {"engine", "watch"}:
        from .service import claim
        release = claim(args.command)
        try:
            if args.command == "engine":
                from .engine import run_server
                run_server()
            else:
                from .watch import run
                try: run()
                except RuntimeError as exc: raise SystemExit(f'cati watch: {exc}')
        finally: release()
    elif args.command == "index":
        from .indexer import index, lock_path
        import fcntl
        import signal
        import threading
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        from .scope import resolved
        from .store import connect
        from . import catalog
        config = resolved(load())
        # The lock first: the catalog must not be rewritten while the background service is indexing.
        with lock_path().open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise SystemExit('cati index: el servicio de vigilancia ya está indexando (cati status); no hace falta lanzarlo a mano.')
            db = connect()
            catalog.sync(db, config["folders"], config["exclude"], stop)
            db.close()
            print(json.dumps(index(config, stop=stop, locked=True), ensure_ascii=False))
    elif args.command == 'all':
        from .scope import roots
        config=load();config['scope']='computer';save(config)
        applied()
        print(json.dumps([str(p) for p in roots(config)],ensure_ascii=False,indent=2))
    elif args.command == "search":
        from .search import search
        print(json.dumps(search(args.query, args.limit, lexical=args.lexical,image_path=args.image), ensure_ascii=False, indent=2))
    elif args.command == "scan":
        from .readers import walk
        from .scope import resolved
        config = resolved(load())
        if args.code: config["code"] = True
        files = list(walk(config))
        from collections import Counter
        print(json.dumps({"files": len(files), "types": dict(Counter(k for _, k in files)),
                          "bytes": sum(p.stat().st_size for p, _ in files)}, indent=2))
    elif args.command == "status":
        from .store import connect, opened
        with opened() as db:
            stats = dict(db.execute("SELECT kind,count(*) FROM files WHERE error IS NULL GROUP BY kind").fetchall())
            errors = db.execute("SELECT count(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
            segments = db.execute("SELECT count(*) FROM segments").fetchone()[0]
        try: progress = json.loads((DATA / "status.json").read_text())
        except (OSError, ValueError): progress = None
        if progress and progress.get('running') and time.time() - progress.get('updated', 0) > 30:
            # Killed without a chance to say so: the file still says «running».
            progress['running'] = False
            progress['stale'] = f"sin noticias del servicio desde hace {int(time.time() - progress.get('updated', 0))} s"
        print(json.dumps({"files": stats, "segments": segments, "errors": errors, "progress": progress}, ensure_ascii=False, indent=2))
    elif args.command == "folders":
        config = load()
        if args.paths:
            roots = [str(Path(p).expanduser().resolve()) for p in args.paths]
            if not all(Path(p).is_dir() for p in roots): parser.error("Todas las rutas deben ser carpetas existentes")
            config["folders"] = roots
            config['scope']='folders'
            save(config)
            applied()
        print(json.dumps(config["folders"], ensure_ascii=False, indent=2))
    else:
        from .gui import run
        run(getattr(args, "background", False), mode="search" if getattr(args, "search", False) else "explorer")


if __name__ == "__main__":
    main()
