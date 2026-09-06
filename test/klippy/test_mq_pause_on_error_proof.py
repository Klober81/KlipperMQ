# Prove Philosophy #5: pause_all_queues_on_error on command_error.
# ARCH sec 7: idle all queue LAs / NeedPrime special_queuing.
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

DISABLED_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2
pause_all_queues_on_error: False

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
        self.event_handlers = {}
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
    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)
    def send_event(self, event, *params):
        return [cb(*params)
                for cb in self.event_handlers.get(event, [])]


class DummyGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)


class DummyToolHead:
    def __init__(self):
        self.special_queuing_state = ""
        self.need_check_pause = 0.
        self.check_stall_time = 1.
        self.lookahead = None


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


def stuff_las(mgr):
    for la in mgr.lookaheads.values():
        la.queue.append(object())


class TestPauseAllQueuesOnError(unittest.TestCase):
    def test_registers_command_error_handler(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        handlers = printer.event_handlers.get(
            'gcode:command_error', [])
        self.assertTrue(handlers)
        self.assertIn(mgr._handle_command_error, handlers)
        src = read_src('klippy', 'extras', 'mq_manager.py')
        self.assertIn('gcode:command_error', src)
        self.assertIn('pause_queues_for_error', src)
        # BLOCK: no new error G-codes
        gcode = printer.lookup_object('gcode')
        for name in gcode.commands:
            self.assertNotIn('ERROR', name.upper())
            self.assertNotIn('PAUSE_ALL', name.upper())

    def test_inject_error_idles_all_las(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        self.assertTrue(mgr.pause_all_queues_on_error)
        self.assertTrue(mgr.ownership.multi_queue)
        stuff_las(mgr)
        for name, la in mgr.lookaheads.items():
            self.assertFalse(la.is_empty(), name)
        th = DummyToolHead()
        mla = MultiLookAhead(mgr, th)
        mla._next_t['q_T0'] = 1.0
        mla._next_t['q_T1'] = 2.0
        th.lookahead = mla
        mgr.multi_lookahead = mla
        printer.objects['toolhead'] = th
        # Inject via stock event path
        printer.send_event('gcode:command_error')
        for name, la in mgr.lookaheads.items():
            self.assertTrue(la.is_empty(), name)
        self.assertEqual(th.special_queuing_state, 'NeedPrime')
        self.assertEqual(th.need_check_pause, -1.)
        self.assertEqual(th.check_stall_time, 0.)
        self.assertEqual(mla._next_t, {})
        # Ownership left defined (not half-cleared)
        self.assertIn('x', mgr.ownership.exclusive)
        self.assertIn('dual_carriage', mgr.ownership.exclusive)

    def test_disabled_option_leaves_las(self):
        printer, mq, mgr = load_mgr(DISABLED_CFG)
        self.assertFalse(mgr.pause_all_queues_on_error)
        stuff_las(mgr)
        th = DummyToolHead()
        printer.objects['toolhead'] = th
        printer.send_event('gcode:command_error')
        for name, la in mgr.lookaheads.items():
            self.assertFalse(la.is_empty(), name)
        self.assertEqual(th.special_queuing_state, '')

    def test_stock_path_unchanged(self):
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.lookaheads, {})
        th = DummyToolHead()
        th.special_queuing_state = 'Priming'
        printer.objects['toolhead'] = th
        printer.send_event('gcode:command_error')
        self.assertEqual(mgr.lookaheads, {})
        self.assertEqual(th.special_queuing_state, 'Priming')
        # toolhead.py untouched; no can_drive on pause path
        th_src = read_src('klippy', 'toolhead.py')
        self.assertNotIn('pause_queues_for_error', th_src)
        self.assertNotIn('pause_all_queues_on_error', th_src)
        mgr_src = read_src('klippy', 'extras', 'mq_manager.py')
        pause_fn = mgr_src.split('def pause_queues_for_error')[1]
        pause_fn = pause_fn.split('def _select_motion_queue')[0]
        self.assertNotIn('can_drive(', pause_fn)


if __name__ == '__main__':
    unittest.main(verbosity=2)
