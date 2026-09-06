# Concurrent overlapping park/unpark during TOOLCHANGE.
# Outgoing park + incoming unpark via SET_MOTION_QUEUE + MultiLookAhead
# merge. Parks from [toolchange Tn] park_x only (Marathon X0 / X433).
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


# Marathon evidence (klippy-merged.cfg PARK_extruder / PARK_extruder1):
#   PARK_extruder  -> G1 X0
#   PARK_extruder1 -> G1 X433
# X434 is brush/PZProbe path - not park; do not invent.
TWO_QUEUE_TC_CFG = """
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

[toolchange T0]
park_x: 0

[toolchange T1]
park_x: 433
"""

STOCK_TC_CFG = (
    "[toolchange T0]\npark_x: 0\n"
    "[toolchange T1]\npark_x: 433\n"
)


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


class TestOverlapParkToolchangeProof(unittest.TestCase):
    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def test_queues_merge_baseline_still_green(self):
        # Queues path unchanged: manual SET_MOTION_QUEUE interleave
        # + MultiLookAhead merge still advances both carriage axes.
        # Owner of today TOOLCHANGE gap is Tool Coordination, not
        # Queues / LA merge.
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True)
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)

        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=2.0)
        self.assertEqual(m0.advance_axis, 'x')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=3.0)
        self.assertEqual(m1.advance_axis, 'dual_carriage')
        merged = th.lookahead.flush(lazy=False)
        axes = [m.advance_axis for m in merged]
        self.assertEqual(axes, ['x', 'dual_carriage'])
        self.assertEqual(
            [m.mq_print_time for m in merged], [0., 0.])
        self.assertEqual(
            {m.advance_axis for m in merged},
            {'x', 'dual_carriage'})

    def test_toolchange_emits_set_motion_queue_both_queues(self):
        # T0->T1 overlapping park/unpark must emit
        # SET_MOTION_QUEUE for outgoing + incoming queues and park
        # X from config (Marathon T0 park G1 X0).
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        # Outgoing park from [toolchange T0] park_x: 0
        self.assertIn('G1 X0', script)
        # Concurrent emit: both queues selected in one TOOLCHANGE
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script)
        # Park before / with queue switch - not invent ACTIVATE_*
        self.assertLess(
            script.find('SET_MOTION_QUEUE QUEUE=q_T0'),
            script.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        # Outgoing park before incoming activate
        self.assertLess(
            script.find('G1 X0'),
            script.find('ACTIVATE_EXTRUDER'))

    def test_toolchange_t1_to_t0_park_x433_both_queues(self):
        # Reverse: outgoing T1 parks at Marathon X433; both queues
        # still selected for overlap.
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T1')
        self.toolchange(obj, 'T0')
        script = gcode.scripts[-1]
        self.assertIn('G1 X433', script)
        self.assertNotIn('X434', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script)

    def test_stock_no_queue_sequential_still_green(self):
        # Stock / no-[queue]: sequential TOOLCHANGE stays valid;
        # no SET_MOTION_QUEUE required.
        printer, mq, mgr, obj, th = load_tc(STOCK_TC_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('SET_MOTION_QUEUE', script)
        self.assertEqual(obj.current.name, 'T1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
