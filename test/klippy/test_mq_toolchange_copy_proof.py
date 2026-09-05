# TOOLCHANGE then COPY/MIRROR: carriage and primary from ownership
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
import extras.toolchange as toolchange
import extras.mq_copy as copy_mod

BASE_SHA = '262f243d'

IDEX_QUEUES = (
    "[queue]\nowned_axes: x\nextruder: extruder\n"
    "[queue q_T1]\nowned_axes: dual_carriage\n"
    "extruder: extruder1\n"
)

REVERSE_TOOLS = (
    "[toolchange T1]\npark_x: 433\n"
    "[toolchange T0]\npark_x: 0\n"
)


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


def load_interaction(text):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    gcode = DummyGCode()
    printer.objects['gcode'] = gcode
    printer.objects['dual_carriage'] = DummyDualCarriage()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    tc_obj = None
    for name in fileconfig.sections():
        sl = name.lower()
        wrap = config.getsection(name)
        if sl == 'toolchange' or sl.startswith('toolchange '):
            parts = name.split()
            if len(parts) > 1:
                tc_obj = toolchange.load_config_prefix(wrap)
            else:
                tc_obj = toolchange.load_config(wrap)
            printer.objects[name] = tc_obj
        elif sl == 'mq_copy':
            cm = copy_mod.load_config(wrap)
            printer.objects[name] = cm
            printer.objects['copy_mirror'] = cm
    printer.send_event('klippy:connect')
    return printer, config, tc_obj, gcode, access


class TestToolchangeCopyProof(unittest.TestCase):
    def test_toolchange_then_copy_from_ownership_not_tools_index(self):
        # base SHA 262f243d: reverse tools list would map T1 to index 0;
        # ownership still gives T1 CARRIAGE=1, then COPY primary from x.
        text = (
            IDEX_QUEUES
            + REVERSE_TOOLS
            + "[mq_copy]\n"
        )
        printer, config, tc, gcode, access = load_interaction(text)
        self.assertEqual(tc.tools[0].name, 'T1')
        self.assertEqual(tc.tools[1].name, 'T0')
        mgr = printer.lookup_object('mq_manager')
        self.assertTrue(mgr.ownership.multi_queue)

        tc.cmd_TOOLCHANGE(DummyGCmd(TOOL='T0'))
        first = gcode.scripts[0]
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=0', first)
        self.assertNotIn('CARRIAGE=1', first)

        tc.cmd_TOOLCHANGE(DummyGCmd(TOOL='T1'))
        tc_script = gcode.scripts[-1]
        self.assertIn('SET_DUAL_CARRIAGE CARRIAGE=1', tc_script)
        self.assertEqual(tc.current.name, 'T1')

        cm = printer.lookup_object('copy_mirror')
        cm.cmd_COPY(DummyGCmd())
        copy_script = gcode.scripts[-1]
        self.assertIn(
            'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY', copy_script)
        self.assertIn(
            'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY', copy_script)
        self.assertLess(
            copy_script.find('CARRIAGE=0 MODE=PRIMARY'),
            copy_script.find('CARRIAGE=1 MODE=COPY'))

    def test_copy_source_q_t1_flips_primary_carriage(self):
        text = (
            IDEX_QUEUES
            + REVERSE_TOOLS
            + "[mq_copy]\nsource: q_T1\n"
        )
        printer, config, tc, gcode, access = load_interaction(text)
        cm = printer.lookup_object('copy_mirror')
        cm.cmd_COPY(DummyGCmd())
        script = gcode.scripts[-1]
        self.assertIn(
            'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=PRIMARY', script)
        self.assertIn(
            'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=COPY', script)


if __name__ == '__main__':
    unittest.main(verbosity=2)
