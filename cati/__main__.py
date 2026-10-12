import argparse
import json
import subprocess
from pathlib import Path

from .config import DATA, prepare, load, save


def main():
    parser = argparse.ArgumentParser(prog="cati", description="Cati: encuentra cualquier archivo describiéndolo")
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui")
    gui.add_argument("--background", action="store_true", help="arranca residente sin mostrar la ventana")
    sub.add_parser("engine")
    sub.add_parser("index")
    sub.add_parser("watch")
    sub.add_parser("all")
    sub.add_parser("status")
    sub.add_parser("doctor", help="comprueba las dependencias del sistema")
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
    if args.command == "doctor":
        from .doctor import report
        raise SystemExit(1 if report() else 0)
    if args.command == "engine":
        from .engine import run_server
        run_server()
    elif args.command == "index":
        from .indexer import index
        import signal
        import threading
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        from .scope import resolved
        from .store import connect
        from . import catalog
        config = resolved(load())
        db = connect()
        catalog.sync(db, config["folders"], config["exclude"], stop)
        db.close()
        print(json.dumps(index(config,stop=stop), ensure_ascii=False))
    elif args.command == 'watch':
        from .watch import run
        run()
    elif args.command == 'all':
        from .scope import roots
        config=load();config['scope']='computer';save(config)
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
        print(json.dumps({"files": stats, "segments": segments, "errors": errors,
                          "progress": json.loads((DATA / "status.json").read_text()) if (DATA / "status.json").exists() else None}, ensure_ascii=False, indent=2))
    elif args.command == "folders":
        config = load()
        if args.paths:
            roots = [str(Path(p).expanduser().resolve()) for p in args.paths]
            if not all(Path(p).is_dir() for p in roots): parser.error("Todas las rutas deben ser carpetas existentes")
            config["folders"] = roots
            config['scope']='folders'
            save(config)
        print(json.dumps(config["folders"], ensure_ascii=False, indent=2))
    else:
        from .gui import run
        run(getattr(args, "background", False))


if __name__ == "__main__":
    main()
