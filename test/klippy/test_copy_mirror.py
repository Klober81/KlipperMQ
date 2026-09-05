# In-process tests for extras/copy_mirror.py
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
import extras.copy_mirror as copy_mirror
import extras.mq_copy as copy_mod
import extras.mirror as mirror_mod


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


IDEX_QUEUES = (
    "[queue]\nowned_axes: x\nextruder: extruder\n"
    "[queue q_T1]\nowned_axes: dual_carriage\n"
    "extruder: extruder1\n"
)


def _cm_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl in ('mq_copy', 'mirror'):
            sections.append(name)
    return sections


def load_cm(text, with_gcode=True, with_dual=True):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    if with_gcode:
        printer.objects['gcode'] = DummyGCode()
    if with_dual:
        printer.objects['dual_carriage'] = DummyDualCarriage()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    obj = None
    for name in _cm_sections(fileconfig):
        wrap = config.getsection(name)
        if name.lower() == 'mq_copy':
            obj = copy_mod.load_config(wrap)
        else:
            obj = mirror_mod.load_config(wrap)
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


class TestCopyMirror(unittest.TestCase):
    def assert_error(self, text, fragment):
        with self.assertRaises(configfile.error) as ctx:
            load_cm(text)
        msg = str(ctx.exception)
        self.assertIn(fragment, msg, msg)
        return msg

    def test_no_sections_no_commands_stock_dual(self):
        # (a) no [mq_copy]/[mirror] -> no COPY/MIRROR; stock dual loads
        dual = os.path.join(ROOT, 'test', 'klippy',
                            'dual_carriage.cfg')
        with open(dual, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_cm(text)
        self.assertIsNone(obj)
        self.assertIsNone(
            printer.lookup_object('copy_mirror', None))
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('COPY', gcode.commands)
        self.assertNotIn('MIRROR', gcode.commands)
        self.assertNotIn('OFF', gcode.commands)

    def test_mirror_missing_center_config_error(self):
        # (b) [mirror] missing center -> config_error
        text = IDEX_QUEUES + "[mirror]\naxis: x\n"
        self.assert_error(text, "must specify center")

    def test_mirror_missing_axis_config_error(self):
        text = IDEX_QUEUES + "[mirror]\ncenter: 150\n"
        self.assert_error(text, "must specify axis")

    def test_singleton_both_sections(self):
        text = (
            IDEX_QUEUES
            + "[mq_copy]\n"
            + "[mirror]\naxis: x\ncenter: 150\n"
        )
        printer, config, obj, access = load_cm(text)
        a = copy_mod.load_config(config.getsection('mq_copy'))
        b = mirror_mod.load_config(config.getsection('mirror'))
        self.assertEqual(id(a), id(b))
        self.assertEqual(id(a), id(obj))
        self.assertIs(
            printer.lookup_object('copy_mirror'), obj)
        gcode = printer.lookup_object('gcode')
        self.assertIn('COPY', gcode.commands)
        self.assertIn('MIRROR', gcode.commands)
        self.assertIn('OFF', gcode.commands)
        mq = printer.lookup_object('mq_config')
        self.assertIsNotNone(mq.copy)
        self.assertIsNotNone(mq.mirror)
        self.assertIsNone(mq.copy.source)
        self.assertIsNone(mq.mirror.source)
        self.assertEqual(mq.mirror.axis, 'x')
        self.assertEqual(mq.mirror.center, 150.)
        check_unused(printer, config, access)

    def test_source_default_primary(self):
        text = IDEX_QUEUES + "[mq_copy]\n"
        printer, config, obj, access = load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertIsNone(mq.copy.source)
        gcode = printer.lookup_object('gcode')
        obj.cmd_COPY(DummyGCmd())
        script = gcode.scripts[-1]
        # primary owns x => CARRIAGE=0 PRIMARY; follower=1 COPY
        lines = script.split('\n')
        self.assertEqual(
            lines[0],
            'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY')
        self.assertEqual(
            lines[1],
            'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY')
        self.assertEqual(
            lines[2],
            'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
            ' MOTION_QUEUE=extruder')
        check_unused(printer, config, access)

    def test_copy_mirror_off_emit_order(self):
        # (c) COPY/MIRROR line order; OFF unsyncs
        text = (
            IDEX_QUEUES
            + "[mq_copy]\n"
            + "[mirror]\naxis: x\ncenter: 150\n"
        )
        printer, config, obj, access = load_cm(text)
        gcode = printer.lookup_object('gcode')
        obj.cmd_COPY(DummyGCmd())
        copy_script = gcode.scripts[-1]
        self.assertEqual(
            copy_script.split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        obj.cmd_MIRROR(DummyGCmd())
        mirror_script = gcode.scripts[-1]
        self.assertEqual(
            mirror_script.split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=MIRROR',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        obj.cmd_OFF(DummyGCmd())
        off_script = gcode.scripts[-1]
        self.assertEqual(
            off_script.split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder1',
            ])
        check_unused(printer, config, access)

    def test_copy_only_registers_copy_and_off(self):
        text = IDEX_QUEUES + "[mq_copy]\n"
        printer, config, obj, access = load_cm(text)
        gcode = printer.lookup_object('gcode')
        self.assertIn('COPY', gcode.commands)
        self.assertNotIn('MIRROR', gcode.commands)
        self.assertIn('OFF', gcode.commands)

    def test_mirror_only_registers_mirror_and_off(self):
        text = IDEX_QUEUES + "[mirror]\naxis: x\ncenter: 150\n"
        printer, config, obj, access = load_cm(text)
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('COPY', gcode.commands)
        self.assertIn('MIRROR', gcode.commands)
        self.assertIn('OFF', gcode.commands)

    def test_missing_dual_carriage_errors(self):
        text = IDEX_QUEUES + "[mq_copy]\n"
        printer, config, obj, access = load_cm(
            text, with_dual=False)
        with self.assertRaises(configfile.error) as ctx:
            obj.cmd_COPY(DummyGCmd())
        self.assertIn('dual_carriage', str(ctx.exception))

    def test_source_token_primary_aliases(self):
        text = (
            IDEX_QUEUES
            + "[mq_copy]\nsource: primary\n"
        )
        printer, config, obj, access = load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertEqual(mq.copy.source, 'primary')
        gcode = printer.lookup_object('gcode')
        obj.cmd_COPY(DummyGCmd())
        script = gcode.scripts[-1]
        self.assertIn('CARRIAGE=0 MODE=PRIMARY', script)
        self.assertIn('CARRIAGE=1 MODE=COPY', script)
        check_unused(printer, config, access)


if __name__ == '__main__':
    unittest.main(verbosity=2)
