from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from scheduling import DEFAULT_SCHEDULE, Scheduler, boundary, plan, validate_schedule
from zoneinfo import ZoneInfo
import server

class ScheduleTests(unittest.TestCase):
    def schedule(self, **changes):
        return validate_schedule({**DEFAULT_SCHEDULE, 'enabled': True, 'timezone': 'UTC', **changes})

    def test_weekday_window_and_exact_boundaries(self):
        value = self.schedule(weekdays=[0])
        self.assertEqual(plan(value, datetime(2026,11,9,8,59,tzinfo=timezone.utc))['desired_status'], 'stopped')
        inside = plan(value, datetime(2026,11,9,9,tzinfo=timezone.utc))
        self.assertEqual(inside['desired_status'], 'running')
        self.assertEqual(inside['next_action'], 'stop')
        self.assertEqual(plan(value, datetime(2026,11,9,17,tzinfo=timezone.utc))['desired_status'], 'stopped')
        self.assertEqual(plan(value, datetime(2026,11,10,10,tzinfo=timezone.utc))['next_run'], '2026-11-16T09:00:00+00:00')

    def test_overnight_belongs_to_start_weekday(self):
        value = self.schedule(weekdays=[0], start_time='22:00', stop_time='06:00')
        self.assertEqual(plan(value, datetime(2026,11,10,2,tzinfo=timezone.utc))['desired_status'], 'running')
        self.assertEqual(plan(value, datetime(2026,11,10,6,tzinfo=timezone.utc))['desired_status'], 'stopped')
        self.assertEqual(plan(value, datetime(2026,11,11,2,tzinfo=timezone.utc))['desired_status'], 'stopped')

    def test_dst_gap_and_repeated_hour(self):
        zone = ZoneInfo('America/Chicago')
        self.assertEqual(boundary(datetime(2026,3,8).date(), '02:30', zone), datetime(2026,3,8,8,tzinfo=timezone.utc))
        self.assertEqual(boundary(datetime(2026,11,1).date(), '01:30', zone), datetime(2026,11,1,6,30,tzinfo=timezone.utc))

    def test_invalid_schedules(self):
        for changes in ({'timezone':'Not/AZone'}, {'weekdays':[]}, {'weekdays':[True]}, {'weekdays':[7]}, {'start_time':'25:00'}, {'stop_time':'09:00'}, {'enabled':'yes'}):
            with self.assertRaises(ValueError): self.schedule(**changes)

    def test_reconcile_persistence_and_manual_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            class Agent:
                status = 'stopped'
                def action(self, action): self.status = 'running' if action == 'start' else 'stopped'
            agent = Agent(); agent.directory = Path(directory)
            service = Scheduler(agent, server.write_json, threading.RLock())
            service.save(self.schedule(weekdays=[0]))
            inside = datetime(2026,11,9,12,tzinfo=timezone.utc)
            service.tick(inside)
            self.assertEqual(agent.status, 'running')
            agent.status = 'paused'; service.tick(inside)
            self.assertEqual(agent.status, 'paused')
            service.tick(datetime(2026,11,9,18,tzinfo=timezone.utc))
            self.assertEqual(agent.status, 'stopped')
            reloaded = Scheduler(agent, server.write_json, threading.RLock())
            self.assertTrue(reloaded.schedule['enabled'])
            self.assertEqual(reloaded.state['last_action'], 'stop')
            reloaded.save({**reloaded.schedule, 'enabled': False})
            reloaded.tick(inside)
            self.assertEqual(agent.status, 'stopped')
