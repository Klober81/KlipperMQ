# Xplorer 2xGantry park/TOOLCHANGE smoke (MQ overlay shaped).
# TOOLCHANGE park/unpark + SET_MOTION_QUEUE on both queues.
# Parks from mq-2xgantry.cfg / stock PARK only (T0 X0/Y449,
# T1 X419/Y0). CUT: park/unpark smoke, not rematch -d, not
# full print demo. No shareable / commanded_pos / sec12 invent.
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
import toolhead


# Xplorer 2xGantry evidence (mq-2xgantry.cfg L1-L4, L31-L37):
#   PARK_extruder  -> G1 X0 then G1 Y449
#   PARK_extruder1 -> G1 X419 then G1 Y0
# Overlay [toolchange]: T0 park_x:0 park_y:449;
#   T1 park_x:419 park_y:0
# Queues: owned_axes x,gantry0 / dc_x,gantry1 (overlay L24-L29)
# Do NOT invent parks / commanded_pos / shareable / ACTIVATE.
# Do NOT use Marathon X433, IDEX X421, or stock X200.
TWO_QUEUE_TC_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2

[queue q_T0]
owned_axes: x, gantry0
extruder: extruder

[queue q_T1]
owned_axes: dc_x, gantry1
extruder: extruder1

[toolchange T0]
park_x: 0
park_y: 449

[toolchange T1]
park_x: 419
park_y: 0
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


def _toolchange_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl == 'toolchange' or sl.startswith('toolchange '):
            sections.append(name)
    return sections


def load_tc(text, with_toolhead=False, with_dual_carriage=False):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    if with_dual_carriage:
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


class TestXplorer2xGantryParkSmoke(unittest.TestCase):
    """Park/unpark TOOLCHANGE smoke for 2xGantry MQ overlay."""

    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def test_ownership_parks_2xgantry_only(self):
        # Parks from overlay evidence only; ownership matches
        # mq-2xgantry.cfg owned_axes (x,gantry0 / dc_x,gantry1).
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        self.assertTrue(mgr.ownership.multi_queue)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(mgr.ownership.exclusive['gantry0'], q0)
        self.assertIs(mgr.ownership.exclusive['dc_x'], q1)
        self.assertIs(mgr.ownership.exclusive['gantry1'], q1)
        t0 = obj._by_name['t0']
        t1 = obj._by_name['t1']
        self.assertEqual(t0.park_x, 0)
        self.assertEqual(t0.park_y, 449)
        self.assertEqual(t1.park_x, 419)
        self.assertEqual(t1.park_y, 0)
        # No shareable invent in this CUT
        self.assertFalse(
            hasattr(mgr.ownership, 'shareable')
            and mgr.ownership.shareable)

    def test_toolchange_park_unpark_queues(self):
        # Park/unpark CUT: TOOLCHANGE T1 parks outgoing T0 at
        # X0 Y449 with both queues; TOOLCHANGE T0 parks outgoing
        # T1 at X419 Y0 with both queues. No dual_carriage object
        # here -- Architecture still open on named multi-axis DC
        # for gantry/dc_x ownership (see mq-2xgantry.cfg note).
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertIsNotNone(obj)

        # Activate T0 first (no outgoing park)
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')

        # T0 -> T1: outgoing park X0 Y449 + both queues
        self.toolchange(obj, 'T1')
        self.assertEqual(obj.current.name, 'T1')
        script_t1 = gcode.scripts[-1]
        self.assertIn('G1 X0 Y449', script_t1)
        self.assertNotIn('X433', script_t1)
        self.assertNotIn('X421', script_t1)
        self.assertNotIn('X200', script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script_t1)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t1)
        self.assertLess(
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T0'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        self.assertLess(
            script_t1.find('G1 X0 Y449'),
            script_t1.find('SET_MOTION_QUEUE QUEUE=q_T1'))

        # T1 -> T0: outgoing park X419 Y0 + both queues
        self.toolchange(obj, 'T0')
        self.assertEqual(obj.current.name, 'T0')
        script_t0 = gcode.scripts[-1]
        self.assertIn('G1 X419 Y0', script_t0)
        self.assertNotIn('X433', script_t0)
        self.assertNotIn('X421', script_t0)
        self.assertNotIn('X200', script_t0)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script_t0)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script_t0)
        self.assertLess(
            script_t0.find('G1 X419 Y0'),
            script_t0.find('SET_MOTION_QUEUE QUEUE=q_T0'))

    def test_dual_carriage_failfast_architecture_open(self):
        # With dual_carriage present, q_T1 (dc_x,gantry1) owns
        # neither CARRIAGE_AXES token -- product fail-fast. Parks
        # CUT above is the ship gate; this documents Architecture
        # open (mq-2xgantry.cfg: runtime named multi-axis
        # TOOLCHANGE still open). Not a park invent.
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_dual_carriage=True)
        self.toolchange(obj, 'T0')
        with self.assertRaises(configfile.error) as ctx:
            self.toolchange(obj, 'T1')
        msg = str(ctx.exception)
        self.assertIn('owns neither x nor dual_carriage', msg)
        self.assertIn('q_T1', msg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
