"""Fenced direct-worker data API. No router/model/task invocation on normal traffic."""
import argparse
import json
import os
import time
from pathlib import Path

import broker_bootstrap as broker
from portable import QueueError, encode, number, protocol, read
from routing import Registry


class Worker:
    def __init__(self, registry, credential):
        self.registry = Registry(registry)
        self.credential = credential

    def _ticket(self, ticket):
        if not isinstance(ticket, dict) or ticket.get('routing') != self.credential:
            raise QueueError('ticket_worker_generation_mismatch')
        return ticket['broker_ticket']

    def heartbeat(self, seconds=180):
        number(seconds, .05, 240)
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential)
            assignment = deployment.heartbeat(route['assignment'], 'ready', seconds)
            route.update(assignment=assignment, lease_until=assignment['expires'])
            self.registry.save(state)
            return dict(generation=route['generation'], lease_until=route['lease_until'])

    def claim(self, wait=20, lease=60):
        number(wait, 0, 20); number(lease, .05, 180)
        end = time.monotonic() + wait
        while True:
            with self.registry.locked() as state:
                route, deployment = self.registry._validate_binding(state, self.credential)
                ticket = broker.claim(deployment, route['assignment'], wait=0, lease=lease)
                # Existing broker.claim refreshes role liveness. Commit binding liveness too.
                with deployment.locked() as control:
                    assignment = dict(deployment.current(control, route['assignment']))
                route.update(assignment=assignment, lease_until=assignment['expires'])
                self.registry.save(state)
                if ticket is not None:
                    return dict(routing=self.credential, broker_ticket=ticket)
            if time.monotonic() >= end:
                return None
            time.sleep(min(.1, max(0, end - time.monotonic())))

    def read(self, ticket):
        original = self._ticket(ticket)
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential)
            return broker.begin(deployment, route['assignment'], original)

    def renew(self, ticket, seconds=60):
        original = self._ticket(ticket)
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential)
            renewed = broker.renew(deployment, route['assignment'], original, seconds)
            with deployment.locked() as control:
                assignment = dict(deployment.current(control, route['assignment']))
            route.update(assignment=assignment, lease_until=assignment['expires'])
            self.registry.save(state)
            return dict(routing=self.credential, broker_ticket=renewed)

    def complete(self, ticket, result):
        original = self._ticket(ticket)
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential)
            out = broker.complete(deployment, route['assignment'], original, result)
            return dict(**out, session_key=route['session_key'], generation=route['generation'],
                        worker_instance=route['worker_instance'])

    def status(self, ticket):
        original = self._ticket(ticket)
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential)
            return broker.status(deployment, route['assignment'], original)

    def close(self, code='normal_exit'):
        with self.registry.locked() as state:
            route, deployment = self.registry._validate_binding(state, self.credential, allow_expired=True, allow_closed=True)
            with deployment.locked() as control:
                already_closed = control['roles']['broker']['status'] == 'closed'
            result = dict(idempotent=True, meaning='exact worker was already closed') if already_closed else broker.stop(deployment, route['assignment'], code)
            route.update(phase='closed', closed_at=time.time())
            self.registry.save(state)
            return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry', required=True)
    p.add_argument('--credential', required=True)
    p.add_argument('operation', choices=['heartbeat', 'claim', 'read', 'renew', 'complete', 'status', 'close'])
    p.add_argument('--ticket'); p.add_argument('--result'); p.add_argument('--save')
    p.add_argument('--wait', type=float, default=20); p.add_argument('--lease', type=float, default=60)
    p.add_argument('--code', default='normal_exit')
    args = p.parse_args(); os.umask(0o077)
    worker = Worker(args.registry, read(Path(args.credential), 8192))
    if args.operation == 'claim':
        if not args.save: raise QueueError('save_path_required')
        ticket = worker.claim(args.wait, args.lease)
        if ticket is not None:
            protocol.atomic_json(Path(args.save), ticket, exclusive=True)
            result = dict(job_id=ticket['broker_ticket']['job']['id'], ticket_saved=True, read_required=True,
                          session_key=worker.credential['session_key'], generation=worker.credential['generation'])
        else: result = None
    elif args.operation == 'heartbeat': result = worker.heartbeat(args.lease)
    elif args.operation == 'close': result = worker.close(args.code)
    else:
        if not args.ticket: raise QueueError('ticket_required')
        ticket = read(Path(args.ticket), 1500000)
        if args.operation == 'read': result = worker.read(ticket)
        elif args.operation == 'status': result = worker.status(ticket)
        elif args.operation == 'complete':
            if not args.result: raise QueueError('result_required')
            result = worker.complete(ticket, read(Path(args.result), 131072))
        else:
            if not args.save: raise QueueError('save_path_required')
            renewed = worker.renew(ticket, args.lease)
            protocol.atomic_json(Path(args.save), renewed, exclusive=True)
            result = dict(renewed=True, lease_until=renewed['broker_ticket']['job']['lease_until'])
    print(encode(result).decode())
    return 2 if result is None else 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (QueueError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', type(exc).__name__)}))
        raise SystemExit(1)
