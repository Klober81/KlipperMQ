# Prove-first: redundant last-bookmark persistence sinks.
# CUT (#3): note_bookmark writes last bookmark to (1) recovery
# file, (2) print log via gcode.respond_info, (3) Klipper log via
# logging.info. QUERY_BOOKMARK / RESUME_* stay OPEN; do not close
# ARCHITECTURE section 12.
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
        self.infos = []
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)
    def respond_info(self, msg):
        self.infos.append(msg)


class HostMQ:
    """Real note_accepted_move; stub flush machinery."""
    def __init__(self, printer):
        self.printer = printer
        self.bookmarks = motion_queuing.BookmarkSeq()
        self.drip_start_times = []
    check_drip_timing = (
        motion_queuing.PrinterMotionQueuing.check_drip_timing)
    note_accepted_move = (
        motion_queuing.PrinterMotionQueuing.note_accepted_move)


def load_recovery(text, state_path):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    printer.objects['print_stats'] = type(
        'PS', (), {'filename': 'job.gcode'})()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    obj = None
    if fileconfig.has_section('recovery'):
        wrap = config.getsection('recovery')
        mq.recovery.filename = state_path
        obj = recovery.load_config(wrap)
        printer.objects['recovery'] = obj
    printer.send_event('klippy:connect')
    return printer, obj


class TestMqBookmarkPersistProof(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix='mq_bmp_', suffix='.state', delete=False)
        self._tmp.close()
        self.state_path = self._tmp.name
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def tearDown(self):
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def _cfg(self):
        path = self.state_path.replace('\\', '/')
        return (
            "[recovery]\n"
            "z_hop_on_recover: 5\n"
            "filename: %s\n"
        ) % (path,)

    def _assert_three_sinks(self, printer, seq, filename,
                            log_cm):
        # (1) dedicated recovery file
        again = recovery.RecoveryState.load(self.state_path)
        self.assertIsNotNone(again.last)
        self.assertEqual(again.last.seq_id, seq)
        self.assertEqual(again.live.seq_id, seq)
        if filename is not None:
            self.assertEqual(again.last.filename, filename)
        # (2) print log sink (gcode.respond_info)
        gcode = printer.lookup_object('gcode')
        needle = 'recovery: last bookmark seq=%u' % (seq,)
        self.assertTrue(
            any(needle in m for m in gcode.infos),
            gcode.infos)
        # (3) Klipper log sink (logging.info)
        self.assertTrue(
            any(needle in r.getMessage()
                for r in log_cm.records),
            [r.getMessage() for r in log_cm.records])

    def test_note_bookmark_writes_all_three_sinks(self):
        printer, obj = load_recovery(self._cfg(), self.state_path)
        with self.assertLogs(level='INFO') as log_cm:
            rec = obj.note_bookmark(
                42, filename='persist.gcode')
        self.assertEqual(rec.seq_id, 42)
        self._assert_three_sinks(
            printer, 42, 'persist.gcode', log_cm)

    def test_motion_path_writes_all_three_sinks(self):
        # motion_queuing.note_accepted_move -> note_bookmark
        printer, obj = load_recovery(self._cfg(), self.state_path)
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host

        class TH(object):
            def register_lookahead_callback(self, cb):
                pass

        with self.assertLogs(level='INFO') as log_cm:
            seq = host.note_accepted_move(TH())
        self.assertEqual(seq, 1)
        self._assert_three_sinks(
            printer, 1, 'job.gcode', log_cm)

    def test_open_apis_remain_open(self):
        printer, obj = load_recovery(self._cfg(), self.state_path)
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('QUERY_BOOKMARK', gcode.commands)
        self.assertNotIn('RESUME_BOOKMARK', gcode.commands)
        self.assertNotIn('RESUME_FROM_BOOKMARK', gcode.commands)
        rec_path = os.path.join(
            ROOT, 'klippy', 'extras', 'recovery.py')
        with open(rec_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('_log_bookmark', src)
        self.assertIn('logging.info(msg)', src)
        self.assertIn('gcode.respond_info(msg)', src)
        self.assertIn('self._persist()', src)
        self.assertIn('QUERY_BOOKMARK / RESUME_*', src)
        self.assertIn(
            'Do not close ARCHITECTURE section 12', src)
        self.assertNotIn('section 12 closed', src.lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)
