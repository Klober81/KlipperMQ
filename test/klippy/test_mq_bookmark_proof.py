# Prove-first: host bookmark insert+echo / recovery.note_bookmark.
# CUT (#3): Host inserts a deterministic seq into the motion stream
# (QUEUE_BOOKMARK via lookahead when MCU advertises BOOKMARK), MCU
# echo updates last_echo_id, and recovery.note_bookmark persists the
# host-assigned seq. QUERY_BOOKMARK / RESUME_* stay OPEN; do not
# close ARCHITECTURE section 12.
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


QUEUE_BOOKMARK = motion_queuing.QUEUE_BOOKMARK
BOOKMARK_ECHO = motion_queuing.BOOKMARK_ECHO


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


class DummyCmd:
    def __init__(self):
        self.sent = []
    def send(self, data=(), minclock=0, reqclock=0):
        self.sent.append((tuple(data), minclock, reqclock))


class DummyMCU:
    def __init__(self, cap=True):
        self.cap = cap
        self.cmd = DummyCmd()
        self.echo = None
        self.lookup_msg = None
    def get_constants(self):
        if self.cap:
            return {'BOOKMARK': 1}
        return {}
    def try_lookup_command(self, msgformat):
        self.lookup_msg = msgformat
        if not self.cap:
            return None
        return self.cmd
    def register_serial_response(self, cb, msg, oid=None):
        self.echo = (cb, msg)
    def print_time_to_clock(self, print_time):
        return int(print_time * 1000)


class DummyToolHead:
    def __init__(self):
        self.callbacks = []
    def register_lookahead_callback(self, callback):
        self.callbacks.append(callback)


class HostMQ:
    """Real note_accepted_move / drip gate; stub flush machinery."""
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


class TestMqBookmarkProof(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix='mq_bm_', suffix='.state', delete=False)
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

    def test_host_insert_emits_queue_bookmark_and_echo(self):
        # Host insert lands in the timed stream via lookahead;
        # echo updates last_echo_id (MCU receipt confirmation).
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=True)
        th = DummyToolHead()
        b.bind_mcus([mcu])
        self.assertEqual(mcu.lookup_msg, QUEUE_BOOKMARK)
        self.assertIsNotNone(mcu.echo)
        self.assertEqual(mcu.echo[1], BOOKMARK_ECHO)
        seq = b.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(len(th.callbacks), 1)
        th.callbacks[0](2.5)
        data, minclock, reqclock = mcu.cmd.sent[0]
        self.assertEqual(data, (2500, 1))
        self.assertEqual(reqclock, 2500)
        mcu.echo[0]({'seq': 1, 'clock': 2500})
        self.assertEqual(b.last_echo_id, 1)
        self.assertEqual(b.last_id, 1)

    def test_note_accepted_move_records_via_note_bookmark(self):
        printer, obj = load_recovery(self._cfg(), self.state_path)
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host
        mcu = DummyMCU(cap=True)
        host.bookmarks.bind_mcus([mcu])
        th = DummyToolHead()
        seq = host.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(obj.live.seq_id, 1)
        self.assertEqual(obj.last.seq_id, 1)
        self.assertEqual(obj.live.filename, 'job.gcode')
        # Stream emit still scheduled after persist.
        self.assertEqual(len(th.callbacks), 1)
        th.callbacks[0](1.0)
        self.assertEqual(mcu.cmd.sent[0][0], (1000, 1))
        mcu.echo[0]({'seq': 1, 'clock': 1000})
        self.assertEqual(host.bookmarks.last_echo_id, 1)
        again = recovery.RecoveryState.load(self.state_path)
        self.assertEqual(again.live.seq_id, 1)
        self.assertEqual(again.last.seq_id, 1)
        gcode = printer.lookup_object('gcode')
        self.assertTrue(
            any('last bookmark seq=1' in m for m in gcode.infos))

    def test_stock_without_recovery_still_assigns_seq(self):
        # No [recovery]: insert+echo still work; no persist crash.
        printer = DummyPrinter()
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host
        mcu = DummyMCU(cap=True)
        host.bookmarks.bind_mcus([mcu])
        th = DummyToolHead()
        seq = host.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertIsNone(printer.lookup_object('recovery', None))
        th.callbacks[0](0.5)
        self.assertEqual(mcu.cmd.sent[0][0], (500, 1))
        mcu.echo[0]({'seq': 1, 'clock': 500})
        self.assertEqual(host.bookmarks.last_echo_id, 1)
        self.assertFalse(os.path.exists(self.state_path))

    def test_drip_skips_insert_and_persist(self):
        printer, obj = load_recovery(self._cfg(), self.state_path)
        host = HostMQ(printer)
        host.drip_start_times = [1.0]
        printer.objects['motion_queuing'] = host
        th = DummyToolHead()
        self.assertIsNone(host.note_accepted_move(th))
        self.assertIsNone(obj.live)
        self.assertEqual(th.callbacks, [])
        self.assertFalse(os.path.exists(self.state_path))

    def test_open_apis_not_invented(self):
        printer, obj = load_recovery(self._cfg(), self.state_path)
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('QUERY_BOOKMARK', gcode.commands)
        self.assertNotIn('RESUME_BOOKMARK', gcode.commands)
        self.assertNotIn('RESUME_FROM_BOOKMARK', gcode.commands)
        # Provisional internal checkpoint only (OPEN).
        self.assertIn('_MQ_RECOVERY_CHECKPOINT', gcode.commands)
        rec_path = os.path.join(
            ROOT, 'klippy', 'extras', 'recovery.py')
        with open(rec_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('QUERY_BOOKMARK / RESUME_*', src)
        self.assertIn('Do not close ARCHITECTURE section 12', src)
        self.assertIn('def note_bookmark', src)
        # Proof does not claim section 12 closed.
        self.assertNotIn('section 12 closed', src.lower())

    def test_protocol_spelling_matches_arch_host_path(self):
        self.assertEqual(
            QUEUE_BOOKMARK, 'queue_bookmark clock=%u seq=%u')
        self.assertEqual(
            BOOKMARK_ECHO, 'bookmark_echo clock=%u seq=%u')
        th_path = os.path.join(ROOT, 'klippy', 'toolhead.py')
        with open(th_path, encoding='utf-8') as f:
            th = f.read()
        # Host insert on accepted move (non-drip); no drip insert.
        self.assertIn('note_accepted_move', th)
        self.assertIn('if not self._in_drip:', th)
        drip = th[th.find('def _drip_load_trapq'):
                  th.find('def drip_move')]
        self.assertNotIn('note_accepted_move', drip)


if __name__ == '__main__':
    unittest.main(verbosity=2)
