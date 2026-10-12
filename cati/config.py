import json
import os
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
APP = "cati"
APP_ID = "dev.cati.Cati"
DATA = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local/share")) / APP
CONFIG = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / APP / "config.json"
MODEL_REVISION = "bfcd298762cc34d0357ece5ebdd31791a3a374d8"
FINGERPRINT = f"embeddinggemma-2:{MODEL_REVISION}:llama-b11461:linux-v1:768"
def user_folder(key, fallback):
    try:
        value = subprocess.check_output(['xdg-user-dir', key], text=True, timeout=3).strip()
        if value and Path(value) != Path.home() and Path(value).is_dir(): return value
    except (OSError, subprocess.SubprocessError): pass
    return str(Path.home() / fallback)


DEFAULTS = {"folders": [user_folder('DOCUMENTS', 'Documents'), user_folder('DOWNLOAD', 'Downloads')],
            "code": False, "media": True, "port": 18769, "threads": 4,
            "max_file_mb": 500, "max_text_mb": 32, "max_pdf_pages": 5000, "video_stride": 8,
            "exclude": [str(PROJECT)], "gpu_layers": 0}
DEFAULTS.update(scope='computer', skip_datasets=False, project_screenshots_only=False,
                scan_seconds=300, event_delay=2, job_segments=16, job_seconds=2, engine_idle=900)


def load():
    if CONFIG.exists():
        return DEFAULTS | json.loads(CONFIG.read_text())
    return DEFAULTS.copy()


def save(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(CONFIG)


def prepare():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    if not CONFIG.exists():
        save(DEFAULTS)
