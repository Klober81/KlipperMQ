# In-process tests for extras/recovery.py
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, tempfile, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import extras.mq_config as mq_config
import extras.motion_queuing as motion_queuing
import extras.recovery as recovery


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
        self.infos = []
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)
    def run_script_from_command(self, script):
        self.scripts.append(script)
    def respond_info(self, msg):
        self.infos.append(msg)


class DummyMotionQueuing:
    def __init__(self, last_id=0):
        self.bookmarks = type('B', (), {'last_id': last_id})()


class DummyPrintStats:
    def __init__(self, filename=''):
        self.filename = filename


def load_recovery(text, state_path=None, with_gcode=True,
                  motion_last_id=0, filename='job.gcode'):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    if with_gcode:
        printer.objects['gcode'] = DummyGCode()
    printer.objects['motion_queuing'] = DummyMotionQueuing(
        motion_last_id)
    printer.objects['print_stats'] = DummyPrintStats(filename)
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    obj = None
    if fileconfig.has_section('recovery'):
        wrap = config.getsection('recovery')
        if state_path is not None:
            mq.recovery.filename = state_path
        obj = recovery.load_config(wrap)
        printer.objects['recovery'] = obj
    # mq_config may be registered after extras load_config at boot;
    # consume only on klippy:connect (same gate as toolchange).
    printer.send_event('klippy:connect')
    return printer, config, obj, access


class TestRecovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix='mq_rec_', suffix='.state', delete=False)
        self._tmp.close()
        self.state_path = self._tmp.name
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def tearDown(self):
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def _cfg(self, extra=''):
        path = self.state_path.replace('\\', '/')
        return (
            "[recovery]\n"
            "z_hop_on_recover: 5\n"
            "filename: %s\n"
            "%s"
        ) % (path, extra)

    def test_stock_cfg_without_recovery_has_no_object(self):
        cfg = os.path.join(ROOT, 'config', 'example-cartesian.cfg')
        with open(cfg, encoding='utf-8') as f:
            text = f.read()
        printer, config, obj, access = load_recovery(text)
        self.assertIsNone(obj)
        self.assertIsNone(
            printer.lookup_object('recovery', None))

    def test_recovery_without_z_hop_fails(self):
        path = self.state_path.replace('\\', '/')
        text = "[recovery]\nfilename: %s\n" % (path,)
        with self.assertRaises(configfile.error) as ctx:
            load_recovery(text)
        self.assertIn('z_hop_on_recover', str(ctx.exception))

    def test_z_hop_zero_or_negative_fails(self):
        path = self.state_path.replace('\\', '/')
        for bad in ('0', '-1', '-0.1'):
            text = (
                "[recovery]\nz_hop_on_recover: %s\n"
                "filename: %s\n"
            ) % (bad, path)
            with self.assertRaises(configfile.error) as ctx:
                load_recovery(text)
            msg = str(ctx.exception)
            self.assertTrue(
                'z_hop_on_recover' in msg or 'above' in msg, msg)

    def test_consumes_mq_config_no_reparse_policy(self):
        text = self._cfg(
            "require_user_confirmation: False\n"
            "restore_chamber: False\n"
            "bed_temp_hold_time: 3\n")
        printer, config, obj, access = load_recovery(text)
        cfg = printer.lookup_object('mq_config').recovery
        self.assertIs(obj.cfg, cfg)
        self.assertFalse(cfg.require_user_confirmation)
        self.assertFalse(cfg.restore_chamber)
        self.assertEqual(cfg.bed_temp_hold_time, 3.)
        self.assertEqual(cfg.z_hop_on_recover, 5.)
        for key in access:
            self.assertNotIn('park_', key[1])

    def test_persist_clear_live_keeps_last(self):
        printer, config, obj, access = load_recovery(self._cfg())
        obj.note_bookmark(7, filename='a.gcode')
        self.assertIsNotNone(obj.live)
        self.assertEqual(obj.live.seq_id, 7)
        self.assertEqual(obj.last.seq_id, 7)
        obj.clear_live('abort')
        self.assertIsNone(obj.live)
        self.assertEqual(obj.last.seq_id, 7)
        again = recovery.RecoveryState.load(self.state_path)
        self.assertIsNone(again.live)
        self.assertEqual(again.last.seq_id, 7)

    def test_successful_print_clears_live(self):
        printer, config, obj, access = load_recovery(self._cfg())
        obj.note_bookmark(3, filename='b.gcode')
        printer.send_event('print_stats:finish', 'complete')
        self.assertIsNone(obj.live)
        self.assertEqual(obj.last.seq_id, 3)
        boot = recovery.RecoveryState.load(self.state_path)
        self.assertIsNone(boot.live)
        self.assertFalse(boot.should_auto_resume())

    def test_restore_chamber_false_skips(self):
        text = self._cfg("restore_chamber: False\n")
        printer, config, obj, access = load_recovery(text)
        printer.objects['heater_generic chamber'] = object()
        self.assertFalse(obj.should_restore_chamber())

    def test_no_chamber_heater_skips(self):
        printer, config, obj, access = load_recovery(self._cfg())
        self.assertTrue(obj.cfg.restore_chamber)
        self.assertFalse(obj.should_restore_chamber())

    def test_note_accepted_move_persists_bookmark(self):
        printer, config, obj, access = load_recovery(self._cfg())
        class HostMQ:
            def __init__(self, printer):
                self.printer = printer
                self.bookmarks = motion_queuing.BookmarkSeq()
                self.drip_start_times = []
            check_drip_timing = (
                motion_queuing.PrinterMotionQueuing.check_drip_timing)
            note_accepted_move = (
                motion_queuing.PrinterMotionQueuing.note_accepted_move)
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host
        class TH(object):
            def register_lookahead_callback(self, cb):
                raise AssertionError('no MCU bookmark cmds expected')
        seq = host.note_accepted_move(TH())
        self.assertEqual(seq, 1)
        self.assertEqual(obj.live.seq_id, 1)
        self.assertEqual(obj.last.seq_id, 1)
        again = recovery.RecoveryState.load(self.state_path)
        self.assertEqual(again.last.seq_id, 1)
        self.assertEqual(again.live.seq_id, 1)
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('QUERY_BOOKMARK', gcode.commands)
        self.assertIn('_MQ_RECOVERY_CHECKPOINT', gcode.commands)

    def test_drip_skips_bookmark_persist(self):
        printer, config, obj, access = load_recovery(self._cfg())
        class HostMQ:
            def __init__(self, printer):
                self.printer = printer
                self.bookmarks = motion_queuing.BookmarkSeq()
                self.drip_start_times = [1.0]
            check_drip_timing = (
                motion_queuing.PrinterMotionQueuing.check_drip_timing)
            note_accepted_move = (
                motion_queuing.PrinterMotionQueuing.note_accepted_move)
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host
        class TH(object):
            def register_lookahead_callback(self, cb):
                pass
        self.assertIsNone(host.note_accepted_move(TH()))
        self.assertIsNone(obj.live)
        self.assertFalse(os.path.exists(self.state_path))

    def test_script_omits_raw_threshold_waits(self):
        text = self._cfg(
            "bed_temp_threshold: 5\n"
            "chamber_temp_threshold: 3\n"
            "restore_chamber: True\n")
        printer, config, obj, access = load_recovery(text)
        printer.objects['heater_generic chamber'] = object()
        script = '\n'.join(obj.build_recovery_script())
        self.assertNotIn('TEMPERATURE_WAIT', script)
        self.assertNotIn('MINIMUM=5', script)
        self.assertNotIn('MINIMUM=3', script)
        self.assertIn('G28 X Y', script)
        self.assertIn('M117', script)


    def test_mq_config_lookup_deferred_until_connect(self):
        text = self._cfg()
        printer = DummyPrinter()
        access = {}
        fileconfig = configfile.ConfigFileReader().build_fileconfig(
            text, 'test.cfg')
        config = configfile.ConfigWrapper(
            printer, fileconfig, access, 'printer')
        printer.objects['gcode'] = DummyGCode()
        wrap = config.getsection('recovery')
        obj = recovery.load_config(wrap)
        self.assertIsNone(obj.cfg)
        self.assertFalse(obj._consumed)
        mq = mq_config.load_config(config.getsection('mq_config'))
        printer.objects['mq_config'] = mq
        printer.send_event('klippy:connect')
        self.assertTrue(obj._consumed)
        self.assertIs(obj.cfg, mq.recovery)


if __name__ == '__main__':
    unittest.main(verbosity=2)