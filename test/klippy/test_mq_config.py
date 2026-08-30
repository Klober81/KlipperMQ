# In-process tests for extras/mq_config.py
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import extras.mq_config as mq_config


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


def load_mq(text):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(printer, fileconfig, access, 'printer')
    obj = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = obj
    return printer, config, obj, access

def check_unused(printer, config, access):
    fileconfig = config.fileconfig
    valid_sections = {s: 1 for s, o in printer.lookup_objects()}
    valid_sections.update({s: 1 for s, o in access})
    for section_name in fileconfig.sections():
        section = section_name.lower()
        if section not in valid_sections:
            raise configfile.error(
                "Section '%s' is not a valid config section" % (section,))
        for option in fileconfig.options(section_name):
            option = option.lower()
            if (section, option) not in access:
                raise configfile.error(
                    "Option '%s' is not valid in section '%s'"
                    % (option, section))


class TestMQConfig(unittest.TestCase):
    def assert_error(self, text, fragment):
        with self.assertRaises(configfile.error) as ctx:
            load_mq(text)
        msg = str(ctx.exception)
        self.assertIn(fragment, msg, msg)

    def test_implicit_primary_stock(self):
        cfg = "[printer]\nkinematics: cartesian\n"
        printer, config, obj, access = load_mq(cfg)
        self.assertTrue(obj.primary.is_implicit)
        self.assertTrue(obj.primary.is_primary)
        self.assertEqual(obj.primary.name, 'q_T0')
        self.assertEqual(obj.primary.aliases, ('primary', 'q_T0', 'queue_T0'))
        self.assertEqual(obj.primary.owned_axes, ())
        self.assertIs(obj.lookup_queue('primary'), obj.primary)
        self.assertIs(obj.lookup_queue('q_T0'), obj.primary)
        self.assertIs(obj.lookup_queue('queue_T0'), obj.primary)
        self.assertEqual(len(obj.queues), 1)
        self.assertEqual(obj.max_queues, 5)
        self.assertEqual(obj.toolchanges, [])
        self.assertFalse(obj.recovery.section_present)
        self.assertTrue(obj.recovery.require_user_confirmation)
        self.assertTrue(obj.recovery.restore_chamber)

    def test_explicit_queue_requires_owned_axes(self):
        self.assert_error("[queue]\n", "owned_axes")
        self.assert_error("[queue q_T1]\n", "owned_axes")
        self.assert_error("[queue]\nowned_axes:\n", "owned_axes")
        self.assert_error("[queue q_T1]\nowned_axes:\n", "owned_axes")

    def test_max_queues_only_under_printer(self):
        self.assert_error(
            "[queue]\nowned_axes: x\nmax_queues: 3\n",
            "max_queues")
        self.assert_error(
            "[extruder]\nmax_queues: 2\n",
            "max_queues")

    def test_too_many_queues(self):
        text = (
            "[printer]\nmax_queues: 1\n"
            "[queue q_T0]\nowned_axes: x\n"
            "[queue q_T1]\nowned_axes: y\n"
        )
        self.assert_error(text, "Too many queues")

    def test_explicit_primary_and_secondary(self):
        text = (
            "[printer]\nmax_queues: 3\n"
            "[queue]\nowned_axes: x\nextruder: extruder\n"
            "[queue q_T1]\nowned_axes: y\n"
        )
        printer, config, obj, access = load_mq(text)
        self.assertFalse(obj.primary.is_implicit)
        self.assertEqual(obj.primary.owned_axes, ('x',))
        self.assertEqual(obj.primary.extruder, 'extruder')
        q1 = obj.lookup_queue('q_T1')
        self.assertFalse(q1.is_primary)
        self.assertEqual(q1.owned_axes, ('y',))
        self.assertEqual(obj.max_queues, 3)
        self.assertEqual(len(obj.queues), 2)
        check_unused(printer, config, access)

    def test_secondary_only_still_has_implicit_primary(self):
        text = "[queue q_T1]\nowned_axes: x\n"
        printer, config, obj, access = load_mq(text)
        self.assertTrue(obj.primary.is_implicit)
        self.assertEqual(obj.primary.owned_axes, ())
        self.assertEqual(obj.lookup_queue('q_T1').owned_axes, ('x',))
        self.assertEqual(len(obj.queues), 2)
        check_unused(printer, config, access)

    def test_named_primary_aliases(self):
        for name in ('q_T0', 'primary', 'queue_T0'):
            text = "[queue %s]\nowned_axes: x, z\n" % (name,)
            printer, config, obj, access = load_mq(text)
            self.assertFalse(obj.primary.is_implicit)
            self.assertEqual(obj.primary.owned_axes, ('x', 'z'))
            self.assertEqual(len(obj.queues), 1)
            check_unused(printer, config, access)

    def test_duplicate_primary(self):
        self.assert_error(
            "[queue]\nowned_axes: x\n[queue q_T0]\nowned_axes: y\n",
            "Duplicate primary")

    def test_axis_owned_by_two_queues(self):
        self.assert_error(
            "[queue]\nowned_axes: x\n[queue q_T1]\nowned_axes: x\n",
            "owned by both")

    def test_toolchange_requires_park_axis(self):
        self.assert_error("[toolchange]\n", "park_x or park_y")
        self.assert_error(
            "[toolchange T0]\npark_z_hop: 2\n",
            "park_x or park_y")

    def test_toolchange_park_x_only(self):
        text = "[toolchange T0]\npark_x: 0\n"
        printer, config, obj, access = load_mq(text)
        self.assertEqual(len(obj.toolchanges), 1)
        tc = obj.toolchanges[0]
        self.assertEqual(tc.name, 'T0')
        self.assertEqual(tc.park_x, 0.)
        self.assertIsNone(tc.park_y)
        self.assertEqual(tc.park_z_hop, 0.)
        check_unused(printer, config, access)

    def test_recovery_optional_and_defaults(self):
        printer, config, obj, access = load_mq("")
        self.assertFalse(obj.recovery.section_present)
        self.assertEqual(obj.recovery.bed_temp_hold_time, 0.)
        self.assertTrue(obj.recovery.require_user_confirmation)
        text = (
            "[recovery]\n"
            "require_user_confirmation: False\n"
            "restore_chamber: False\n"
            "bed_temp_hold_time: 12\n"
        )
        printer, config, obj, access = load_mq(text)
        self.assertTrue(obj.recovery.section_present)
        self.assertFalse(obj.recovery.require_user_confirmation)
        self.assertFalse(obj.recovery.restore_chamber)
        self.assertEqual(obj.recovery.bed_temp_hold_time, 12.)
        check_unused(printer, config, access)

    def test_stock_example_cartesian(self):
        cfg = os.path.join(ROOT, 'config', 'example-cartesian.cfg')
        with open(cfg, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_mq(text)
        self.assertTrue(obj.primary.is_implicit)
        self.assertTrue(obj.primary.is_primary)
        self.assertEqual(obj.primary.name, 'q_T0')
        self.assertEqual(obj.primary.aliases, ('primary', 'q_T0', 'queue_T0'))
        self.assertEqual(obj.primary.owned_axes, ())
        self.assertEqual(len(obj.queues), 1)
        self.assertEqual(obj.max_queues, 5)
        self.assertEqual(obj.toolchanges, [])
        self.assertFalse(obj.recovery.section_present)

    def test_stock_dual_carriage(self):
        cfg = os.path.join(ROOT, 'test', 'klippy', 'dual_carriage.cfg')
        with open(cfg, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_mq(text)
        self.assertTrue(obj.primary.is_implicit)
        self.assertEqual(obj.primary.name, 'q_T0')
        self.assertEqual(obj.primary.owned_axes, ())
        self.assertEqual(len(obj.queues), 1)
        self.assertIs(obj.lookup_queue('primary'), obj.primary)

    def test_hook_present_in_klippy(self):
        path = os.path.join(ROOT, 'klippy', 'klippy.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("self.load_object(config, 'mq_config')", src)
        extras_loop = (
            "for section_config in config.get_prefix_sections(''):\n"
            "            self.load_object(config, section_config.get_name()"
            ", None)")
        extras_at = src.find(extras_loop)
        hook_at = src.find("self.load_object(config, 'mq_config')")
        toolhead_at = src.find("for m in [toolhead]:")
        self.assertGreater(extras_at, 0)
        self.assertGreater(hook_at, extras_at)
        self.assertGreater(toolhead_at, hook_at)


if __name__ == '__main__':
    unittest.main(verbosity=2)
