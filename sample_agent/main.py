"""Real file backup agent. Writes ZIP snapshots; never deletes source files or backups."""
import fnmatch
import hashlib
import json
import os
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CONFIG_SCHEMA = {
    'type': 'object',
    'required': ['source_directory', 'backup_directory', 'interval', 'compression', 'enabled'],
    'properties': {
        'source_directory': {'type': 'string', 'title': 'Source folder', 'minLength': 1, 'description': 'Relative paths resolve beside main.py.'},
        'backup_directory': {'type': 'string', 'title': 'Backup destination', 'minLength': 1},
        'interval': {'type': 'number', 'title': 'Check every (seconds)', 'minimum': 1, 'maximum': 86400},
        'enabled': {'type': 'boolean', 'title': 'Enable backup jobs'},
        'compression': {'type': 'string', 'title': 'Compression', 'enum': ['deflated', 'stored']},
        'mode': {'type': 'string', 'title': 'When to back up', 'enum': ['on_change', 'every_cycle'], 'format': 'radio'},
        'include_patterns': {'type': 'array', 'title': 'Include filename patterns', 'minItems': 1, 'items': {'type': 'string'}},
        'exclude_patterns': {'type': 'array', 'title': 'Exclude filename patterns', 'items': {'type': 'string'}},
        'max_file_mb': {'type': 'number', 'title': 'Maximum file size (MB)', 'minimum': 0.001, 'maximum': 1024},
        'label': {'type': 'string', 'title': 'Backup label', 'maxLength': 80}
    }
}

def backup_once(ctx, config):
    base = Path(__file__).resolve().parent
    source = (base / config['source_directory']).resolve()
    destination = (base / config['backup_directory']).resolve()
    if not source.is_dir(): raise ValueError('Source folder does not exist')
    if destination == source or source.is_relative_to(destination):
        raise ValueError('Backup destination must be separate from the source and its parents')
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for root, directories, filenames in os.walk(source, followlinks=False):
        directories[:] = sorted(d for d in directories if not (Path(root) / d).is_symlink() and not (Path(root) / d).resolve().is_relative_to(destination))
        for filename in sorted(filenames):
            path = Path(root) / filename
            if path.is_symlink() or not path.is_file(): continue
            relative = path.relative_to(source).as_posix()
            if not any(fnmatch.fnmatch(relative, pattern) for pattern in config.get('include_patterns', ['*'])): continue
            if any(fnmatch.fnmatch(relative, pattern) for pattern in config.get('exclude_patterns', [])): continue
            if path.stat().st_size > float(config.get('max_file_mb', 10)) * 1024 * 1024:
                ctx.log(f'Skipping oversized file: {relative}', 'warning')
                continue
            paths.append((path, relative))
    digest = hashlib.sha256()
    for path, relative in paths:
        digest.update(relative.encode())
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                if ctx.stopping.is_set(): return None
                digest.update(chunk)
    signature = digest.hexdigest()
    state = base / 'backup-state.json'
    previous = json.loads(state.read_text()) if state.exists() else {}
    if config.get('mode', 'on_change') == 'on_change' and previous.get('signature') == signature and previous.get('destination') == str(destination) and Path(previous.get('archive', '')).is_file():
        ctx.log('No file changes; backup skipped')
        return {'files_scanned': len(paths), 'backup_created': 0, 'bytes_written': 0}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    archive = destination / f'backup-{stamp}-{uuid.uuid4().hex[:6]}.zip'
    temporary = archive.with_suffix('.part')
    started = time.monotonic()
    try:
        compression = zipfile.ZIP_DEFLATED if config.get('compression', 'deflated') == 'deflated' else zipfile.ZIP_STORED
        with zipfile.ZipFile(temporary, 'w', compression=compression) as output:
            for path, relative in paths:
                if ctx.stopping.is_set(): return None
                output.write(path, 'files/' + relative)
            output.writestr('sysman-manifest.json', json.dumps({'created': datetime.now(timezone.utc).isoformat(), 'label': config.get('label', ''), 'files': [relative for _, relative in paths], 'sha256': signature}, indent=2))
        temporary.replace(archive)
        state_temporary = state.with_suffix('.tmp')
        state_temporary.write_text(json.dumps({'signature': signature, 'destination': str(destination), 'archive': str(archive)}))
        state_temporary.replace(state)
        ctx.log(f'Created {archive.name}: {len(paths)} files, {archive.stat().st_size} bytes')
        return {'files_scanned': len(paths), 'backup_created': 1, 'bytes_written': archive.stat().st_size, 'duration_ms': round((time.monotonic()-started)*1000)}
    finally:
        temporary.unlink(missing_ok=True)


def run(ctx):
    cycles = backups = errors = bytes_total = 0
    ctx.log('File backup worker started')
    while not ctx.stopping.is_set():
        config = ctx.config
        cycles += 1
        files = duration = 0
        try:
            if config.get('enabled', True):
                result = backup_once(ctx, config)
                if result:
                    backups += result['backup_created']
                    bytes_total += result['bytes_written']
                    files = result['files_scanned']
                    duration = result.get('duration_ms', 0)
            else: ctx.log('Backup jobs disabled in configuration')
        except Exception as error:
            errors += 1
            ctx.log(str(error), 'error')
        ctx.metrics(cycles=cycles, backups_created=backups, files_scanned=files, bytes_written=bytes_total, errors=errors, duration_ms=duration)
        if ctx.wait(max(1, float(config.get('interval', 10)))): break
    ctx.log('File backup worker stopped')
