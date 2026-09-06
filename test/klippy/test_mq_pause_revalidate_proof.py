# Prove ARCH sec 7: pause_all -> ownership re-validate before resume.
# Selective resume allowed. No shareable: config invent.
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


class DummyGCmd:
    def __init__(self, **params):
        self._params = dict((k.upper(), str(v))
                            for k, v in params.items())
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


def pause_with_toolhead(printer, mgr):
    stuff_las(mgr)
    th = DummyToolHead()
    mla = MultiLookAhead(mgr, th)
    th.lookahead = mla
    mgr.multi_lookahead = mla
    printer.objects['toolhead'] = th
    printer.send_event('gcode:command_error')
    return th


class TestPauseOwnershipRevalidate(unittest.TestCase):
    def test_api_surface_for_testing(self):
        # API for Testing: revalidate + selective resume.
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        self.assertTrue(hasattr(mgr, 'revalidate_ownership'))
        self.assertTrue(hasattr(mgr, 'resume_queues_after_error'))
        self.assertTrue(callable(mgr.revalidate_ownership))
        self.assertTrue(callable(mgr.resume_queues_after_error))
        src = read_src('klippy', 'extras', 'mq_manager.py')
        self.assertIn('def revalidate_ownership', src)
        self.assertIn('def resume_queues_after_error', src)
        # No shareable: config invent; no RESUME_* / error G-codes.
        self.assertNotIn('shareable:', TWO_QUEUE_CFG.lower())
        gcode = printer.lookup_object('gcode')
        for name in gcode.commands:
            self.assertNotIn('RESUME', name.upper())
            self.assertNotIn('ERROR', name.upper())
            self.assertNotIn('PAUSE_ALL', name.upper())
        # Prefer zero toolhead.py
        th_src = read_src('klippy', 'toolhead.py')
        self.assertNotIn('revalidate_ownership', th_src)
        self.assertNotIn('resume_queues_after_error', th_src)
        self.assertNotIn('error_paused_queues', th_src)

    def test_resume_calls_revalidate_before_clear(self):
        # Source order proof: revalidate before NeedPrime clear /
        # paused-set mutate on the resume path.
        src = read_src('klippy', 'extras', 'mq_manager.py')
        resume_fn = src.split('def resume_queues_after_error')[1]
        resume_fn = resume_fn.split('\ndef ')[0]
        self.assertIn('revalidate_ownership', resume_fn)
        reval_at = resume_fn.find('revalidate_ownership')
        clear_need = resume_fn.find('NeedPrime')
        self.assertGreaterEqual(reval_at, 0)
        if clear_need >= 0:
            self.assertLess(reval_at, clear_need)
        call_line = resume_fn.find('self.revalidate_ownership')
        self.assertGreaterEqual(call_line, 0)
        mut = resume_fn.find('_error_paused_queues -=')
        if mut < 0:
            mut = resume_fn.find('_error_paused_queues.clear')
        if mut < 0:
            mut = resume_fn.find('_error_paused_queues =')
        if mut >= 0:
            self.assertLess(call_line, mut)

    def test_pause_then_resume_all_revalidates(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = pause_with_toolhead(printer, mgr)
        self.assertEqual(th.special_queuing_state, 'NeedPrime')
        paused = mgr.get_status()['error_paused_queues']
        self.assertEqual(sorted(paused), ['q_T0', 'q_T1'])
        self.assertIn('x', mgr.ownership.exclusive)
        self.assertIn('dual_carriage', mgr.ownership.exclusive)
        mgr.resume_queues_after_error()
        self.assertEqual(th.special_queuing_state, '')
        self.assertEqual(mgr.get_status()['error_paused_queues'], [])
        self.assertIs(mgr.ownership.exclusive['x'], mgr.primary)

    def test_claim_while_paused_then_resume(self):
        # User may transfer ownership while paused (runtime claim).
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = pause_with_toolhead(printer, mgr)
        mgr.cmd_QUEUE_CLAIM(DummyGCmd(QUEUE='q_T0', AXIS='y'))
        self.assertIs(mgr.ownership.shareable.get('y'), mgr.primary)
        mgr.revalidate_ownership()
        mgr.resume_queues_after_error(['q_T0', 'q_T1'])
        self.assertEqual(th.special_queuing_state, '')
        self.assertIs(mgr.ownership.shareable.get('y'), mgr.primary)

    def test_corrupt_ownership_blocks_resume(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = pause_with_toolhead(printer, mgr)
        del mgr.ownership.exclusive['x']
        with self.assertRaises(configfile.error) as ctx:
            mgr.resume_queues_after_error()
        self.assertIn('re-validate', str(ctx.exception).lower())
        self.assertEqual(th.special_queuing_state, 'NeedPrime')
        paused = mgr.get_status()['error_paused_queues']
        self.assertEqual(sorted(paused), ['q_T0', 'q_T1'])

    def test_selective_resume(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = pause_with_toolhead(printer, mgr)
        mgr.resume_queues_after_error(['q_T1'])
        paused = mgr.get_status()['error_paused_queues']
        self.assertEqual(paused, ['q_T0'])
        self.assertEqual(th.special_queuing_state, '')
        self.assertIn('q_T0', paused)
        mgr.resume_queues_after_error(['q_T0'])
        self.assertEqual(mgr.get_status()['error_paused_queues'], [])

    def test_stock_and_disabled_unchanged(self):
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        th = DummyToolHead()
        th.special_queuing_state = 'Priming'
        printer.objects['toolhead'] = th
        printer.send_event('gcode:command_error')
        mgr.revalidate_ownership()
        mgr.resume_queues_after_error()
        self.assertEqual(th.special_queuing_state, 'Priming')
        self.assertEqual(mgr.get_status()['error_paused_queues'], [])

        printer, mq, mgr = load_mgr(DISABLED_CFG)
        stuff_las(mgr)
        th = DummyToolHead()
        printer.objects['toolhead'] = th
        printer.send_event('gcode:command_error')
        self.assertEqual(mgr.get_status()['error_paused_queues'], [])
        for name, la in mgr.lookaheads.items():
            self.assertFalse(la.is_empty(), name)
        mgr.resume_queues_after_error()
        self.assertEqual(th.special_queuing_state, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
