# Short IDEX print-path smoke (Marathon-shaped MQ).
# TOOLCHANGE + SET_MOTION_QUEUE + dual_carriage motion on both
# queues. Parks from [toolchange] only (Formbot X0 / X433).
# CUT: not a full IDEX print demo claim.
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import extras.mq_config as mq_config
import extras.mq_manager as mq_manager
import extras.toolchange as toolchange
from extras.mq_lookahead import MultiLookAhead
from extras.mq_manager import CARRIAGE_AXES
import toolhead


# Marathon evidence (PARK_extruder / PARK_extruder1):
#   PARK_extruder  -> G1 X0
#   PARK_extruder1 -> G1 X433
# Do not invent parks / commanded_pos / shareable.
TWO_QUEUE_TC_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2

[queue q_T0]
owned_axes: x
extruder: extrude

[queue q_T1]
owned_axes: dual_carriage
extruder: extruder1

[toolchange T0]
park_x: 0

[toolchange T1]
park_x: 433
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
    def send_event(self, event, *params):
        for cb in self._handlers.get(event, []):
            cb(*params)


class DummyGCode:
    def __init__(self):
        self.commands = {}
        self.scripts = []
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)
    def run_script_from_command(self, script):
        self.scripts.append(script)


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


class DummyDualCarriage:
    pass


class StubToolhead:
    def __init__(self):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False


class CarriageAxisMove:
    """Abstract carriage-rail delta; no park / commanded_pos invent."""
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


def _toolchange_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl == 'toolchange' or sl.startswith('toolchange '):
            sections.append(name)
    return sections


def load_tc(text, with_toolhead=False):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    printer.objects['dual_carriage'] = DummyDualCarriage()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    th = None
    if with_toolhead:
        th = StubToolhead()
        printer.objects['toolhead'] = th
    obj = None
    for name in _toolchange_sections(fileconfig):
        wrap = config.getsection(name)
        parts = name.split()
        if len(parts) > 1:
            obj = toolchange.load_config_prefix(wrap)
        else:
            obj = toolchange.load_config(wrap)
        printer.objects[name] = obj
    printer.send_event('klippy:connect')
    return printer, mq, mgr, obj, th


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
    if not mgr.can_drive(q, axis):
        raise AssertionError(
            'queue %s cannot drive %s' % (q.name, axis))
    move = CarriageAxisMove(
        advance_axis=axis, min_move_t=min_move_t, distance=distance)
    th.lookahead.add_move(move)
    return move


class TestIdexPrintpathSmoke(unittest.TestCase):
    """Short dual-carriage print-like path on both queues."""

    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def test_printpath_toolchange_and_both_carriages(self):
        # Print-like CUT: T0 print segment -> TOOLCHANGE T1
        # (park X0 + both queues) -> T1 print segment ->
        # TOOLCHANGE T0 (park X433) -> T0 print segment.
        # Flush merge shows both carriage axes advanced.
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')

        # --- segment 0: activate T0, short print on x ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        self.assertIs(mgr.active_motion_queue, q0)
        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=5.0)
        self.assertEqual(m0.advance_axis, 'x')

        # --- toolchange to T1: outgoing park X0, both queues ---
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        script_t1 = gcode.scripts[-1]
        self.assertIn('G1 X0', script_t1)
        self.assertNotIn('X434', script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t1)
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=0', script_t1)
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=1', script_t1)
        # Product emit may include ACTIVATE_EXTRUDER; do not invent.
        self.assertLess(
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T0'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        self.assertLess(
            script_t1.find('G1 X0'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))

        # --- segment 1: short print on dual_carriage ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(mgr.active_motion_queue, q1)
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=6.0)
        self.assertEqual(m1.advance_axis, 'dual_carriage')

        # --- toolchange back to T0: outgoing park X433 ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        script_t0 = gcode.scripts[-1]
        self.assertIn('G1 X433', script_t0)
        self.assertNotIn('X434', script_t0)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t0)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script_t0)

        # --- segment 2: short print on x again ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        m2 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=4.0)
        self.assertEqual(m2.advance_axis, 'x')

        # One flush: both carriages in merged print-like stream
        merged = th.lookahead.flush(lazy=False)
        axes = [m.advance_axis for m in merged]
        self.assertEqual(len(merged), 3)
        self.assertIn('x', axes)
        self.assertIn('dual_carriage', axes)
        self.assertEqual(
            {m.advance_axis for m in merged},
            {'x', 'dual_carriage'})
        for m in merged:
            self.assertNotEqual(
                m.axes_d[0], 0.,
                'carriage axis %s did not advance'
                % (m.advance_axis,))
        # Order: T0 print, T1 print, T0 print (tie: x before dual)
        self.assertEqual(
            axes, ['x', 'dual_carriage', 'x'])

    def test_printpath_ownership_parks_formbot_only(self):
        # Config parks are Formbot X0 / X433 only; ownership maps
        # x <-> q_T0 and dual_carriage <-> q_T1.
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        self.assertTrue(mgr.ownership.multi_queue)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.assertEqual(mgr.carriage_for_queue(q0), 0)
        self.assertEqual(mgr.carriage_for_queue(q1), 1)
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(
            mgr.ownership.exclusive['dual_carriage'], q1)
        t0 = obj._by_name['t0']
        t1 = obj._by_name['t1']
        self.assertEqual(t0.park_x, 0)
        self.assertEqual(t1.park_x, 433)
        self.assertIsNone(t0.park_y)
        self.assertIsNone(t1.park_y)


if __name__ == '__main__':
    unittest.main(verbosity=2)
