# Longer Xplorer-shaped IQEX print-path demo (MQ overlay).
# Multiple TOOLCHANGE parks/unparks + SET_MOTION_QUEUE four-queue
# merge + QUEUE_CLAIM/RELEASE for leftover shared Y (Z if touched).
# Parks from [toolchange] only (Xplorer IQEX X0 / X419). CUT: not a
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


# Xplorer IQEX evidence (mq-iqex.cfg / stock PARK macros):
#   PARK_extruder  -> G1 X0   (T0)
#   PARK_extruder1 -> G1 X419 (T1)
#   PARK_extruder2 -> G1 X0   (T2)
#   PARK_extruder3 -> G1 X419 (T3)
# Overlay queues: owned_axes t0/t1/t2/t3 (mq-iqex.cfg).
# Leftover Y/Z need QUEUE_CLAIM before shared-axis moves.
# Do not invent parks / commanded_pos / shareable product config.
# Do NOT invent SET_DUAL_CARRIAGE for named t0-t3
# (CARRIAGE_AXES still x/dual_carriage; Architecture open).
# Do NOT use IDEX X421, Marathon X433, or stock X200.
# Evidence: configs/mq/xplorer/mq-iqex.cfg
#           configs/stock/xplorer/.../Xp_V1.1_Macros_IQEX.cfg
FOUR_QUEUE_TC_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 4

[queue]
owned_axes: t0
extruder: extruder

[queue q_T1]
owned_axes: t1
extruder: extruder1

[queue q_T2]
owned_axes: t2
extruder: extruder2

[queue q_T3]
owned_axes: t3
extruder: extruder3

[toolchange T0]
park_x: 0

[toolchange T1]
park_x: 419

[toolchange T2]
park_x: 0

[toolchange T3]
park_x: 419
"""

STOCK_TC_CFG = (
    "[toolchange T0]\npark_x: 0\n"
    "[toolchange T1]\npark_x: 419\n"
    "[toolchange T2]\npark_x: 0\n"
    "[toolchange T3]\npark_x: 419\n"
)

# Outgoing park_x by tool name (overlay evidence only).
PARK_BY_TOOL = {
    'T0': 0,
    'T1': 419,
    'T2': 0,
    'T3': 419,
}


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


class StubToolhead:
    def __init__(self):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False


class OwnedAxisMove:
    """Exclusive owned-axis delta; no park / commanded_pos invent.
    IQEX overlay axes are t0-t3 (not CARRIAGE_AXES)."""
    def __init__(self, advance_axis, min_move_t=0.01, distance=1.0):
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
    # No DummyDualCarriage: named t0-t3 are Architecture open;
    # do not invent SET_DUAL_CARRIAGE CARRIAGE index for them.
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
    axes = list(q.exclusive_axes)
    if not axes:
        raise AssertionError(
            'active queue %s has no exclusive axis' % (q.name,))
    return axes[0]


def enqueue_owned_advance(th, mgr, min_move_t=0.01, distance=1.0):
    axis = axis_for_active(mgr)
    q = mgr.active_motion_queue
    if not mgr.can_drive(q, axis):
        raise AssertionError(
            'queue %s cannot drive %s' % (q.name, axis))
    move = OwnedAxisMove(
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


def _forbid_foreign_parks(script):
    # IDEX / Marathon / stock pins must not appear.
    if 'X421' in script:
        raise AssertionError('invented/foreign park X421 in script')
    if 'X433' in script:
        raise AssertionError('invented/foreign park X433 in script')
    if 'X200' in script:
        raise AssertionError('invented/foreign park X200 in script')
    if 'SET_DUAL_CARRIAGE' in script:
        raise AssertionError(
            'invented SET_DUAL_CARRIAGE for IQEX named axes')


class TestXplorerIqexPrintDemo(unittest.TestCase):
    """Longer four-queue print-like path + shared Y/Z claim."""

    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def claim(self, mgr, queue, axis):
        mgr.cmd_QUEUE_CLAIM(DummyGCmd(QUEUE=queue, AXIS=axis))

    def release(self, mgr, queue, axis):
        mgr.cmd_QUEUE_RELEASE(DummyGCmd(QUEUE=queue, AXIS=axis))

    def assert_park_script(self, script, park_x, out_q, in_q):
        self.assertIn('G1 X%d' % (park_x,), script)
        _forbid_foreign_parks(script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=%s' % (out_q,), script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=%s' % (in_q,), script)
        self.assertLess(
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (out_q,)),
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (in_q,)))
        self.assertLess(
            script.find('G1 X%d' % (park_x,)),
            script.find('SET_MOTION_QUEUE QUEUE=%s' % (in_q,)))

    def test_print_demo_ownership_parks_xplorer_iqex_only(self):
        # Overlay parks X0/X419 only; ownership maps t0..t3.
        # No CARRIAGE_AXES invent / SET_DUAL_CARRIAGE invent.
        printer, mq, mgr, obj, th = load_tc(FOUR_QUEUE_TC_CFG)
        self.assertTrue(mgr.ownership.multi_queue)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        q2 = mgr.lookup_queue('q_T2')
        q3 = mgr.lookup_queue('q_T3')
        self.assertIs(mgr.ownership.exclusive['t0'], q0)
        self.assertIs(mgr.ownership.exclusive['t1'], q1)
        self.assertIs(mgr.ownership.exclusive['t2'], q2)
        self.assertIs(mgr.ownership.exclusive['t3'], q3)
        self.assertIsNone(mgr.carriage_for_queue(q0))
        self.assertIsNone(mgr.carriage_for_queue(q1))
        self.assertIsNone(mgr.carriage_for_queue(q2))
        self.assertIsNone(mgr.carriage_for_queue(q3))
        self.assertEqual(CARRIAGE_AXES, ('x', 'dual_carriage'))
        for name, park in PARK_BY_TOOL.items():
            spec = obj._by_name[name.lower()]
            self.assertEqual(spec.park_x, park)
            self.assertIsNone(spec.park_y)

    def test_print_demo_multi_toolchange_shared_yz(self):
        # Longer Xplorer IQEX CUT (not production print):
        #   T0 print + claim Y + release
        #   -> TOOLCHANGE T1 (park X0) -> T1 print
        #   -> TOOLCHANGE T2 (park X419) -> T2 print + claim Z
        #   -> TOOLCHANGE T3 (park X0) -> T3 print
        #   -> TOOLCHANGE T0 (park X419) -> T0 print + claim Y
        #   -> TOOLCHANGE T2 (park X0) -> T2 print
        #   -> flush: four owned axes + shared Y/Z in merge
        printer, mq, mgr, obj, th = load_tc(
            FOUR_QUEUE_TC_CFG, with_toolhead=True)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)
        self.assertIn('QUEUE_CLAIM', gcode.commands)
        self.assertIn('QUEUE_RELEASE', gcode.commands)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        q2 = mgr.lookup_queue('q_T2')
        q3 = mgr.lookup_queue('q_T3')
        self.assertNotIn('y', mgr.ownership.exclusive)
        self.assertNotIn('z', mgr.ownership.exclusive)
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))

        # --- segment 0: T0 print on t0 + shared Y claim ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        self.assertIs(mgr.active_motion_queue, q0)
        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=5.0)
        self.assertEqual(m0.advance_axis, 't0')
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

        # --- TOOLCHANGE T0->T1: park X0 ---
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        self.assert_park_script(
            gcode.scripts[-1], 0, 'q_T0', 'q_T1')

        # --- segment 1: T1 print on t1 ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(mgr.active_motion_queue, q1)
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=6.0)
        self.assertEqual(m1.advance_axis, 't1')

        # --- TOOLCHANGE T1->T2: park X419 ---
        self.toolchange(obj, 'T2')
        self.assertEqual(obj.current.name, 'T2')
        self.assert_park_script(
            gcode.scripts[-1], 419, 'q_T1', 'q_T2')

        # --- segment 2: T2 print + claim Z ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T2'))
        self.assertIs(mgr.active_motion_queue, q2)
        m2 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=4.0)
        self.assertEqual(m2.advance_axis, 't2')
        self.claim(mgr, 'q_T2', 'z')
        self.assertTrue(mgr.can_drive(q2, 'z'))
        mz = enqueue_shared_advance(
            th, mgr, 'z', min_move_t=0.02, distance=1.0)
        self.assertEqual(mz.advance_axis, 'z')
        self.release(mgr, 'q_T2', 'z')
        self.assertIsNone(mgr.get_status()['shareable']['z'])

        # --- TOOLCHANGE T2->T3: park X0 ---
        self.toolchange(obj, 'T3')
        self.assertEqual(obj.current.name, 'T3')
        self.assert_park_script(
            gcode.scripts[-1], 0, 'q_T2', 'q_T3')

        # --- segment 3: T3 print on t3 ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T3'))
        self.assertIs(mgr.active_motion_queue, q3)
        m3 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=3.0)
        self.assertEqual(m3.advance_axis, 't3')

        # --- TOOLCHANGE T3->T0: park X419 ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        self.assert_park_script(
            gcode.scripts[-1], 419, 'q_T3', 'q_T0')

        # --- segment 4: T0 print + claim Y handoff ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        m4 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=2.5)
        self.assertEqual(m4.advance_axis, 't0')
        self.claim(mgr, 'q_T0', 'y')
        self.assertTrue(mgr.can_drive(q0, 'y'))
        my1 = enqueue_shared_advance(
            th, mgr, 'y', min_move_t=0.02, distance=1.5)
        self.assertEqual(my1.advance_axis, 'y')
        self.release(mgr, 'q_T0', 'y')
        self.assertIsNone(mgr.get_status()['shareable']['y'])

        # --- TOOLCHANGE T0->T2: park X0 (extra cycle) ---
        self.toolchange(obj, 'T2')
        self.assertEqual(obj.current.name, 'T2')
        self.assert_park_script(
            gcode.scripts[-1], 0, 'q_T0', 'q_T2')

        # --- segment 5: T2 print ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T2'))
        m5 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=2.0)
        self.assertEqual(m5.advance_axis, 't2')

        # One flush: four owned axes + shared leftovers
        merged = th.lookahead.flush(lazy=False)
        axes = [m.advance_axis for m in merged]
        self.assertEqual(len(merged), 9)
        self.assertIn('t0', axes)
        self.assertIn('t1', axes)
        self.assertIn('t2', axes)
        self.assertIn('t3', axes)
        self.assertIn('y', axes)
        self.assertIn('z', axes)
        owned = [a for a in axes if a in ('t0', 't1', 't2', 't3')]
        self.assertEqual(
            owned,
            ['t0', 't1', 't2', 't3', 't0', 't2'])
        for m in merged:
            if m.advance_axis in ('t0', 't1', 't2', 't3'):
                self.assertNotEqual(m.axes_d[0], 0.)
            elif m.advance_axis == 'y':
                self.assertNotEqual(m.axes_d[1], 0.)
            elif m.advance_axis == 'z':
                self.assertNotEqual(m.axes_d[2], 0.)

        # Five TOOLCHANGE emits after first activate; parks only
        # X0 / X419 from overlay evidence.
        tc_scripts = gcode.scripts
        self.assertGreaterEqual(len(tc_scripts), 6)
        parks = []
        for s in tc_scripts:
            _forbid_foreign_parks(s)
            if 'G1 X0' in s and 'G1 X419' not in s:
                parks.append(0)
            elif 'G1 X419' in s:
                parks.append(419)
        # First TOOLCHANGE T0 may emit activate-only (no park).
        # Subsequent parks: T0->T1 X0, T1->T2 X419, T2->T3 X0,
        # T3->T0 X419, T0->T2 X0.
        self.assertEqual(parks, [0, 419, 0, 419, 0])

    def test_print_demo_stock_tc_no_queue(self):
        # Stock / no-[queue]: sequential TOOLCHANGE stays valid
        # with Xplorer IQEX parks only.
        printer2, mq2, mgr2, obj2, th2 = load_tc(STOCK_TC_CFG)
        self.assertFalse(mgr2.ownership.multi_queue)
        gcode2 = printer2.lookup_object('gcode')
        self.toolchange(obj2, 'T0')
        self.toolchange(obj2, 'T1')
        script = gcode2.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('SET_MOTION_QUEUE', script)
        _forbid_foreign_parks(script)
        self.assertEqual(obj2.current.name, 'T1')
        self.toolchange(obj2, 'T2')
        script2 = gcode2.scripts[-1]
        self.assertIn('G1 X419', script2)
        _forbid_foreign_parks(script2)
        self.assertEqual(obj2.current.name, 'T2')
        self.toolchange(obj2, 'T3')
        script3 = gcode2.scripts[-1]
        self.assertIn('G1 X0', script3)
        _forbid_foreign_parks(script3)
        self.assertEqual(obj2.current.name, 'T3')


if __name__ == '__main__':
    unittest.main(verbosity=2)
