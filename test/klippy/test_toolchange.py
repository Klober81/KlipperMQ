# In-process tests for extras/toolchange.py
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
import extras.toolchange as toolchange


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
        for cb in self.event_handlers.get(event, []):
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


class DummyDualCarriage:
    pass


def _toolchange_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl == 'toolchange' or sl.startswith('toolchange '):
            sections.append(name)
    return sections

def load_tc(text, with_gcode=True):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    if with_gcode:
        printer.objects['gcode'] = DummyGCode()
    printer.objects['dual_carriage'] = DummyDualCarriage()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
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
    return printer, config, obj, access

def check_unused(printer, config, access):
    fileconfig = config.fileconfig
    valid_sections = {s: 1 for s, o in printer.lookup_objects()}
    valid_sections.update({s: 1 for s, o in access})
    for section_name in fileconfig.sections():
        section = section_name.lower()
        if section not in valid_sections:
            raise configfile.error(
                "Section '%s' is not a valid config section"
                % (section,))
        for option in fileconfig.options(section_name):
            option = option.lower()
            if (section, option) not in access:
                raise configfile.error(
                    "Option '%s' is not valid in section '%s'"
                    % (option, section))


MARATHON_T0_T1 = (
    "[toolchange T0]\npark_x: 0\n"
    "[toolchange T1]\npark_x: 433\n"
)


class TestToolchange(unittest.TestCase):
    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def assert_error(self, text, fragment):
        with self.assertRaises(configfile.error) as ctx:
            load_tc(text)
        msg = str(ctx.exception)
        self.assertIn(fragment, msg, msg)
        return msg

    def test_parse_x_only(self):
        text = "[toolchange T0]\npark_x: 0\n"
        printer, config, obj, access = load_tc(text)
        self.assertEqual(len(obj.tools), 1)
        spec = obj.tools[0]
        self.assertEqual(spec.name, 'T0')
        self.assertEqual(spec.park_x, 0.)
        self.assertIsNone(spec.park_y)
        self.assertEqual(spec.park_z_hop, 0.)
        a = toolchange.load_config(
            config.getsection('toolchange T0'))
        b = toolchange.load_config_prefix(
            config.getsection('toolchange T0'))
        self.assertEqual(id(a), id(b))
        self.assertEqual(id(a), id(obj))
        check_unused(printer, config, access)

    def test_fail_neither_axis(self):
        self.assert_error("[toolchange]\n", "park_x or park_y")
        self.assert_error(
            "[toolchange T0]\npark_z_hop: 2\n",
            "park_x or park_y")

    def test_hop_unset(self):
        printer, config, obj, access = load_tc(MARATHON_T0_T1)
        self.assertEqual(obj.tools[0].park_z_hop, 0.)
        self.assertEqual(obj.tools[1].park_z_hop, 0.)
        t0 = toolchange.load_config(
            config.getsection('toolchange T0'))
        t1 = toolchange.load_config_prefix(
            config.getsection('toolchange T1'))
        self.assertEqual(id(t0), id(t1))
        self.assertEqual(id(t0), id(obj))
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        first = gcode.scripts[0] if gcode.scripts else ''
        self.assertNotIn('G1 X0', first)
        self.assertNotIn('X433', first)
        self.toolchange(obj, 'T1')
        park = gcode.scripts[-1]
        self.assertIn('G1 X0', park)
        self.assertNotIn('G91', park)
        self.assertNotIn('Z', park)
        check_unused(printer, config, access)

    def test_unknown_tool_lists_valid_names(self):
        printer, config, obj, access = load_tc(MARATHON_T0_T1)
        with self.assertRaises(configfile.error) as ctx:
            self.toolchange(obj, 'T9')
        msg = str(ctx.exception)
        self.assertIn("Unknown tool", msg)
        self.assertIn("Valid names", msg)
        self.assertIn("T0", msg)
        self.assertIn("T1", msg)

    def test_t0_to_t1_park_order_x0_then_x433(self):
        # Marathon PARK T0 is G1 X0; PARK T1 is G1 X433.
        printer, config, obj, access = load_tc(MARATHON_T0_T1)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        first = gcode.scripts[0] if gcode.scripts else ''
        self.assertNotIn('G1 X0', first)
        self.assertNotIn('X433', first)
        dc0 = 'SET_DUAL_CARRIAGE CARRIAGE=0'
        if first:
            self.assertIn(dc0, first)
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('X433', script)
        self.assertIn(dc0, script)
        self.assertLess(script.find(dc0), script.find('G1 X0'))
        n = len(gcode.scripts)
        self.toolchange(obj, 'T0')
        self.assertEqual(len(gcode.scripts), n + 1)
        self.assertIn('G1 X433', gcode.scripts[-1])
        joined = '\n'.join(gcode.scripts)
        self.assertLess(joined.find('X0'), joined.find('X433'))
        self.assertEqual(obj.current.name, 'T0')

    def test_stock_cfg_without_toolchange_has_no_object(self):
        cfg = os.path.join(ROOT, 'config', 'example-cartesian.cfg')
        with open(cfg, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_tc(text)
        self.assertIsNone(obj)
        self.assertIsNone(
            printer.lookup_object('toolchange', None))
        dual = os.path.join(ROOT, 'test', 'klippy',
                            'dual_carriage.cfg')
        with open(dual, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_tc(text)
        self.assertIsNone(obj)
        self.assertIsNone(
            printer.lookup_object('toolchange', None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
