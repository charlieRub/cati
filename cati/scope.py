"""Carpeta personal y montajes de datos, sin recorrer sistemas de archivos virtuales."""
import json
import os
import subprocess
from pathlib import Path


def roots(config):
    paths = [Path(p).expanduser().resolve() for p in config['folders']]
    if config.get('scope') == 'computer':
        paths.append(Path.home())
        try:
            mounts = json.loads(subprocess.check_output(['findmnt','-J','-l','-o','TARGET,FSTYPE'],text=True,timeout=5))
            for mount in mounts['filesystems']:
                path = Path(mount['target'])
                if str(path).startswith(('/mnt/','/media/','/run/media/')) and mount['fstype'] not in {'proc','sysfs','devtmpfs','tmpfs','autofs'}:
                    paths.append(path)
        except (OSError,ValueError,subprocess.SubprocessError): pass
        for path in [Path('/mnt'),Path('/media')/Path.home().name,Path('/run/media')/Path.home().name]:
            if path.is_dir() and os.access(path,os.R_OK|os.X_OK): paths.append(path)
    result=[]
    for path in sorted(set(paths),key=lambda p:len(p.parts)):
        if path.is_dir() and os.access(path,os.R_OK|os.X_OK) and not any(p==path or p in path.parents for p in result): result.append(path)
    return result


def resolved(config):
    return config | {'folders':[str(p) for p in roots(config)]}
