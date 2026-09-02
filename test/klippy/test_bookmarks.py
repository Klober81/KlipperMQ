# In-process tests for file-stable motion bookmarks
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, sys, types, unittest

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
import extras.print_stats as print_stats


class DummyPrinter:
    def __init__(self):
        self.objects = {}
        self.event_handlers = {}
    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)
    def load_object(self, config, name):
        return self.objects[name]
    def get_reactor(self):
        return self.objects['reactor']
    def lookup_objects(self, module=None):
        if module is None:
            return list(self.objects.items())
        prefix = module + ' '
        objs = [(n, o) for n, o in self.objects.items()
                if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs
    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)
    def send_event(self, event, *params):
        for cb in self.event_handlers.get(event, []):
            cb(*params)


class DummyCmd:
    def __init__(self):
        self.sent = []
    def send(self, data=(), minclock=0, reqclock=0):
        self.sent.append((tuple(data), minclock, reqclock))


class DummyMCU:
    def __init__(self, cap=False):
        self.cap = cap
        self.cmd = DummyCmd()
        self.echo = None
        self.config_cbs = []
    def get_constants(self):
        if self.cap:
            return {'BOOKMARK': 1}
        return {}
    def try_lookup_command(self, msgformat):
        if not self.cap:
            return None
        return self.cmd
    def register_serial_response(self, cb, msg, oid=None):
        self.echo = (cb, msg)
    def register_config_callback(self, cb):
        self.config_cbs.append(cb)
    def print_time_to_clock(self, print_time):
        return int(print_time * 1000)


class DummyToolHead:
    def __init__(self):
        self.callbacks = []
    def register_lookahead_callback(self, callback):
        self.callbacks.append(callback)


def replay_ids(n, cap=False):
    b = motion_queuing.BookmarkSeq()
    if cap:
        b.bind_mcus([DummyMCU(cap=True)])
    th = DummyToolHead()
    return [b.note_accepted_move(th) for _ in range(n)]


class TestBookmarks(unittest.TestCase):
    def test_same_command_sequence_same_ids(self):
        self.assertEqual(replay_ids(5), replay_ids(5))
        self.assertEqual(replay_ids(5), [1, 2, 3, 4, 5])

    def test_ids_are_ints_without_clock(self):
        ids = replay_ids(3)
        for seq in ids:
            self.assertIsInstance(seq, int)
            self.assertNotIsInstance(seq, bool)
            self.assertGreater(seq, 0)
            text = str(seq)
            self.assertNotIn('T', text)
            self.assertNotIn(':', text)
            self.assertNotIn('.', text)

    def test_no_bookmark_section_required(self):
        b = motion_queuing.BookmarkSeq()
        th = DummyToolHead()
        self.assertEqual(b.note_accepted_move(th), 1)
        self.assertEqual(b.last_id, 1)

    def test_without_mcu_cap_warns_and_still_assigns(self):
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=False)
        th = DummyToolHead()
        with self.assertLogs(level='WARNING') as cm:
            b.bind_mcus([mcu])
        self.assertTrue(any('bookmark' in m.lower() for m in cm.output))
        seq = b.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(th.callbacks, [])
        self.assertEqual(mcu.cmd.sent, [])

    def test_with_mcu_cap_emits_timed_command(self):
        b = motion_queuing.BookmarkSeq()
        mcu = DummyMCU(cap=True)
        th = DummyToolHead()
        b.bind_mcus([mcu])
        seq = b.note_accepted_move(th)
        self.assertEqual(seq, 1)
        self.assertEqual(len(th.callbacks), 1)
        th.callbacks[0](1.25)
        data, minclock, reqclock = mcu.cmd.sent[0]
        self.assertEqual(data, (1250, 1))
        self.assertEqual(reqclock, 1250)
        self.assertIsNotNone(mcu.echo)
        b._handle_echo({'seq': 1, 'clock': 1250})
        self.assertEqual(b.last_echo_id, 1)

    def test_insertion_is_lookahead_not_timer_flush(self):
        th_path = os.path.join(ROOT, 'klippy', 'toolhead.py')
        mq_path = os.path.join(ROOT, 'klippy', 'extras',
                               'motion_queuing.py')
        with open(th_path, encoding='utf-8') as f:
            th = f.read()
        with open(mq_path, encoding='utf-8') as f:
            mq = f.read()
        add_at = th.find('lookahead.add_move')
        note_at = th.find('note_accepted_move')
        self.assertGreater(add_at, 0)
        self.assertGreater(note_at, add_at)
        self.assertIn('class BookmarkSeq', mq)
        self.assertNotIn('MOVE_BATCH_TIME', th)
        self.assertNotIn('[bookmark]', mq.lower())
        self.assertNotIn('MOVE_BATCH_TIME', mq)

    def test_motion_queuing_exposes_note_accepted_move(self):
        self.assertTrue(hasattr(motion_queuing.PrinterMotionQueuing,
                                'note_accepted_move'))
        self.assertTrue(hasattr(motion_queuing, 'BookmarkSeq'))

    def _make_print_stats(self):
        printer = DummyPrinter()
        class Pos:
            e = 0.0
        class GCodeMove:
            def get_status(self, eventtime=None):
                return {'position': Pos(), 'extrude_factor': 1.0}
        class Reactor:
            def monotonic(self):
                return 10.0
        class GCode:
            def register_command(self, *a, **k):
                pass
        printer.objects['gcode_move'] = GCodeMove()
        printer.objects['reactor'] = Reactor()
        printer.objects['gcode'] = GCode()
        class Cfg:
            def get_printer(self):
                return printer
        return printer, print_stats.PrintStats(Cfg())

    def test_print_start_reset_repeats_ids(self):
        printer, ps = self._make_print_stats()
        b = motion_queuing.BookmarkSeq()
        printer.register_event_handler('print_stats:start', b.reset)
        th = DummyToolHead()
        ps.note_start()
        first = [b.note_accepted_move(th) for _ in range(3)]
        ps.note_complete()
        ps.note_start()
        second = [b.note_accepted_move(th) for _ in range(3)]
        self.assertEqual(first, [1, 2, 3])
        self.assertEqual(second, [1, 2, 3])

    def test_pause_resume_does_not_reset_ids(self):
        printer, ps = self._make_print_stats()
        b = motion_queuing.BookmarkSeq()
        printer.register_event_handler('print_stats:start', b.reset)
        th = DummyToolHead()
        ps.note_start()
        first = [b.note_accepted_move(th) for _ in range(3)]
        ps.note_start()
        more = [b.note_accepted_move(th) for _ in range(3)]
        self.assertEqual(first, [1, 2, 3])
        self.assertEqual(more, [4, 5, 6])

    def test_print_stats_start_event_before_time(self):
        printer, ps = self._make_print_stats()
        seen = []
        def on_start():
            seen.append(ps.print_start_time)
        printer.register_event_handler('print_stats:start', on_start)
        ps.note_start()
        self.assertEqual(seen, [None])
        self.assertIsNotNone(ps.print_start_time)
        ps.note_start()
        self.assertEqual(len(seen), 1)

    def test_print_start_is_event_not_wrap(self):
        mq_path = os.path.join(ROOT, 'klippy', 'extras',
                               'motion_queuing.py')
        ps_path = os.path.join(ROOT, 'klippy', 'extras',
                               'print_stats.py')
        th_path = os.path.join(ROOT, 'klippy', 'toolhead.py')
        with open(mq_path, encoding='utf-8') as f:
            mq = f.read()
        with open(ps_path, encoding='utf-8') as f:
            ps = f.read()
        with open(th_path, encoding='utf-8') as f:
            th = f.read()
        self.assertNotIn('hook_print_start', mq)
        self.assertNotIn('note_start =', mq)
        self.assertNotIn('work_handler', mq)
        self.assertIn('print_stats:start', mq)
        self.assertIn('self.bookmarks.reset', mq)
        body = ps[ps.find('def note_start'):ps.find('def note_pause')]
        ev = body.find('print_stats:start')
        tm = body.find('self.print_start_time = curtime')
        self.assertGreater(ev, 0)
        self.assertGreater(tm, ev)
        drip = th[th.find('def _drip_load_trapq'):th.find('def drip_move')]
        self.assertNotIn('note_accepted_move', drip)
        self.assertIn('if not self._in_drip:', th)


if __name__ == '__main__':
    unittest.main()
