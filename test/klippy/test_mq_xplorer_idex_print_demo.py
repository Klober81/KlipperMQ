# Longer Xplorer-shaped IDEX print-path demo (MQ).
# Multiple TOOLCHANGE parks/unparks + SET_MOTION_QUEUE dual-traj
# merge + QUEUE_CLAIM/RELEASE for leftover shared Y (Z if touched).
# Parks from [toolchange] only (Xplorer X0 / X421). CUT: not a
# production / full print claim.
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


# Xplorer IDEX evidence (mq-idex.cfg / stock PARK macros):
#   PARK_extruder  -> G1 X0   (CARRIAGE=0)
#   PARK_extruder1 -> G1 X421 (CARRIAGE=1)
# Do NOT use Marathon X433, stock dual_carriage X200, or
# 2xGantry X419/Y449.
# Overlay queues: [queue] owned_axes x; [queue q_T1] dual_carriage.
# Leftover Y/Z need QUEUE_CLAIM before shared-axis moves.
# Do not invent parks / commanded_pos / shareable product config.
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
park_x: 421
"""

STOCK_TC_CFG = (
    "[toolchange T0]\npark_x: 0\n"
    "[toolchange T1]\npark_x: 421\n"
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


class SharedAxisMove:
    """Abstract leftover shared-axis delta (Y or Z). No parks invent."""
    def __init__(self, axis, min_move_t=0.01, distance=1.0):
        axis = axis.lower()
        if axis not in ('y', 'z'):
            raise ValueError("bad shared axis %r" % (axis,))
        self.advance_axis = axis
        self.min_move_t = min_move_t
        self.distance = float(distance)
        # axes_d layout: [x, y, z, e] abstract unit deltas
        self.axes_d = [0., 0., 0., 0.]
        idx = 1 if axis == 'y' else 2
        self.axes_d[idx] = self.distance
        self.start_pos = (0., 0., 0., 0.)
        end = [0., 0., 0., 0.]
        end[idx] = self.distance
        self.end_pos = tuple(end)
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
    # Ownership check only (not product can_drive gating).
    if not mgr.can_drive(q, axis):
        raise AssertionError(
            'queue %s cannot drive %s' % (q.name, axis))
    move = CarriageAxisMove(
        advance_axis=axis, min_move_t=min_move_t, distance=distance)
    th.lookahead.add_move(move)
    return move


def enqueue_shared_advance(th, mgr, axis, min_move_t=0.01,
                           distance=1.0):
    q = mgr.active_motion_queue
    if not mgr.can_drive(q, axis):
        raise AssertionError(
            'queue %s cannot drive shared %s (claim first?)'
            % (q.name, axis))
    move = SharedAxisMove(
        axis=axis, min_move_t=min_move_t, distance=distance)
    th.lookahead.add_move(move)
    return move


class TestXplorerIdexPrintDemo(unittest.TestCase):
    """Longer dual-carriage print-like path + shared Y claim."""

    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def claim(self, mgr, queue, axis):
        mgr.cmd_QUEUE_CLAIM(DummyGCmd(QUEUE=queue, AXIS=axis))

    def release(self, mgr, queue, axis):
        mgr.cmd_QUEUE_RELEASE(DummyGCmd(QUEUE=queue, AXIS=axis))

    def assert_park_script(self, script, park_x, out_q, in_q):
        self.assertIn('G1 X%d' % (park_x,), script)
        self.assertNotIn('X433', script)
        self.assertNotIn('X200', script)
        self.assertNotIn('X419', script)
        self.assertNotIn('X434', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=%s' % (out_q,), script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=%s' % (in_q,), script)
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=0', script)
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=1', script)
        self.assertLess(
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (out_q,)),
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (in_q,)))
        self.assertLess(
            script.find('G1 X%d' % (park_x,)),
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (in_q,)))

    def test_print_demo_multi_toolchange_shared_y(self):
        # Longer Xplorer-shaped CUT (not production print):
        #   T0 print + claim Y shared move + release
        #   -> TOOLCHANGE T1 (park X0) -> T1 print
        #   -> TOOLCHANGE T0 (park X421) -> T0 print + claim Z
        #   -> TOOLCHANGE T1 (park X0) -> T1 print + claim Y
        #   -> flush: both carriages + shared axes in merge
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)
        self.assertIn('QUEUE_CLAIM', gcode.commands)
        self.assertIn('QUEUE_RELEASE', gcode.commands)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        # Leftover Y/Z are not exclusive and not yet shareable
        self.assertNotIn('y', mgr.ownership.exclusive)
        self.assertNotIn('z', mgr.ownership.exclusive)
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))

        # --- segment 0: T0 print on x + shared Y claim ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        self.assertIs(mgr.active_motion_queue, q0)
        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=5.0)
        self.assertEqual(m0.advance_axis, 'x')
        self.claim(mgr, 'q_T0', 'y')
        self.assertTrue(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))
        self.assertEqual(
            mgr.get_status()['shareable']['y'], 'q_T0')
        my0 = enqueue_shared_advance(
            th, mgr, 'y', min_move_t=0.02, distance=2.0)
        self.assertEqual(my0.advance_axis, 'y')
        self.release(mgr, 'q_T0', 'y')
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertIsNone(mgr.get_status()['shareable']['y'])

        # --- TOOLCHANGE T0->T1: park X0, both queues ---
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        self.assert_park_script(
            gcode.scripts[-1], 0, 'q_T0', 'q_T1')

        # --- segment 1: T1 print on dual_carriage ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(mgr.active_motion_queue, q1)
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=6.0)
        self.assertEqual(m1.advance_axis, 'dual_carriage')

        # --- TOOLCHANGE T1->T0: park X421 ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        self.assert_park_script(
            gcode.scripts[-1], 421, 'q_T1', 'q_T0')

        # --- segment 2: T0 print + claim Z (touched) ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        m2 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=4.0)
        self.assertEqual(m2.advance_axis, 'x')
        self.claim(mgr, 'q_T0', 'z')
        self.assertTrue(mgr.can_drive(q0, 'z'))
        mz = enqueue_shared_advance(
            th, mgr, 'z', min_move_t=0.02, distance=1.0)
        self.assertEqual(mz.advance_axis, 'z')
        self.release(mgr, 'q_T0', 'z')
        self.assertIsNone(mgr.get_status()['shareable']['z'])

        # --- TOOLCHANGE T0->T1 again: park X0 ---
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        self.assert_park_script(
            gcode.scripts[-1], 0, 'q_T0', 'q_T1')

        # --- segment 3: T1 print + claim Y handoff ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        m3 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=3.0)
        self.assertEqual(m3.advance_axis, 'dual_carriage')
        self.claim(mgr, 'q_T1', 'y')
        self.assertTrue(mgr.can_drive(q1, 'y'))
        self.assertFalse(mgr.can_drive(q0, 'y'))
        my1 = enqueue_shared_advance(
            th, mgr, 'y', min_move_t=0.02, distance=1.5)
        self.assertEqual(my1.advance_axis, 'y')
        self.release(mgr, 'q_T1', 'y')
        self.assertIsNone(mgr.get_status()['shareable']['y'])

        # One flush: dual-traj merge by mq_print_time (ties: primary
        # queue / x before dual_carriage), shared Y/Z interleaved.
        merged = th.lookahead.flush(lazy=False)
        axes = [m.advance_axis for m in merged]
        times = [m.mq_print_time for m in merged]
        self.assertEqual(len(merged), 7)
        self.assertEqual(
            axes,
            ['x', 'dual_carriage', 'y', 'dual_carriage',
             'x', 'y', 'z'])
        self.assertEqual(
            times, [0., 0., 0.02, 0.02, 0.04, 0.04, 0.06])
        carriage = [a for a in axes if a in CARRIAGE_AXES]
        self.assertEqual(
            carriage,
            ['x', 'dual_carriage', 'dual_carriage', 'x'])
        for m in merged:
            if m.advance_axis in CARRIAGE_AXES:
                self.assertNotEqual(m.axes_d[0], 0.)
            elif m.advance_axis == 'y':
                self.assertNotEqual(m.axes_d[1], 0.)
            elif m.advance_axis == 'z':
                self.assertNotEqual(m.axes_d[2], 0.)
        # Three TOOLCHANGE emits with Xplorer parks only
        tc_scripts = gcode.scripts
        self.assertGreaterEqual(len(tc_scripts), 4)
        parks = []
        for s in tc_scripts:
            if 'G1 X0' in s:
                parks.append(0)
            elif 'G1 X421' in s:
                parks.append(421)
        self.assertEqual(parks, [0, 421, 0])
        for s in tc_scripts:
            self.assertNotIn('X433', s)
            self.assertNotIn('X200', s)
            self.assertNotIn('X419', s)
            self.assertNotIn('X434', s)

    def test_print_demo_ownership_and_stock_tc(self):
        # Overlay-shaped ownership + Xplorer parks; stock no-queue
        # TOOLCHANGE still valid (T0/T1 park macros path).
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
        self.assertEqual(t1.park_x, 421)
        self.assertIsNone(t0.park_y)
        self.assertIsNone(t1.park_y)
        # Stock / no-[queue]: sequential TOOLCHANGE stays valid
        printer2, mq2, mgr2, obj2, th2 = load_tc(STOCK_TC_CFG)
        self.assertFalse(mgr2.ownership.multi_queue)
        gcode2 = printer2.lookup_object('gcode')
        self.toolchange(obj2, 'T0')
        self.toolchange(obj2, 'T1')
        script = gcode2.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('SET_MOTION_QUEUE', script)
        self.assertEqual(obj2.current.name, 'T1')
        self.toolchange(obj2, 'T0')
        script2 = gcode2.scripts[-1]
        self.assertIn('G1 X421', script2)
        self.assertEqual(obj2.current.name, 'T0')


if __name__ == '__main__':
    unittest.main(verbosity=2)
