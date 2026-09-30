"""Offline two-role protocol demo. Does not use Drive or invoke native inference."""
import argparse
import json
from pathlib import Path
from . import deployment, LocalFSBackend, Journal, Controller, Worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='new private demo directory; must not exist')
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    pin = deployment('offline-demo', 'synthetic/native-context')
    backend = LocalFSBackend(root/'object-store',create=True)
    controller = Controller(Journal.provision(root/'controller-journal',pin,'controller'),backend)
    worker = Worker(Journal.provision(root/'worker-journal',pin,'worker'),backend)
    controller.publish_deployment()
    turns = []
    for n in range(2):
        request = controller.submit('Synthetic transport question '+str(n+1), 'demo-'+str(n+1))
        permit = worker.start_next()
        # Intentionally synthetic, never a model/tool result claim.
        result = worker.complete(permit, 'Synthetic transport answer '+str(n+1))
        observed = controller.result(request)
        assert observed.oid == result
        receipt = controller.record_delivery(request,result,'offline-demo-readback-'+str(n+1))
        turns.append(dict(request=request,result=result,receipt=receipt))
    print(json.dumps(dict(validation='offline_protocol_only',native_calls=0,drive_calls=0,
                         deployment=pin.oid,turns=turns),indent=2))


if __name__ == '__main__':
    main()
