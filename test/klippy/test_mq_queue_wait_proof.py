# Prove-first harness: QUEUE_WAIT / QUEUE_SYNC (Philosophy #1).
# Commands block until named queue(s) idle (barrier for SYNC).
# Idle = child LA empty and stock print_time past per-queue trapq
# end. Stock no-[queue] path does not register the commands.
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


class StubMCU:
    def __init__(self, est=0.):
        self._est = est
    def estimated_print_time(self, eventtime):
        return self._est


class StubReactor:
    def __init__(self, on_pause=None):
        self.pauses = 0
        self._on_pause = on_pause
        self._mono = 0.
    def monotonic(self):
        return self._mono
    def pause(self, waketime):
        self.pauses += 1
        self._mono = waketime
        if self._on_pause is not None:
            self._on_pause(self)
        return waketime


class StubToolhead:
    def __init__(self, reactor=None, est=0.):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False
        self.reactor = reactor if reactor is not None else StubReactor()
        self.mcu = StubMCU(est=est)
        self.print_time = 0.
        self.can_pause = True


class StubMove:
    def __init__(self, tag, min_move_t=0.01):
        self.tag = tag
        self.min_move_t = min_move_t
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


class TestQueueWaitSyncProof(unittest.TestCase):
    def test_stock_no_queue_unchanged_no_wait_commands(self):
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.lookaheads, {})
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('QUEUE_WAIT', gcode.commands)
        self.assertNotIn('QUEUE_SYNC', gcode.commands)
        self.assertNotIn('SET_MOTION_QUEUE', gcode.commands)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        stock_la = th.lookahead
        printer.send_event('klippy:connect')
        self.assertIs(th.lookahead, stock_la)
        self.assertNotIsInstance(th.lookahead, MultiLookAhead)

    def test_multi_queue_registers_wait_and_sync(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        self.assertTrue(mgr.ownership.multi_queue)
        gcode = printer.lookup_object('gcode')
        self.assertIn('QUEUE_WAIT', gcode.commands)
        self.assertIn('QUEUE_SYNC', gcode.commands)
        self.assertIn('SET_MOTION_QUEUE', gcode.commands)
        src = read_src('klippy', 'extras', 'mq_manager.py')
        self.assertIn('QUEUE_WAIT', src)
        self.assertIn('QUEUE_SYNC', src)
        # BLOCK: wait/sync path must not gate on can_drive.
        wait_src = src[src.find('def cmd_QUEUE_WAIT'):]
        self.assertNotIn('can_drive', wait_src)

    def test_busy_while_child_la_has_moves(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.assertFalse(mgr.queue_is_busy(q0, th))
        self.assertFalse(mgr.queue_is_busy(q1, th))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('q_T1'))
        self.assertFalse(mgr.lookaheads[q1.name].is_empty())
        self.assertTrue(mgr.queue_is_busy(q1, th))
        self.assertFalse(mgr.queue_is_busy(q0, th))

    def test_busy_while_trapq_end_ahead_of_est(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead(est=5.0)
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        q1 = mgr.lookup_queue('q_T1')
        self.assertFalse(mgr.queue_is_busy(q1, th))
        mgr._trapq_end[q1.name] = 10.0
        self.assertTrue(mgr.queue_is_busy(q1, th))
        th.mcu._est = 10.0
        self.assertFalse(mgr.queue_is_busy(q1, th))

    def test_queue_wait_does_not_return_while_la_busy(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        q1 = mgr.lookup_queue('q_T1')

        def on_pause(reactor):
            # Still busy on first pause; clear LA on second.
            if reactor.pauses >= 2:
                mgr.lookaheads[q1.name].reset()

        reactor = StubReactor(on_pause=on_pause)
        th = StubToolhead(reactor=reactor, est=0.)
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('q_T1'))
        self.assertTrue(mgr.queue_is_busy(q1, th))
        mgr.cmd_QUEUE_WAIT(DummyGCmd(QUEUE='q_T1'))
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q1, th))
        self.assertTrue(mgr.lookaheads[q1.name].is_empty())

    def test_queue_wait_does_not_return_while_trapq_busy(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        q1 = mgr.lookup_queue('q_T1')

        def on_pause(reactor):
            # Advance estimated print_time past trapq end.
            if reactor.pauses >= 2:
                th.mcu._est = 20.0

        reactor = StubReactor(on_pause=on_pause)
        th = StubToolhead(reactor=reactor, est=5.0)
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        mgr._trapq_end[q1.name] = 15.0
        self.assertTrue(mgr.queue_is_busy(q1, th))
        mgr.cmd_QUEUE_WAIT(DummyGCmd(QUEUE='q_T1'))
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q1, th))

    def test_queue_sync_barrier_waits_all_queues(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')

        def on_pause(reactor):
            if reactor.pauses == 1:
                mgr.lookaheads[q0.name].reset()
            if reactor.pauses >= 2:
                mgr.lookaheads[q1.name].reset()

        reactor = StubReactor(on_pause=on_pause)
        th = StubToolhead(reactor=reactor)
        printer.objects['toolhead'] = th
        printer.send_event('klippy:connect')
        # Primary active gets first move; then q_T1.
        th.lookahead.add_move(StubMove('q_T0'))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('q_T1'))
        self.assertTrue(mgr.queue_is_busy(q0, th))
        self.assertTrue(mgr.queue_is_busy(q1, th))
        mgr.cmd_QUEUE_SYNC(DummyGCmd())
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q0, th))
        self.assertFalse(mgr.queue_is_busy(q1, th))

    def test_no_toolhead_py_edits_for_wait(self):
        # Hygiene BLOCK: avoid stock toolhead.py edits.
        src = read_src('klippy', 'toolhead.py')
        self.assertNotIn('QUEUE_WAIT', src)
        self.assertNotIn('QUEUE_SYNC', src)
        self.assertNotIn('mq_manager', src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
