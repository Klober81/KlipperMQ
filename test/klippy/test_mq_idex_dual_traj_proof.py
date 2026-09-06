# Prove-first harness: IDEX dual-trajectory emit (Marathon-shaped).
# Two [queue] owned_axes x + dual_carriage; interleaved SET_MOTION_QUEUE;
# MultiLookAhead flush/merge by mq_print_time yields one stream where
# both carriage axes advance (not merely two LookAheadQueues).
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
from extras.mq_manager import CARRIAGE_AXES
import toolhead


# Marathon-shaped ownership (sec 4.3): exclusive x / dual_carriage.
# Parks not invented; abstract unit deltas only (no commanded_pos).
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


class CarriageAxisMove:
    """Move stub: records which exclusive carriage axis advances.

    axes_d[0] nonzero = cartesian carriage-rail delta (abstract unit,
    not a park / commanded_pos invent). advance_axis names the
    exclusive owned_axes token (x | dual_carriage).
    """
    def __init__(self, advance_axis, min_move_t=0.01, distance=1.0):
        if advance_axis not in CARRIAGE_AXES:
            raise ValueError("bad advance_axis %r" % (advance_axis,))
        self.advance_axis = advance_axis
        self.min_move_t = min_move_t
        self.distance = float(distance)
        self.axes_d = [self.distance, 0., 0., 0.]
        self.start_pos = (0., 0., 0., 0.)
        self.end_pos = (self.distance, 0., 0., 0.)
        self.timing_callbacks = []
        self.delta_v2 = 1.0
        self.max_start_v2 = 0.
        self.max_cruise_v2 = 1.0
        self.mcr_delta_v2 = 1.0
        self.max_mcr_start_v2 = 0.
        self.is_kinematic_move = True
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


def boot_facade(text=TWO_QUEUE_CFG):
    printer, mq, mgr = load_mgr(text)
    th = StubToolhead()
    printer.objects['toolhead'] = th
    printer.send_event('klippy:connect')
    return printer, mq, mgr, th


def axis_for_active(mgr):
    q = mgr.active_motion_queue
    idx = mgr.carriage_for_queue(q)
    if idx is None:
        raise AssertionError(
            'active queue %s has no carriage axis' % (q.name,))
    return CARRIAGE_AXES[idx]


def enqueue_owned_advance(th, mgr, min_move_t=0.01, distance=1.0):
    axis = axis_for_active(mgr)
    q = mgr.active_motion_queue
    # Ownership check only (not product can_drive gating).
    if not mgr.can_drive(q, axis):
        raise AssertionError(
            'queue %s cannot drive %s' % (q.name, axis))
    move = CarriageAxisMove(
        advance_axis=axis, min_move_t=min_move_t, distance=distance)
    th.lookahead.add_move(move)
    return move


class TestIdexDualTrajEmitProof(unittest.TestCase):
    def test_ownership_maps_x_and_dual_carriage(self):
        printer, mq, mgr, th = boot_facade()
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.assertEqual(mgr.carriage_for_queue(q0), 0)
        self.assertEqual(mgr.carriage_for_queue(q1), 1)
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(
            mgr.ownership.exclusive['dual_carriage'], q1)
        self.assertEqual(CARRIAGE_AXES, ('x', 'dual_carriage'))

    def test_interleaved_flush_both_carriage_axes_advance(self):
        # Domain: two queues + SET_MOTION_QUEUE interleave +
        # MultiLookAhead merge -> one MCU-bound stream where both
        # carriage axes advance (axes_d + advance_axis evidence).
        printer, mq, mgr, th = boot_facade()
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        la0 = mgr.lookaheads[q0.name]
        la1 = mgr.lookaheads[q1.name]

        # q_T0 owns x: advance primary carriage axis
        self.assertIs(mgr.active_motion_queue, q0)
        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=2.0)
        self.assertEqual(m0.advance_axis, 'x')
        self.assertEqual(m0.mq_print_time, 0.)
        self.assertNotEqual(m0.axes_d[0], 0.)
        self.assertEqual(len(la0.queue), 1)
        self.assertEqual(len(la1.queue), 0)

        # Switch active; facade pointer stable; no flush-all
        facade = th.lookahead
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(th.lookahead, facade)
        self.assertIs(mgr.active_motion_queue, q1)

        # q_T1 owns dual_carriage: advance second carriage
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=3.0)
        self.assertEqual(m1.advance_axis, 'dual_carriage')
        self.assertEqual(m1.mq_print_time, 0.)
        self.assertNotEqual(m1.axes_d[0], 0.)
        self.assertEqual(len(la1.queue), 1)

        # Back to q_T0: later mq_print_time on same queue
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        m2 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=4.0)
        self.assertEqual(m2.advance_axis, 'x')
        self.assertEqual(m2.mq_print_time, 0.02)

        # One flush: merge by mq_print_time into one stream
        merged = th.lookahead.flush(lazy=False)
        self.assertEqual(len(merged), 3)
        self.assertTrue(la0.is_empty())
        self.assertTrue(la1.is_empty())
        self.assertTrue(th.lookahead.is_empty())

        axes = [m.advance_axis for m in merged]
        # Tie at t=0: primary (x/q_T0) before dual_carriage
        self.assertEqual(
            axes, ['x', 'dual_carriage', 'x'])
        self.assertEqual(
            [m.mq_print_time for m in merged],
            [0., 0., 0.02])
        # Both carriage axes advanced in single merged stream
        self.assertIn('x', axes)
        self.assertIn('dual_carriage', axes)
        for m in merged:
            self.assertNotEqual(
                m.axes_d[0], 0.,
                'carriage axis %s did not advance'
                % (m.advance_axis,))
            self.assertEqual(abs(m.axes_d[0]), m.distance)

        # Distinct from concurrent_motion "two LAs exist":
        # emit proof - both axes in ONE flush return list.
        self.assertIsInstance(merged, list)
        self.assertEqual(
            {m.advance_axis for m in merged},
            {'x', 'dual_carriage'})

    def test_set_motion_queue_routes_axis_to_child_la(self):
        printer, mq, mgr, th = boot_facade()
        enqueue_owned_advance(th, mgr)
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        enqueue_owned_advance(th, mgr)
        self.assertEqual(
            mgr.lookaheads['q_T0'].queue[0].advance_axis, 'x')
        self.assertEqual(
            mgr.lookaheads['q_T1'].queue[0].advance_axis,
            'dual_carriage')
        # Before flush: two child queues; after: one stream
        before_axes = set()
        for la in mgr.lookaheads.values():
            for m in la.queue:
                before_axes.add(m.advance_axis)
        self.assertEqual(
            before_axes, {'x', 'dual_carriage'})
        merged = th.lookahead.flush(lazy=False)
        self.assertEqual(
            {m.advance_axis for m in merged},
            {'x', 'dual_carriage'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
