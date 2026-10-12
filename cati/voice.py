"""Búsqueda por voz: graba hasta que dejas de hablar, transcribe en local y decide si es búsqueda u orden.

Nada se ejecuta en reposo: el micrófono y el modelo de Whisper solo se usan mientras hablas.
"""
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

from .config import DATA, PROJECT, load
from .query import normalize

RATE = 16000
FRAME = 480  # 30 ms
MODELS = {'gpu': 'ggml-large-v3-turbo-q5_0.bin', 'cpu': 'ggml-small-q5_1.bin'}


class NoMicrophone(RuntimeError):
    pass


class NotHeard(RuntimeError):
    pass


def recorder():
    """Orden de captura en bruto (16 kHz, mono, s16le) disponible en este sistema."""
    if shutil.which('parecord'):
        return ['parecord', '--raw', f'--rate={RATE}', '--channels=1', '--format=s16le', '--latency-msec=30']
    if shutil.which('arecord'):
        return ['arecord', '-q', '-f', 'S16_LE', '-r', str(RATE), '-c', '1', '-t', 'raw']
    raise NoMicrophone('No hay herramienta de grabación (instala pulseaudio-utils o alsa-utils)')


def record(stop=None, on_level=None, max_seconds=15, wait_seconds=6, silence=0.9):
    """Graba hasta 0,9 s de silencio tras hablar. Devuelve los datos PCM o lanza NoMicrophone/NotHeard.

    El umbral se adapta al ruido de fondo de los primeros 300 ms, así funciona igual con un
    micrófono de mesa que con unos auriculares.
    """
    process = subprocess.Popen(recorder(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames, floor, speaking, quiet, began = [], [], False, 0, time.monotonic()
    try:
        while True:
            if stop and stop.is_set(): break
            data = process.stdout.read(FRAME * 2)
            if len(data) < FRAME * 2:
                error = process.stderr.read().decode(errors='replace')
                raise NoMicrophone('No hay ningún micrófono conectado' if 'entity' in error or 'target' in error or not frames
                                   else error.strip() or 'La grabación se interrumpió')
            samples = np.frombuffer(data, '<i2').astype(np.float32)
            level = float(np.sqrt(np.mean(samples * samples)))
            frames.append(data)
            elapsed = time.monotonic() - began
            if len(floor) < 10: floor.append(level)
            threshold = max(np.median(floor) * 3.0, 250.0)
            if on_level: on_level(min(level / 4000, 1.0), speaking)
            if level > threshold:
                speaking, quiet = True, 0
            elif speaking:
                quiet += 1
                if quiet * FRAME / RATE >= silence: break
            if not speaking and elapsed > wait_seconds: raise NotHeard('No te he oído')
            if elapsed > max_seconds: break
    finally:
        process.terminate()
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: process.kill()
    return b''.join(frames)


def whisper(tool='whisper-cli'):
    """Binario de whisper.cpp (GPU si se compiló con Vulkan) y el modelo que le corresponde."""
    for backend, model in (('vulkan', MODELS['gpu']), ('cpu', MODELS['cpu']), ('cpu', MODELS['gpu'])):
        binary = PROJECT / f'build/whisper-{backend}/bin/{tool}'
        weights = DATA / 'models' / model
        if binary.exists() and weights.exists(): return binary, weights
    raise RuntimeError('La voz no está instalada: ejecuta ./install.sh')


class Server:
    """whisper-server bajo demanda: carga el modelo en el primer uso y lo libera tras dos minutos sin hablar."""
    def __init__(self, idle=120):
        self.idle = idle
        self.process = None
        self.used = 0.0
        self.lock = threading.Lock()
        self.port = int(load().get('port', 18769)) + 1

    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def ensure(self):
        with self.lock:
            self.used = time.monotonic()
            if self.process and self.process.poll() is None: return True
            binary, weights = whisper('whisper-server')
            language = load().get('voice_language', 'es')
            self.process = subprocess.Popen([str(binary), '-m', str(weights), '--host', '127.0.0.1', '--port', str(self.port),
                                             '-l', language, '-nt', '-t', '4', '--prompt', 'Búsqueda de archivos en el ordenador.'],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None: return False
                try:
                    urllib.request.urlopen(self.url(), timeout=1).close()
                    threading.Thread(target=self.reaper, daemon=True).start()
                    return True
                except OSError:
                    time.sleep(.1)
            return False

    def reaper(self):
        while True:
            time.sleep(10)
            with self.lock:
                if self.process is None or self.process.poll() is not None: return
                if time.monotonic() - self.used > self.idle:
                    self.process.terminate(); self.process = None
                    return

    def transcribe(self, wav):
        self.used = time.monotonic()
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\ntext\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="audio_ctx"\r\n\r\n{context(len(wav) - 44)}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voz.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + wav + f'\r\n--{boundary}--\r\n'.encode()
        request = urllib.request.Request(self.url() + '/inference', data=body,
                                         headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode('utf-8', 'replace')

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None: self.process.terminate()
            self.process = None


_server = None


def context(pcm_bytes):
    """Whisper codifica siempre 30 s; para una frase corta basta su duración más margen (≈5 veces menos trabajo)."""
    seconds = pcm_bytes / (RATE * 2)
    return min(1500, int(seconds * 50) + 128)


def server():
    global _server
    if _server is None: _server = Server()
    return _server


def transcribe(source, language=None, persistent=True):
    """Texto de un WAV o de PCM en bruto. Usa la GPU cuando whisper.cpp se compiló con Vulkan; con
    `persistent`, el modelo queda cargado entre frases y se libera solo al dejar de usar la voz."""
    binary, weights = whisper()
    language = language or load().get('voice_language', 'es')
    with tempfile.TemporaryDirectory(prefix='cati-voice-') as work:
        path = Path(work) / 'voz.wav'
        if isinstance(source, (bytes, bytearray)):
            with wave.open(str(path), 'wb') as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(RATE); out.writeframes(source)
        else:
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-t', '30', '-ac', '1', '-ar', str(RATE), str(path)],
                           check=True, timeout=60)
        if persistent:
            try:
                if server().ensure(): return clean(server().transcribe(path.read_bytes()))
            except OSError:
                server().stop()
        result = subprocess.run([str(binary), '-m', str(weights), '-f', str(path), '-l', language, '-nt', '-np',
                                 '-ac', str(context(path.stat().st_size - 44)),
                                 '-t', '4', '--prompt', 'Búsqueda de archivos en el ordenador.'],
                                capture_output=True, text=True, timeout=120)
    if result.returncode: raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'Whisper falló')
    return clean(result.stdout)


def clean(text):
    text = ' '.join(text.split())
    return re.sub(r'^[\s¿¡"\']+|[\s.?!"\']+$', '', text)


ORDINALS = {'primero': 1, 'primer': 1, 'primera': 1, 'segundo': 2, 'segunda': 2, 'tercero': 3, 'tercer': 3, 'tercera': 3,
            'cuarto': 4, 'cuarta': 4, 'quinto': 5, 'quinta': 5, 'sexto': 6, 'sexta': 6, 'septimo': 7, 'octavo': 8,
            'noveno': 9, 'decimo': 10, 'ultimo': -1, 'ultima': -1}
LEAD = re.compile(r'^(?:oye cati|cati|por favor|porfa|quiero|me gustaria|necesito|puedes|podrias)?\s*'
                  r'(?:busca(?:me)?|buscar|encuentra(?:me)?|encontrar|ensena(?:me)?|muestra(?:me)?|dame|pon(?:me)?|abre(?:me)?)?\s*', re.I)


def command(text):
    """Interpreta lo dicho: ('search', frase) o una orden sobre los resultados ('open', n), ('folder',), …"""
    plain = normalize(text).strip(' .,')
    match = re.match(r'^(?:abre|abrir|abreme)\s+(?:el|la)?\s*(\w+)(?:\s+(?:resultado|archivo|documento))?$', plain)
    if match and (match.group(1) in ORDINALS or match.group(1).isdigit()):
        word = match.group(1)
        return ('open', ORDINALS.get(word) or int(word))
    if re.fullmatch(r'(?:abre(?:lo|la)?|abrir|ese|esa|este|esta|vale|perfecto|eso)', plain): return ('open', 0)
    if re.fullmatch(r'(?:muestra(?:lo|la)?|abre(?:lo|la)?|ensena(?:lo|la)?)?\s*(?:en\s+)?(?:su|la)\s+carpeta', plain): return ('folder',)
    if re.fullmatch(r'(?:ver\s+|muestra\s+|ensena\s+)?(?:las\s+)?propiedades|informacion|detalles', plain): return ('properties',)
    if re.fullmatch(r'(?:siguiente|el siguiente|baja|abajo)', plain): return ('move', 1)
    if re.fullmatch(r'(?:anterior|el anterior|sube|arriba)', plain): return ('move', -1)
    if re.fullmatch(r'(?:copia|copiar)\s+(?:la\s+)?ruta', plain): return ('copy',)
    if re.fullmatch(r'(?:cierra|cerrar|cancela|cancelar|salir|nada|olvidalo)', plain): return ('close',)
    if re.fullmatch(r'(?:borra|borrar|elimina|eliminar)(?:lo|la)?|(?:manda|envia)(?:lo|la)? a la papelera', plain): return ('trash',)
    # LEAD matched on the normalized text: cut the same number of words from the original phrase.
    words = re.sub(r'[,;:]', ' ', text).split()
    removed = len(LEAD.match(normalize(' '.join(words))).group(0).split())
    query = ' '.join(words[removed:]) or text
    return ('search', query.strip(' .,¿?¡!'))
