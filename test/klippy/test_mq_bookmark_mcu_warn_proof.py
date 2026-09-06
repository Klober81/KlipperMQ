# Prove-first: Bookmark MCU unsupported-capability warn.
# CUT (#3): When MCU lacks BOOKMARK capability,
# BookmarkSeq.warn_if_needed emits one WARNING; host still
# assigns sequence IDs; no queue_bookmark emit; motion path
# still works. No new protocol spellings.
# QUERY_BOOKMARK / RESUME_* stay OPEN; do not close
# ARCHITECTURE section 12.
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, logging, os, sys, types, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))


def _stub_chelper():
    if 'chelper' in sys.modules:
        return
    fake = types.ModuleType('chelper')
    class _Ffi:
        NULL = None
        def gc(self, *a, **k):
            return None
    class _Lib:
        def trapq_finalize_moves(self, *a, **k):
            return None
        def steppersyncmgr_alloc(self, *a, **k):
            return 1
        def steppersyncmgr_free(self, *a, **k):
            return None
        def steppersyncmgr_gen_steps(self, *a, **k):
            return 0
    def get_ffi():
        return _Ffi(), _Lib()
    fake.get_ffi = get_ffi
    sys.modules['chelper'] = fake

_stub_chelper()
import extras.motion_queuing as motion_queuing


QUEUE_BOOKMARK = motion_queuing.QUEUE_BOOKMARK
BOOKMARK_ECHO = motion_queuing.BOOKMARK_ECHO
WARN_SNIP = 'MCU does not advertise bookmark support'


class DummyCmd:
    def __init__(self):
        self.sent = []
    def send(self, data=(), minclock=0, reqclock=0):
        self.sent.append((tuple(data), minclock, reqclock))


class DummyMCU:
    def __init__(self, cap=False, constants=None):
        self.cap = cap
        self._constants = constants
        self.cmd = DummyCmd()
        self.echo = None
        self.lookup_msg = None
        self.config_cbs = []
    def get_constants(self):
        if self._constants is not None:
            return self._constants
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
    def register_config_callback(self, cb):
        self.config_cbs.append(cb)
    def print_time_to_clock(self, print_time):
        return int(print_time * 1000)
    def is_fileoutput(self):
        return True
    def estimated_print_time(self, eventtime):
        return eventtime


class DummyToolHead:
    def __init__(self):
        self.callbacks = []
    def register_lookahead_callback(self, callback):
        self.callbacks.append(callback)


class DummyReactor:
    NOW = 0.
    NEVER = 999999999.9
    def register_timer(self, callback, waketime=None):
        return (callback, waketime)
    def update_timer(self, timer, waketime):
        return None
    def monotonic(self):
        return 0.


class DummyPrinter:
    def __init__(self, mcus=None):
        self.objects = collections.OrderedDict()
        self.event_handlers = {}
        if mcus is None:
            mcus = [DummyMCU(cap=False)]
        for i, m in enumerate(mcus):
            name = 'mcu' if i == 0 else 'mcu m%d' % (i,)
            self.objects[name] = m
        self.objects['reactor'] = DummyReactor()
    def get_reactor(self):
        return self.objects['reactor']
    def lookup_object(self, name, default=None):
        if name in self.objects:
            return self.objects[name]
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


class DummyConfig:
    def __init__(self, printer):
        self._printer = printer
    def get_printer(self):
        return self._printer


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


class TestMqBookmarkMcuWarnProof(unittest.TestCase):
    def test_warn_when_mcu_lacks_bookmark_cap(self):
        # bind_mcus -> warn_if_needed; host seq still assigned.
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=False)
        th = DummyToolHead()
        with self.assertLogs(level='WARNING') as cm:
            b.bind_mcus([mcu])
        self.assertTrue(any(WARN_SNIP in m for m in cm.output))
        self.assertTrue(b._warned)
        self.assertEqual(b._cmds, [])
        seq = b.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(b.last_id, 1)
        # Motion still works: no MCU emit, no lookahead hook.
        self.assertEqual(th.callbacks, [])
        self.assertEqual(mcu.cmd.sent, [])
        self.assertIsNone(mcu.echo)

    def test_warn_once_even_if_called_again(self):
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=False)
        with self.assertLogs(level='WARNING') as cm:
            b.bind_mcus([mcu])
        first = [m for m in cm.output if WARN_SNIP in m]
        self.assertEqual(len(first), 1)
        # Second pass must not re-warn.
        with self.assertLogs(level='WARNING') as cm2:
            b.warn_if_needed()
            b.bind_mcus([mcu])
            # Force a log so assertLogs has something if silent.
            logging.warning('probe-no-dup')
        again = [m for m in cm2.output if WARN_SNIP in m]
        self.assertEqual(again, [])

    def test_bookmark_zero_constant_warns(self):
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=False, constants={'BOOKMARK': 0})
        with self.assertLogs(level='WARNING') as cm:
            b.bind_mcus([mcu])
        self.assertTrue(any(WARN_SNIP in m for m in cm.output))
        th = DummyToolHead()
        self.assertEqual(b.note_accepted_move(th), 1)
        self.assertEqual(th.callbacks, [])

    def test_with_cap_no_warn_and_emits(self):
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=True)
        th = DummyToolHead()
        # No warning when MCU advertises BOOKMARK.
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.WARNING)
        try:
            with self.assertRaises(AssertionError):
                with self.assertLogs(level='WARNING'):
                    b.bind_mcus([mcu])
                    logging.getLogger(
                        'silent').debug('no warn expected')
        finally:
            root.setLevel(old)
        self.assertFalse(b._warned)
        self.assertEqual(len(b._cmds), 1)
        seq = b.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(len(th.callbacks), 1)
        th.callbacks[0](1.0)
        self.assertEqual(mcu.cmd.sent[0][0], (1000, 1))

    def test_connect_path_warns_after_bind_one(self):
        # Production wiring: config cb -> bind_one; connect -> warn.
        printer = DummyPrinter(mcus=[DummyMCU(cap=False)])
        cfg = DummyConfig(printer)
        mq = motion_queuing.PrinterMotionQueuing(cfg)
        mcu = printer.objects['mcu']
        self.assertTrue(mcu.config_cbs)
        for cb in mcu.config_cbs:
            cb()
        self.assertEqual(mq.bookmarks._cmds, [])
        with self.assertLogs(level='WARNING') as cm:
            printer.send_event('klippy:connect')
        self.assertTrue(any(WARN_SNIP in m for m in cm.output))
        self.assertTrue(mq.bookmarks._warned)
        th = DummyToolHead()
        seq = mq.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(th.callbacks, [])

    def test_host_motion_path_without_cap(self):
        printer = DummyPrinter()
        host = HostMQ(printer)
        printer.objects['motion_queuing'] = host
        mcu = DummyMCU(cap=False)
        with self.assertLogs(level='WARNING') as cm:
            host.bookmarks.bind_mcus([mcu])
        self.assertTrue(any(WARN_SNIP in m for m in cm.output))
        th = DummyToolHead()
        seq = host.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(th.callbacks, [])
        self.assertEqual(mcu.cmd.sent, [])
        # Second move still works (motion not gated on MCU cap).
        self.assertEqual(host.note_accepted_move(th), 2)

    def test_open_apis_not_invented(self):
        mq_path = os.path.join(
            ROOT, 'klippy', 'extras', 'motion_queuing.py')
        with open(mq_path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('def warn_if_needed', src)
        self.assertIn(WARN_SNIP, src)
        self.assertIn(
            'host still assigns sequence IDs', src)
        self.assertNotIn('QUERY_BOOKMARK', src)
        self.assertNotIn('RESUME_BOOKMARK', src)
        self.assertNotIn('RESUME_FROM_BOOKMARK', src)
        rec_path = os.path.join(
            ROOT, 'klippy', 'extras', 'recovery.py')
        with open(rec_path, encoding='utf-8') as f:
            rec = f.read()
        self.assertIn('QUERY_BOOKMARK / RESUME_*', rec)
        self.assertIn(
            'Do not close ARCHITECTURE section 12', rec)
        self.assertNotIn('section 12 closed', rec.lower())

    def test_protocol_spellings_unchanged(self):
        self.assertEqual(
            QUEUE_BOOKMARK, 'queue_bookmark clock=%u seq=%u')
        self.assertEqual(
            BOOKMARK_ECHO, 'bookmark_echo clock=%u seq=%u')


if __name__ == '__main__':
    unittest.main(verbosity=2)
