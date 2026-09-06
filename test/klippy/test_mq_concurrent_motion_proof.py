# Prove-first harness: concurrent two-queue motion independence.
# Cut B: per-queue LookAheadQueue map + SET_MOTION_QUEUE on mq_manager.
#
# Documents: stock G0/G1 serialize through one ToolHead.lookahead; mq_manager
# ownership (can_drive) does not fork lookahead or gate toolhead.move.
# Multi-queue: mgr.lookaheads holds distinct LAs; SET_MOTION_QUEUE selects
# which LA toolhead.move uses. Host still one chronological MCU timed stream.
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
    def add_object(self, name, obj):
        if name in self.objects:
            raise self.config_error(
                "Printer object '%s' already created" % (name,))
        self.objects[name] = obj
    def lookup_object(self, name, default=configfile.sentinel):
        if name in self.objects:
            return self.objects[name]
        if default is configfile.sentinel:
            raise self.config_error("Unknown config object '%s'" % (name,))
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


class DummyGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)


class DummyGCmd:
    def __init__(self, **params):
        self._params = dict((k.upper(), str(v)) for k, v in params.items())
    def get(self, name, default=None):
        key = name.upper()
        if key in self._params:
            return self._params[key]
        if default is not None:
            return default
        raise configfile.error("Error on command: missing %s" % (name,))
    def error(self, msg):
        raise configfile.error(msg)


def load_mgr(text):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(printer, fileconfig, access, 'printer')
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


class TestConcurrentMotionProof(unittest.TestCase):
    def test_stock_toolhead_constructs_exactly_one_lookahead(self):
        # Structural fact on this SHA: one LookAheadQueue per ToolHead.
        src = read_src('klippy', 'toolhead.py')
        self.assertEqual(src.count('LookAheadQueue()'), 1)
        self.assertIn('self.lookahead = LookAheadQueue()', src)
        self.assertIn('class LookAheadQueue:', src)
        self.assertIn('move.calc_junction(self.queue[-2])', src)

    def test_g1_path_targets_single_toolhead_move(self):
        src = read_src('klippy', 'extras', 'gcode_move.py')
        self.assertIn('self.move_with_transform = toolhead.move', src)
        self.assertIn(
            'self.move_with_transform(self.last_position, self.speed)',
            src)
        # SET_MOTION_QUEUE lives on mq_manager (extras), not stock G1.
        self.assertNotIn('SET_MOTION_QUEUE', src)
        self.assertNotIn('mq_manager', src)

    def test_can_drive_does_not_appear_in_toolhead_or_gcode_move(self):
        # Gate: do not claim can_drive gates toolhead moves (it does not).
        for parts in (('klippy', 'toolhead.py'),
                      ('klippy', 'extras', 'gcode_move.py')):
            src = read_src(*parts)
            self.assertNotIn('can_drive', src, '/'.join(parts))

    def test_stock_path_has_empty_lookaheads_no_set_motion_queue(self):
        # Stock / multi_queue false: no per-queue LA fork; toolhead sole LA.
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.lookaheads, {})
        gcode = printer.lookup_object('gcode')
        self.assertIn('QUEUE_CLAIM', gcode.commands)
        self.assertNotIn('SET_MOTION_QUEUE', gcode.commands)

    def test_multi_queue_registers_set_motion_queue_and_lookahead_helpers(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        self.assertTrue(mgr.ownership.multi_queue)
        gcode = printer.lookup_object('gcode')
        self.assertIn('SET_MOTION_QUEUE', gcode.commands)
        self.assertTrue(hasattr(mgr, 'lookahead_for'))
        self.assertTrue(hasattr(mgr, 'lookaheads'))
        mgr_src = read_src('klippy', 'extras', 'mq_manager.py')
        self.assertIn('LookAheadQueue', mgr_src)
        self.assertIn('SET_MOTION_QUEUE', mgr_src)
        self.assertIn('self.lookaheads', mgr_src)

    def test_desired_per_queue_lookahead_map(self):
        """Two queues expose distinct LookAheadQueue objs on mgr.lookaheads.

        Cut B shape:
          mgr.lookaheads: {queue_name: LookAheadQueue instance}
        Independence = per-queue LA; host still merges to one MCU timed stream.
        """
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        lookaheads = getattr(mgr, 'lookaheads', None)
        self.assertIsInstance(
            lookaheads, dict,
            'expected mgr.lookaheads map')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        la0 = lookaheads.get(q0.name)
        la1 = lookaheads.get(q1.name)
        self.assertIsNotNone(la0, 'missing lookahead for q_T0')
        self.assertIsNotNone(la1, 'missing lookahead for q_T1')
        self.assertIsNot(la0, la1,
                         'q_T0 and q_T1 must not share one LookAheadQueue')
        self.assertEqual(type(la0).__name__, 'LookAheadQueue')
        self.assertEqual(type(la1).__name__, 'LookAheadQueue')
        # SET_MOTION_QUEUE selects active queue (thin route to toolhead LA).
        self.assertIs(mgr.active_motion_queue, mgr.primary)
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        self.assertIs(mgr.active_motion_queue, q1)
        self.assertIs(mgr.lookahead_for(q1), la1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
