"""Isolated agent worker. The supervisor owns process lifecycle and backups."""
import importlib.util
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

class Context:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())

    @property
    def config(self):
        return json.loads((self.directory / 'config.json').read_text())

    def emit(self, kind, **values):
        print(json.dumps({'kind': kind, **values}), flush=True)

    def log(self, message, level='info'):
        self.emit('log', message=str(message), level=level)

    def metrics(self, **values):
        self.emit('metrics', metrics={k: v for k, v in values.items() if isinstance(v, (int, float))})

    def wait(self, seconds):
        return self.stopping.wait(seconds)

if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(directory))
    # Disable bytecode so code replacement always imports the latest source.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('agent_main', directory / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = Context(directory)
    try:
        module.run(context)
    except Exception as error:
        context.log(str(error), 'error')
        raise
