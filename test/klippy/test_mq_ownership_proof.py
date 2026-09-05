# Ownership proof for extras/mq_manager.py (base SHA 42613631)
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

BASE_SHA = '42613631'


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


class TestMQOwnershipProof(unittest.TestCase):
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

    def test_two_queue_ownership_proof(self):
        self.assertTrue(BASE_SHA.startswith('42613631'))
        text = (
            "[printer]\nmax_queues: 3\n"
            "[queue]\nowned_axes: x\n"
            "[queue q_T1]\nowned_axes: dual_carriage\n"
        )
        printer, config, mq, mgr, access = load_mgr(text)
        q0 = mgr.primary
        q1 = mgr.lookup_queue('q_T1')
        st = mgr.get_status()

        self.assertTrue(mgr.ownership.multi_queue)
        self.assertTrue(st['multi_queue'])
        self.assertEqual(q0.exclusive_axes, ('x',))
        self.assertEqual(q1.exclusive_axes, ('dual_carriage',))
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(mgr.ownership.exclusive['dual_carriage'], q1)
        self.assertEqual(st['exclusive']['x'], 'q_T0')
        self.assertEqual(st['exclusive']['dual_carriage'], 'q_T1')
        self.assertEqual(st['ownership']['x'], 'q_T0')
        self.assertEqual(st['ownership']['dual_carriage'], 'q_T1')
        self.assertTrue(mgr.can_drive(q0, 'x'))
        self.assertTrue(mgr.can_drive(q1, 'dual_carriage'))
        self.assertFalse(mgr.can_drive(q0, 'dual_carriage'))
        self.assertFalse(mgr.can_drive(q1, 'x'))
        self.assertNotIn('y', mgr.ownership.exclusive)
        self.assertNotIn('z', mgr.ownership.exclusive)
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))
        self.assertFalse(mgr.can_drive(q0, 'z'))
        self.assertFalse(mgr.can_drive(q1, 'z'))

        self.claim(mgr, 'q_T0', 'y')
        self.assertTrue(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))
        self.assertEqual(mgr.get_status()['shareable']['y'], 'q_T0')
        self.assertEqual(mgr.get_status()['ownership']['y'], 'q_T0')
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T1', 'y'),
            "already owned")
        self.release(mgr, 'q_T0', 'y')
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertIsNone(mgr.get_status()['shareable']['y'])
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T1', 'x'),
            "exclusive")
        self.assert_cmd_error(
            lambda: self.claim(mgr, 'q_T0', 'dual_carriage'),
            "exclusive")
        self.assert_cmd_error(
            lambda: self.release(mgr, 'q_T0', 'x'),
            "exclusive")

        cart = os.path.join(ROOT, 'config', 'example-cartesian.cfg')
        printer, config, mq, mgr, access = load_mgr_file(cart)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertFalse(mgr.get_status()['multi_queue'])
        self.assertEqual(mgr.get_status()['ownership'], {})
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertTrue(mgr.can_drive(mgr.primary, 'y'))

        dual = os.path.join(ROOT, 'test', 'klippy', 'dual_carriage.cfg')
        printer, config, mq, mgr, access = load_mgr_file(dual)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(len(mgr.queues), 1)
        self.assertEqual(mgr.get_status()['exclusive'], {})
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertTrue(mgr.can_drive(mgr.primary, 'dual_carriage'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
