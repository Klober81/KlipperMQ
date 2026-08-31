# In-process tests for extras/mq_manager.py
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


def load_mgr(text, with_gcode=True):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(printer, fileconfig, access, 'printer')
    if with_gcode:
        printer.objects['gcode'] = DummyGCode()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    return printer, config, mq, mgr, access

def load_mgr_file(path):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return load_mgr(text)


class TestMQManager(unittest.TestCase):
    def claim(self, mgr, queue, axis):
        mgr.cmd_QUEUE_CLAIM(DummyGCmd(QUEUE=queue, AXIS=axis))

    def release(self, mgr, queue, axis):
        mgr.cmd_QUEUE_RELEASE(DummyGCmd(QUEUE=queue, AXIS=axis))

    def assert_cmd_error(self, fn, fragment):
        with self.assertRaises(configfile.error) as ctx:
            fn()
        msg = str(ctx.exception)
        self.assertIn(fragment, msg, msg)
        return msg

    def test_stock_implicit_primary_can_drive_any_axis(self):
        cfg = "[printer]\nkinematics: cartesian\n"
        printer, config, mq, mgr, access = load_mgr(cfg)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertTrue(mgr.primary.is_primary)
        self.assertEqual(mgr.primary.exclusive_axes, ())
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.ownership.exclusive, {})
        self.assertEqual(mgr.ownership.shareable, {})
        st = mgr.get_status()
        self.assertEqual(st['ownership'], {})
        self.assertEqual(st['exclusive'], {})
        self.assertEqual(st['shareable'], {})
        self.assertFalse(st['multi_queue'])
        self.assertTrue(st['pause_all_queues_on_error'])
        for axis in ('x', 'y', 'z', 'dual_carriage', 'e'):
            self.assertTrue(mgr.can_drive(mgr.primary, axis), axis)
        self.assertEqual(mq.primary.owned_axes, ())

    def test_two_queues_exclusive_map(self):
        text = (
            "[queue]\nowned_axes: x\n"
            "[queue q_T1]\nowned_axes: dual_carriage\n"
        )
        printer, config, mq, mgr, access = load_mgr(text)
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIn('x', mgr.ownership.exclusive)
        self.assertIn('dual_carriage', mgr.ownership.exclusive)
        self.assertIs(mgr.ownership.exclusive['x'], mgr.primary)
        q1 = mgr.lookup_queue('q_T1')
        self.assertIs(mgr.ownership.exclusive['dual_carriage'], q1)
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertFalse(mgr.can_drive(mgr.primary, 'dual_carriage'))
        self.assertTrue(mgr.can_drive(q1, 'dual_carriage'))
        self.assertFalse(mgr.can_drive(q1, 'x'))
        st = mgr.get_status()
        self.assertEqual(st['ownership']['x'], 'q_T0')
        self.assertEqual(st['ownership']['dual_carriage'], 'q_T1')
        self.assertEqual(st['exclusive']['x'], 'q_T0')
        self.assertEqual(st['exclusive']['dual_carriage'], 'q_T1')
        self.assertEqual(len(st['queues']), 2)

    def test_secondary_implicit_primary_leftover_unclaimed(self):
        text = "[queue q_T1]\nowned_axes: x\n"
        printer, config, mq, mgr, access = load_mgr(text)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertEqual(mgr.primary.exclusive_axes, ())
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertNotIn('y', mgr.ownership.exclusive)
        self.assertNotIn('y', mgr.ownership.shareable)
        self.assertFalse(mgr.can_drive(mgr.primary, 'y'))
        self.assertFalse(mgr.can_drive(mgr.lookup_queue('q_T1'), 'y'))
        self.assertTrue(mgr.can_drive(mgr.lookup_queue('q_T1'), 'x'))
        self.assertFalse(mgr.can_drive(mgr.primary, 'x'))
        self.claim(mgr, 'q_T0', 'y')
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))
        self.assertFalse(mgr.can_drive(mgr.lookup_queue('q_T1'), 'y'))
        st = mgr.get_status()
        self.assertEqual(st['shareable']['y'], 'q_T0')
        self.assertIsNone(st['shareable'].get('x'))
        self.assertEqual(st['ownership']['y'], 'q_T0')

    def test_claim_release_steal_exclusive_unknown(self):
        text = (
            "[queue]\nowned_axes: x\n"
            "[queue q_T1]\nowned_axes: dual_carriage\n"
        )
        printer, config, mq, mgr, access = load_mgr(text)
        gcode = printer.lookup_object('gcode')
        self.assertIn('QUEUE_CLAIM', gcode.commands)
        self.assertIn('QUEUE_RELEASE', gcode.commands)
        self.claim(mgr, 'primary', 'y')
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))
        self.claim(mgr, 'q_T0', 'y')
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T1', 'y'),
            "already owned")
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))
        self.assertFalse(mgr.can_drive(mgr.lookup_queue('q_T1'), 'y'))
        self.release(mgr, 'q_T0', 'y')
        self.assertFalse(mgr.can_drive(mgr.primary, 'y'))
        st = mgr.get_status()
        self.assertIsNone(st['shareable']['y'])
        self.claim(mgr, 'q_T1', 'y')
        self.assertTrue(mgr.can_drive(mgr.lookup_queue('q_T1'), 'y'))
        self.release(mgr, 'q_T1', 'y')
        self.assert_cmd_error(
            lambda: self.release(mgr, 'q_T1', 'y'),
            "does not own shareable")
        self.assert_cmd_error(
            lambda: self.release(mgr, 'q_T0', 'x'),
            "exclusive")
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T1', 'x'),
            "exclusive")
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T0', 'dual_carriage'),
            "exclusive")
        msg = self.assert_cmd_error(
            lambda: self.claim(mgr, 'no_such', 'y'),
            "Unknown queue")
        self.assertIn("Valid names", msg)
        self.assertIn("q_T0", msg)
        self.assertIn("q_T1", msg)
        self.assert_cmd_error(
            lambda: self.release(mgr, 'ghost', 'y'),
            "Unknown queue")

    def test_hook_present_in_klippy(self):
        path = os.path.join(ROOT, 'klippy', 'klippy.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        extras_loop = (
            "for section_config in config.get_prefix_sections(''):\n"
            "            self.load_object(config, section_config.get_name()"
            ", None)")
        extras_at = src.find(extras_loop)
        mq_config_at = src.find("self.load_object(config, 'mq_config')")
        toolhead_at = src.find("for m in [toolhead]:")
        mq_manager_at = src.find("self.load_object(config, 'mq_manager')")
        self.assertGreater(extras_at, 0)
        self.assertGreater(mq_config_at, extras_at)
        self.assertGreater(mq_manager_at, mq_config_at)
        self.assertGreater(toolhead_at, mq_manager_at)
        self.assertNotIn("mq_manager", src[toolhead_at:])

    def test_real_load_two_queue_text_and_repo_files(self):
        text = (
            "[printer]\nkinematics: cartesian\n"
            "[queue]\nowned_axes: x\n"
            "[queue q_T1]\nowned_axes: dual_carriage\n"
        )
        printer, config, mq, mgr, access = load_mgr(text)
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertFalse(mgr.can_drive(mgr.primary, 'dual_carriage'))
        self.assertTrue(mgr.can_drive(
            mgr.lookup_queue('q_T1'), 'dual_carriage'))
        st = mgr.get_status()
        self.assertEqual(st['primary'], 'q_T0')
        self.assertEqual(st['ownership']['x'], 'q_T0')
        self.assertEqual(st['ownership']['dual_carriage'], 'q_T1')

        cart = os.path.join(ROOT, 'config', 'example-cartesian.cfg')
        printer, config, mq, mgr, access = load_mgr_file(cart)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))
        self.assertEqual(mgr.get_status()['ownership'], {})
        self.assertEqual(mgr.get_status()['queues'][mgr.primary.name]
                         ['exclusive_axes'], [])

        dual = os.path.join(ROOT, 'test', 'klippy', 'dual_carriage.cfg')
        printer, config, mq, mgr, access = load_mgr_file(dual)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(len(mgr.queues), 1)
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertTrue(mgr.can_drive(mgr.primary, 'dual_carriage'))
        self.assertEqual(mgr.get_status()['exclusive'], {})

    def test_gcode_optional_and_pause_default(self):
        printer, config, mq, mgr, access = load_mgr(
            "[printer]\nkinematics: cartesian\n", with_gcode=False)
        self.assertTrue(mgr.pause_all_queues_on_error)
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        text = (
            "[printer]\npause_all_queues_on_error: False\n"
            "[queue q_T1]\nowned_axes: x\n"
        )
        printer, config, mq, mgr, access = load_mgr(text)
        self.assertFalse(mgr.pause_all_queues_on_error)
        self.assertFalse(mgr.get_status()['pause_all_queues_on_error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
