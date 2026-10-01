"""Versioned controller timing. No timers, network, renewal, or native activity.

Signed event times use the trusted controller host wall clock, never provider
modifiedTime. A 180-second liveness allowance does not extend a session lease.
"""
from .model import require

TIMING = {'contract': 'dots-global-timing/1', 'freshness_seconds': 180,
          'heartbeat_interval_seconds': 25, 'operation_budget_seconds': 120,
          'spawn_check_seconds': 10}

def validate_timing(value):
    require(isinstance(value, dict) and value == TIMING
            and all(type(value[k]) is int for k in TIMING if k != 'contract'),
            'unsupported_global_timing_policy')
    return value

def controller_active(heartbeat, expires, timing, now):
    validate_timing(timing)
    return expires > now and 0 <= now - heartbeat < timing['freshness_seconds']

def require_active(controller, timing, now, *, initialized=False):
    require(controller is not None and controller_active(controller['heartbeat_at'],
            controller['lease_expires'], timing, now), 'global_controller_not_active')
    if initialized:
        require(controller['heartbeat_at'] > controller['joined_at'],
                'global_first_heartbeat_required')
    return controller

def event_deadline(state, kind, at):
    """Latest time an active agent may dispatch/accept this prepared CAS."""
    timing = validate_timing(state['controller_timing'])
    deadline = min(state['expires'], at + timing['operation_budget_seconds'])
    c = state['logical']['controller']
    if kind not in ('join', 'demand', 'ready', 'close') and c is not None:
        deadline = min(deadline, c['lease_expires'],
                       c['heartbeat_at'] + timing['freshness_seconds'])
    return deadline


def observation_window_current(state, observed_events, now):
    """A newly observed native event cannot be adopted after its preparation budget.

    Old events already checked are history. Seeing them again never renews time.
    """
    native=[event for event in state['events'][observed_events:] if event['actor']=='native']
    return not native or now < native[-1]['at'] + state['controller_timing']['operation_budget_seconds']
