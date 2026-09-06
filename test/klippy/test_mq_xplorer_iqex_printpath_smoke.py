# Short IQEX print-path smoke (Xplorer-shaped MQ).
# TOOLCHANGE + SET_MOTION_QUEUE + owned-axis motion on
# four queues. Parks from [toolchange] only (X0 / X419).
# CUT: not a full IQEX print demo claim.
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
# Do not invent parks / commanded_pos / shareable.
# Do NOT invent SET_DUAL_CARRIAGE for named t0-t3
# (CARRIAGE_AXES still x/dual_carriage; Architecture open).
# Do NOT use IDEX X421, Marathon X433, or stock X200.
# Evidence: configs/mq/xplorer/mq-iqex.cfg
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


class TestXplorerIqexPrintpathSmoke(unittest.TestCase):
    """Short four-queue print-like path; parks from overlay only."""

    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def test_printpath_ownership_parks_xplorer_iqex_only(self):
        # Config parks are Xplorer IQEX X0 / X419 only; ownership
        # maps t0..t3 to q_T0..q_T3. No CARRIAGE_AXES invent.
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
        # Named-carriage index not claimed at this tip.
        self.assertIsNone(mgr.carriage_for_queue(q0))
        self.assertIsNone(mgr.carriage_for_queue(q1))
        self.assertIsNone(mgr.carriage_for_queue(q2))
        self.assertIsNone(mgr.carriage_for_queue(q3))
        self.assertEqual(CARRIAGE_AXES, ('x', 'dual_carriage'))
        t0 = obj._by_name['t0']
        t1 = obj._by_name['t1']
        t2 = obj._by_name['t2']
        t3 = obj._by_name['t3']
        self.assertEqual(t0.park_x, 0)
        self.assertEqual(t1.park_x, 419)
        self.assertEqual(t2.park_x, 0)
        self.assertEqual(t3.park_x, 419)
        self.assertIsNone(t0.park_y)
        self.assertIsNone(t1.park_y)
        self.assertIsNone(t2.park_y)
        self.assertIsNone(t3.park_y)

    def test_printpath_toolchange_and_four_queues(self):
        # Print-like CUT: T0 print -> TOOLCHANGE T1 (park X0) ->
        # T1 print -> TOOLCHANGE T2 (park X419) -> T2 print ->
        # TOOLCHANGE T3 (park X0) -> T3 print -> flush merge.
        printer, mq, mgr, obj, th = load_tc(
            FOUR_QUEUE_TC_CFG, with_toolhead=True)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        q2 = mgr.lookup_queue('q_T2')
        q3 = mgr.lookup_queue('q_T3')

        # --- segment 0: activate T0, short print on t0 ---
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        self.assertIs(mgr.active_motion_queue, q0)
        m0 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=5.0)
        self.assertEqual(m0.advance_axis, 't0')

        # --- toolchange to T1: outgoing park X0 ---
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        script_t1 = gcode.scripts[-1]
        self.assertIn('G1 X0', script_t1)
        _forbid_foreign_parks(script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t1)
        self.assertLess(
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T0'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        self.assertLess(
            script_t1.find('G1 X0'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))

        # --- segment 1: short print on t1 ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(mgr.active_motion_queue, q1)
        m1 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=6.0)
        self.assertEqual(m1.advance_axis, 't1')

        # --- toolchange to T2: outgoing park X419 ---
        self.toolchange(obj, 'T2')
        self.assertEqual(obj.current.name, 'T2')
        script_t2 = gcode.scripts[-1]
        self.assertIn('G1 X419', script_t2)
        _forbid_foreign_parks(script_t2)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t2)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T2', script_t2)

        # --- segment 2: short print on t2 ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T2'))
        self.assertIs(mgr.active_motion_queue, q2)
        m2 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=4.0)
        self.assertEqual(m2.advance_axis, 't2')

        # --- toolchange to T3: outgoing park X0 ---
        self.toolchange(obj, 'T3')
        self.assertEqual(obj.current.name, 'T3')
        script_t3 = gcode.scripts[-1]
        self.assertIn('G1 X0', script_t3)
        _forbid_foreign_parks(script_t3)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T2', script_t3)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T3', script_t3)

        # --- segment 3: short print on t3 ---
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T3'))
        self.assertIs(mgr.active_motion_queue, q3)
        m3 = enqueue_owned_advance(
            th, mgr, min_move_t=0.02, distance=3.0)
        self.assertEqual(m3.advance_axis, 't3')

        # One flush: all four owned axes in merged stream
        merged = th.lookahead.flush(lazy=False)
        axes = [m.advance_axis for m in merged]
        self.assertEqual(len(merged), 4)
        self.assertEqual(
            set(axes), {'t0', 't1', 't2', 't3'})
        for m in merged:
            self.assertNotEqual(
                m.axes_d[0], 0.,
                'owned axis %s did not advance'
                % (m.advance_axis,))
        self.assertEqual(axes, ['t0', 't1', 't2', 't3'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
