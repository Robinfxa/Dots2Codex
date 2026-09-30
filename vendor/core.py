"""Load only the byte-identical audited file bridge implementation."""
import hashlib,sys
from pathlib import Path
BASE=Path(__file__).resolve().parent/'bridge_core'
FROZEN={'file_queue.py':'e2ceb466da2aac3164cfa89541f4ee561ddcb0521a82fa03d1697559a1772246','facade.py':'afd6990c20e282577c89b22f81ae0e29655773a41710ae9f6409568ff0177d5e','reference/bridge.py':'1e89ec2084ea2a8c81e1d1ca3f76e9b7262da0e98c2cdf0114fb52bd716911f9'}
for n,h in FROZEN.items():
 if hashlib.sha256((BASE/n).read_bytes()).hexdigest()!=h:raise RuntimeError('frozen_core_changed')
sys.path.insert(0,str(BASE))
from file_queue import FileQueue,QueueError,protocol,read,digest
from facade import LocalFacade,ResponsesHandler
