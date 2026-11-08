"""Persistent, timezone-aware weekly worker windows, reconciled by SysMan."""
from datetime import datetime, timedelta, timezone
import re
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SCHEDULE = {'enabled': False, 'timezone': 'America/Chicago', 'weekdays': list(range(7)),
                    'start_time': '09:00', 'stop_time': '17:00'}
ICONS = ('agents', 'mail', 'backups', 'activity', 'clock', 'database', 'shield', 'globe', 'folder', 'code')


def validate_schedule(value):
    if not isinstance(value, dict): raise ValueError('Schedule must be an object')
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool): raise ValueError('Enabled must be a checkbox value')
    name = value.get('timezone', '')
    if not isinstance(name, str): raise ValueError('Enter a valid IANA timezone')
    try: ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError): raise ValueError('Enter a valid IANA timezone, such as America/Chicago') from None
    days = value.get('weekdays')
    if not isinstance(days, list) or not days or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError('Choose at least one weekday')
    for key in ('start_time', 'stop_time'):
        if not isinstance(value.get(key), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value[key]):
            raise ValueError('Start and stop times must use HH:MM')
    if value['start_time'] == value['stop_time']: raise ValueError('Start and stop times must differ')
    return {'enabled': enabled, 'timezone': name, 'weekdays': sorted(set(days)),
            'start_time': value['start_time'], 'stop_time': value['stop_time']}


def boundary(day, wall_time, zone):
    """First fold for repeated times; move nonexistent times to first valid minute."""
    hour, minute = map(int, wall_time.split(':'))
    local = datetime(day.year, day.month, day.day, hour, minute)
    for _ in range(1441):
        candidate = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == local: return candidate
        local += timedelta(minutes=1)
    raise ValueError('Unable to resolve schedule time in this timezone')


def plan(schedule, current=None):
    if not schedule['enabled']:
        return {'desired_status': None, 'next_run': None, 'next_action': None}
    current = current or datetime.now(timezone.utc)
    zone = ZoneInfo(schedule['timezone'])
    today = current.astimezone(zone).date()
    events = []
    overnight = schedule['stop_time'] < schedule['start_time']
    for offset in range(-8, 9):
        day = today + timedelta(days=offset)
        if day.weekday() not in schedule['weekdays']: continue
        start = boundary(day, schedule['start_time'], zone)
        end = boundary(day + timedelta(days=int(overnight)), schedule['stop_time'], zone)
        # A spring-forward gap can collapse a short window. Skip that day's window.
        if end <= start: continue
        events.extend([(start, 'start'), (end, 'stop')])
    events.sort(key=lambda item: (item[0], item[1] == 'stop'))
    previous = [event for event in events if event[0] <= current]
    following = [event for event in events if event[0] > current]
    return {'desired_status': 'running' if previous and previous[-1][1] == 'start' else 'stopped',
            'next_run': following[0][0].isoformat() if following else None,
            'next_action': following[0][1] if following else None}


class Scheduler:
    """One non-overlapping loop per agent, so an offline runner cannot delay others."""
    def __init__(self, agent, save_json, platform_lock, interval=15):
        self.agent, self.save_json, self.platform_lock = agent, save_json, platform_lock
        self.path = agent.directory / 'schedule.json'
        self.lock = threading.RLock()
        self.execution_lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.interval = interval
        import json
        self.record = json.loads(self.path.read_text()) if self.path.exists() else {'schedule': dict(DEFAULT_SCHEDULE), 'state': {}}
        self.schedule = validate_schedule(self.record['schedule'])
        self.state = self.record.get('state', {})

    def snapshot(self):
        with self.lock:
            return {'schedule': self.schedule.copy(), 'schedule_state': {**self.state, **plan(self.schedule)}}

    def save(self, value):
        value = validate_schedule(value)
        with self.execution_lock, self.lock:
            # Persist before replacing an active schedule.
            self.save_json(self.path, {'schedule': value, 'state': {}})
            self.schedule, self.state = value, {}
        return self.snapshot()

    def tick(self, current=None):
        with self.execution_lock:
            with self.lock:
                target = plan(self.schedule, current)['desired_status']
            if target is None: return
            moment = (current or datetime.now(timezone.utc)).isoformat()
            try:
                # Import lazily to keep the supervisor and adapter independent.
                from remote_agent import RemoteAgent
                remote = isinstance(self.agent, RemoteAgent)
                if remote:
                    self.agent.sync()
                    status = self.agent.snapshot()['status']
                else: status = self.agent.status
                action = None
                if target == 'running' and status not in ('running', 'paused'): action = 'start'
                elif target == 'stopped' and status in ('running', 'paused'): action = 'stop'
                if action:
                    if remote: self.agent.request('POST', '/api/agent/action', {'action': action})
                    else:
                        # Match local API lock order, avoiding concurrent lifecycle commands.
                        with self.platform_lock: self.agent.action(action)
                    self.state.update(last_run=moment, last_action=action)
                    self.state['last_error'] = None
                    self.save_json(self.path, {'schedule': self.schedule, 'state': self.state})
                elif self.state.get('last_error'):
                    self.state['last_error'] = None
                    self.save_json(self.path, {'schedule': self.schedule, 'state': self.state})
            except (ValueError, OSError, TypeError) as error:
                self.state.update(last_error=str(error), last_attempt=moment)
                self.save_json(self.path, {'schedule': self.schedule, 'state': self.state})

    def start(self):
        if self.thread: return
        def loop():
            while not self.closed.is_set():
                try: self.tick()
                except OSError: pass  # A disk failure must not kill scheduling for other agents.
                if self.closed.wait(self.interval): break
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()
        if self.thread: self.thread.join(timeout=20)
