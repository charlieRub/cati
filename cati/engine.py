"""Cliente del motor llama.cpp de Cati, limitado a loopback y autenticado."""
import base64
import json
import http.client
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

from .config import DATA, PROJECT, load


class EngineError(RuntimeError):
    pass


ACTIVE = DATA / 'search.active'


def mark_search():
    """La búsqueda interactiva avisa de que está usando el motor."""
    try: ACTIVE.touch()
    except OSError: pass


def yield_to_search(quiet=3.0, longest=60.0):
    """El análisis de fondo espera a que pasen unos segundos sin búsquedas antes de usar la GPU."""
    waited = 0.0
    while waited < longest:
        try: age = time.time() - ACTIVE.stat().st_mtime
        except OSError: return
        if age >= quiet: return
        time.sleep(quiet - age)
        waited += quiet - age


class Engine:
    # Background indexing uses one of the server's two slots; the other is always free for a search.
    background_slot = threading.BoundedSemaphore(1)

    def __init__(self, start=True, background=False):
        self.background = background
        self.config = load()
        self.start = start
        self.key = (DATA / "engine.key").read_text().strip()
        self.url = f"http://127.0.0.1:{int(self.config['port'])}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.ready = False

    def encode(self, inputs):
        if self.background:
            yield_to_search()
            with Engine.background_slot:
                began = time.monotonic()
                result = self._encode(inputs)
                # Pace background GPU work, while interactive searches retain
                # their own server slot and do not wait for this cooldown.
                elapsed = time.monotonic() - began
                duty = max(.05,min(1,float(self.config.get('background_duty',.25))))
                if duty < 1: time.sleep(min(10,elapsed*(1-duty)/duty))
                return result
        mark_search()
        return self._encode(inputs)

    def _encode(self, inputs):
        if self.start and not self.ready:
            from .service import start
            start("engine")
            deadline = time.monotonic() + 60
            while True:
                try:
                    req = urllib.request.Request(self.url + '/health', headers={'Authorization': f'Bearer {self.key}'})
                    with self.opener.open(req, timeout=3): break
                except (urllib.error.URLError, TimeoutError) as exc:
                    if isinstance(exc, urllib.error.HTTPError): exc.close()
                    if time.monotonic() >= deadline: raise EngineError('El motor no pudo arrancar. Consulta journalctl --user -u cati-engine o ~/.local/share/cati/engine.log.')
                    time.sleep(.2)
            self.ready = True
        request = urllib.request.Request(self.url + "/v1/embeddings",
            data=json.dumps({"input": inputs, "encoding_format": "float"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        # A sleeping server wakes on the request; this timeout also covers media on CPU.
        try:
            with self.opener.open(request, timeout=180) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            self.ready = False
            if isinstance(exc,urllib.error.HTTPError):exc.close()
            raise EngineError(f"El motor local no respondió: {exc}") from exc
        rows = sorted(payload["data"], key=lambda x: x["index"])
        if len(rows) != len(inputs):
            raise EngineError("El motor devolvió un número de vectores inesperado")
        result = np.asarray([row["embedding"] for row in rows], dtype=np.float32)
        if result.shape != (len(inputs), 768) or not np.isfinite(result).all():
            raise EngineError("Vector inválido: dimensión o valores no finitos")
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        if (norms < 1e-8).any():
            raise EngineError("El motor devolvió un vector vacío")
        return result / norms

    def text(self, texts, titles=None, query=False, code=False):
        if query:
            task = "code retrieval" if code else "search result"
            return self.encode([f"task: {task} | query: {text}" for text in texts])
        return self.encode([f"title: {title} | text: {text}" for title, text in
                            zip(titles or ["none"] * len(texts), texts)])

    def media(self, path, kind):
        data = base64.b64encode(Path(path).read_bytes()).decode()
        if kind == "image":
            part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}}
        else:
            part = {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}}
        return self.encode([{"content": [part]}])[0]


def run_server():
    import os
    config = load()
    backend = 'vulkan' if config['gpu_layers'] else 'runtime'
    binary = PROJECT / f"build/{backend}/bin/llama-server"
    args = [str(binary), "--model", str(DATA / "models/embeddinggemma-2-Q8_0.gguf"),
            "--host", "127.0.0.1", "--port", str(config["port"]), "--embedding",
            "--pooling", "mean", "--ctx-size", "4096", "--batch-size", "2048",
            # Two slots: a search never waits behind an indexing batch.
            "--ubatch-size", "2048", "--parallel", "2", "--threads", str(config["threads"]),
            "--threads-batch", str(config["threads"]), "--n-gpu-layers", str(config["gpu_layers"]),
            "--api-key-file", str(DATA / "engine.key"), "--sleep-idle-seconds", str(config.get("engine_idle", 900)),
            "--no-warmup", "--log-verbosity", "1", "--image-min-tokens", "1", "--image-max-tokens", "1120"]
    if config["media"]:
        args += ["--mmproj", str(DATA / "models/mmproj-embeddinggemma-2-Q8_0.gguf")]
    os.execv(binary, args)
