# Prove-first harness: MultiLookAhead flush/merge cut.
# Facade installs on multi_queue connect; SET_MOTION_QUEUE changes
# active child only (no flush-all, no pointer swap). flush drains
# every child LA and merges by ascending mq_print_time (stamped on
# add_move from per-queue next_t; never Move.print_time).
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import extras.mq_config as mq_config
import extras.mq_manager as mq_manager
from extras.mq_lookahead import MultiLookAhead
import toolhead


TWO_QUEUE_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2

[queue q_T0]
owned_axes: x
extruder: extruder

[queue q_T1]
owned_axes: dual_carriage
extruder: extruder1
"""

STOCK_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
"""


class DummyPrinter:
    config_error = configfile.error
    def __init__(self):
        self.objects = collections.OrderedDict()
        self._handlers = {}
    def add_object(self, name, obj):
        if name in self.objects:
            raise self.config_error(
                "Printer object '%s' already created" % (name,))
        self.objects[name] = obj
    def lookup_object(self, name, default=configfile.sentinel):
        if name in self.objects:
            return self.objects[name]
        if default is configfile.sentinel:
            raise self.config_error(
                "Unknown config object '%s'" % (name,))
        return default
    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + ' '
        objs = [(n, self.objects[n])
                for n in self.objects if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs
    def register_event_handler(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)
    def send_event(self, event):
        for cb in self._handlers.get(event, []):
            cb()


class DummyGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)


class DummyGCmd:
    def __init__(self, **params):
        self._params = dict(
            (k.upper(), str(v)) for k, v in params.items())
    def get(self, name, default=None):
        key = name.upper()
        if key in self._params:
            return self._params[key]
        if default is not None:
            return default
        raise configfile.error(
            "Error on command: missing %s" % (name,))
    def error(self, msg):
        raise configfile.error(msg)


class StubToolhead:
    def __init__(self):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False


class StubMove:
    def __init__(self, tag, min_move_t=0.01, print_time=None):
        self.tag = tag
        self.min_move_t = min_move_t
        # Optional decoy: must NOT drive flush merge key.
        if print_time is not None:
            self.print_time = print_time
        self.timing_callbacks = []
        self.delta_v2 = 1.0
        self.max_start_v2 = 0.
        self.max_cruise_v2 = 1.0
        self.mcr_delta_v2 = 1.0
        self.max_mcr_start_v2 = 0.
        self.is_kinematic_move = False
    def calc_junction(self, prev):
        return
    def set_junction(self, start_v2, cruise_v2, end_v2):
        self.start_v = 0.
        self.cruise_v = 1.
        self.end_v = 0.
        self.accel_t = 0.
        self.cruise_t = self.min_move_t
        self.decel_t = 0.


def load_mgr(text):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    return printer, mq, mgr


def read_src(*parts):
    path = os.path.join(ROOT, *parts)
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestFlushMergeProof(unittest.TestCase):
    def test_stock_no_queue_no_facade_one_la(self):
        # Stock no-[queue]: never install facade; one LA.
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.lookaheads, {})
        th = StubToolhead()
        printer.objects['toolhead'] = th
        stock_la = th.lookahead
        printer.send_event('klippy:connect')
        self.assertIs(th.lookahead, stock_la)
        self.assertNotIsInstance(th.lookahead, MultiLookAhead)
        self.assertFalse(hasattr(mgr, 'multi_lookahead'))

    def test_two_queues_flush_merges_by_mq_print_time(self):
        # Stamp path: each queue next_t starts at 0; first adds tie
        # then primary-first. Decoy print_time must not reorder.
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIs(th.lookahead, mgr.multi_lookahead)

        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        la0 = mgr.lookaheads[q0.name]
        la1 = mgr.lookaheads[q1.name]

        # Primary active: enqueue on q_T0 (stamp 0)
        m0 = StubMove('q_T0', print_time=99.0)
        th.lookahead.add_move(m0)
        self.assertEqual(m0.mq_print_time, 0.)
        self.assertEqual(len(la0.queue), 1)
        self.assertEqual(len(la1.queue), 0)

        # Switch active; no flush-all; facade pointer stable
        facade = th.lookahead
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(th.lookahead, facade)
        self.assertIs(mgr.active_motion_queue, q1)
        self.assertEqual(len(la0.queue), 1)

        # Decoy print_time=1 would sort first if print_time were key
        m1 = StubMove('q_T1', print_time=1.0)
        th.lookahead.add_move(m1)
        self.assertEqual(m1.mq_print_time, 0.)
        self.assertEqual(len(la1.queue), 1)

        merged = th.lookahead.flush(lazy=False)
        # Equal mq_print_time: primary (q_T0) before q_T1
        self.assertEqual(
            [m.tag for m in merged], ['q_T0', 'q_T1'])
        self.assertTrue(la0.is_empty())
        self.assertTrue(la1.is_empty())
        self.assertTrue(th.lookahead.is_empty())

    def test_within_queue_later_adds_sort_later(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        m_a = StubMove('a', min_move_t=0.02)
        m_b = StubMove('b', min_move_t=0.02)
        th.lookahead.add_move(m_a)
        th.lookahead.add_move(m_b)
        self.assertEqual(m_a.mq_print_time, 0.)
        self.assertEqual(m_b.mq_print_time, 0.02)
        merged = th.lookahead.flush(lazy=False)
        self.assertEqual([m.tag for m in merged], ['a', 'b'])

    def test_inactive_queue_drains_without_reselect(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        # Park moves on both children via add_move + select
        th.lookahead.add_move(StubMove('early_T0'))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('early_T1'))
        # Stay on q_T1; flush must still drain inactive q_T0
        merged = th.lookahead.flush(lazy=False)
        # Per-queue next_t both 0: primary-first tie-break
        self.assertEqual(
            [m.tag for m in merged],
            ['early_T0', 'early_T1'])
        self.assertTrue(mgr.lookaheads[q0.name].is_empty())
        self.assertTrue(mgr.lookaheads[q1.name].is_empty())
        # Still on q_T1; no re-select required
        self.assertIs(mgr.active_motion_queue, q1)

    def test_set_motion_queue_does_not_flush_all(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        th.lookahead.add_move(StubMove('keep'))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertEqual(
            len(mgr.lookaheads['q_T0'].queue), 1)

    def test_tie_break_primary_then_queue_name(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        th.lookahead.add_move(StubMove('T0'))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('T1'))
        merged = th.lookahead.flush(lazy=False)
        # Same mq_print_time: primary (q_T0) before q_T1
        self.assertEqual([m.tag for m in merged], ['T0', 'T1'])

    def test_drip_primary_only_no_multi_merge(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        # Seed inactive child
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('inactive'))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        th._in_drip = True
        # Explicit gate (not buried _in_drip alone)
        self.assertTrue(mgr.homing_uses_primary_only)
        self.assertTrue(mgr.is_drip_or_homing(th))
        self.assertIs(mgr.drip_queue(th), mgr.primary)
        th.lookahead.add_move(StubMove('drip'))
        out = th.lookahead.flush(lazy=False)
        self.assertEqual([m.tag for m in out], ['drip'])
        # Inactive child untouched
        self.assertEqual(
            len(mgr.lookaheads['q_T1'].queue), 1)
        th.lookahead.reset()
        self.assertTrue(mgr.lookaheads['q_T0'].is_empty())
        self.assertEqual(
            len(mgr.lookaheads['q_T1'].queue), 1)

    def test_can_drive_absent_from_toolhead_gcode_move(self):
        for parts in (('klippy', 'toolhead.py'),
                      ('klippy', 'extras', 'gcode_move.py')):
            src = read_src(*parts)
            self.assertNotIn(
                'can_drive', src, '/'.join(parts))

    def test_no_toolhead_process_lookahead_monkeypatch(self):
        # BLOCK: monkey-patch _process_lookahead; keep stock consumer.
        mgr_src = read_src('klippy', 'extras', 'mq_manager.py')
        la_src = read_src('klippy', 'extras', 'mq_lookahead.py')
        for src in (mgr_src, la_src):
            self.assertNotIn('_process_lookahead', src)
        th = read_src('klippy', 'toolhead.py')
        # Zero toolhead edits for this cut
        self.assertNotIn('MultiLookAhead', th)
        self.assertNotIn('mq_lookahead', th)
        self.assertNotIn('mq_manager', th)

    def test_no_print_time_getattr_merge_key(self):
        # Philosophy BLOCK: never getattr(move, "print_time", ...)
        la_src = read_src('klippy', 'extras', 'mq_lookahead.py')
        self.assertNotIn('getattr(move, "print_time"', la_src)
        self.assertNotIn("getattr(move, 'print_time'", la_src)
        self.assertIn('mq_print_time', la_src)
        self.assertIn('Move.print_time', la_src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
