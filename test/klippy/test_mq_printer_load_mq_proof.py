# Plus stress #3: real Printer._read_config multi-queue load proof.
# Boots Printer (37-style debuginput + dict) with two [queue]; asserts
# MultiLookAhead install + SET_MOTION_QUEUE active routing / merge
# evidence (dual-traj style stub moves). Additive unittest only.
#
# BLOCK: can_drive safety claims; invent parks / sec 12.
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, sys, tempfile, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import reactor
import klippy
from extras.mq_lookahead import MultiLookAhead
from extras.mq_manager import CARRIAGE_AXES

DICT_CANDIDATES = [
    os.path.join(ROOT, '..', 'artifacts', 'tests', 'slice1', 'dict',
                 'atmega2560.dict'),
    os.environ.get('KLIPPY_DICT_DIR', ''),
]


def find_dict():
    for path in DICT_CANDIDATES:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    env_dir = os.environ.get('KLIPPY_DICT_DIR')
    if env_dir:
        cand = os.path.join(env_dir, 'atmega2560.dict')
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    raise unittest.SkipTest(
        'atmega2560.dict not found; set KLIPPY_DICT_DIR or place under '
        'artifacts/tests/slice1/dict/')


class DummyGCmd:
    def __init__(self, **params):
        self._params = dict(
            (k.upper(), str(v)) for k, v in params.items())
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


class CarriageAxisMove:
    """Abstract unit-delta stub (not park / commanded_pos invent)."""
    def __init__(self, advance_axis, min_move_t=0.01, distance=1.0):
        if advance_axis not in CARRIAGE_AXES:
            raise ValueError("bad advance_axis %r" % (advance_axis,))
        self.advance_axis = advance_axis
        self.min_move_t = min_move_t
        self.distance = float(distance)
        self.axes_d = [self.distance, 0., 0., 0.]
        self.start_pos = (0., 0., 0., 0.)
        self.end_pos = (self.distance, 0., 0., 0.)
        self.timing_callbacks = []
        self.delta_v2 = 1.0
        self.max_start_v2 = 0.
        self.max_cruise_v2 = 1.0
        self.mcr_delta_v2 = 1.0
        self.max_mcr_start_v2 = 0.
        self.is_kinematic_move = True
    def calc_junction(self, prev):
        return
    def set_junction(self, start_v2, cruise_v2, end_v2):
        self.start_v = 0.
        self.cruise_v = 1.
        self.end_v = 0.
        self.accel_t = 0.
        self.cruise_t = self.min_move_t
        self.decel_t = 0.


def axis_for_active(mgr):
    q = mgr.active_motion_queue
    idx = mgr.carriage_for_queue(q)
    if idx is None:
        raise AssertionError(
            'active queue %s has no carriage axis' % (q.name,))
    return CARRIAGE_AXES[idx]


def enqueue_owned_advance(th, mgr, min_move_t=0.01, distance=1.0):
    # Route by carriage_for_queue map only (no can_drive safety claim).
    axis = axis_for_active(mgr)
    move = CarriageAxisMove(
        advance_axis=axis, min_move_t=min_move_t, distance=distance)
    th.lookahead.add_move(move)
    return move


class TestPrinterLoadMultiQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger().setLevel(logging.WARNING)
        cls.dict_path = find_dict()
        cls.cfg_twoq = os.path.join(
            os.path.dirname(__file__), 'mq_ownership_loadproof.cfg')

    def _boot_twoq_and_probe(self):
        gcode_path = None
        out_path = None
        gcode_f = None
        try:
            gcode_fobj = tempfile.NamedTemporaryFile(
                mode='w', suffix='.gcode', delete=False, encoding='ascii')
            gcode_path = gcode_fobj.name
            gcode_fobj.write('M400\n')
            gcode_fobj.close()
            out_fobj = tempfile.NamedTemporaryFile(delete=False)
            out_path = out_fobj.name
            out_fobj.close()
            gcode_f = open(gcode_path, 'rb')
            start_args = {
                'config_file': self.cfg_twoq,
                'apiserver': None,
                'start_reason': 'startup',
                'debuginput': gcode_path,
                'gcode_fd': gcode_f.fileno(),
                'debugoutput': out_path,
                'dictionary': self.dict_path,
                'software_version': 'mq-plus-printer-load-mq',
                'cpu_info': 'test',
            }
            main_reactor = reactor.Reactor()
            printer = klippy.Printer(main_reactor, None, start_args)
            box = {}

            def on_ready():
                mgr = printer.lookup_object('mq_manager')
                mq = printer.lookup_object('mq_config')
                th = printer.lookup_object('toolhead')
                box['mgr'] = mgr
                box['mq'] = mq
                box['th'] = th
                box['ready'] = True
                # --- MultiLookAhead + SET_MOTION_QUEUE evidence ---
                self.assertTrue(mgr.ownership.multi_queue)
                self.assertEqual(len(mgr.queues), 2)
                q0 = mgr.lookup_queue('q_T0')
                q1 = mgr.lookup_queue('q_T1')
                self.assertIs(mgr.primary, q0)
                self.assertIsInstance(th.lookahead, MultiLookAhead)
                self.assertIs(mgr.multi_lookahead, th.lookahead)
                self.assertIn('q_T0', mgr.lookaheads)
                self.assertIn('q_T1', mgr.lookaheads)
                self.assertIsNot(
                    mgr.lookaheads['q_T0'], mgr.lookaheads['q_T1'])
                facade = th.lookahead
                self.assertIs(mgr.active_motion_queue, q0)

                # Interleave SET_MOTION_QUEUE; facade pointer stable
                m0 = enqueue_owned_advance(
                    th, mgr, min_move_t=0.02, distance=2.0)
                self.assertEqual(m0.advance_axis, 'x')
                self.assertEqual(len(mgr.lookaheads['q_T0'].queue), 1)
                self.assertEqual(len(mgr.lookaheads['q_T1'].queue), 0)

                mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
                self.assertIs(th.lookahead, facade)
                self.assertIs(mgr.active_motion_queue, q1)
                m1 = enqueue_owned_advance(
                    th, mgr, min_move_t=0.02, distance=3.0)
                self.assertEqual(m1.advance_axis, 'dual_carriage')
                self.assertEqual(len(mgr.lookaheads['q_T1'].queue), 1)

                mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
                self.assertIs(th.lookahead, facade)
                self.assertIs(mgr.active_motion_queue, q0)
                m2 = enqueue_owned_advance(
                    th, mgr, min_move_t=0.02, distance=4.0)
                self.assertEqual(m2.advance_axis, 'x')

                merged = th.lookahead.flush(lazy=False)
                axes = [m.advance_axis for m in merged]
                box['axes'] = axes
                box['mq_print_times'] = [m.mq_print_time for m in merged]
                box['merged_len'] = len(merged)
                self.assertEqual(len(merged), 3)
                self.assertEqual(
                    axes, ['x', 'dual_carriage', 'x'])
                self.assertEqual(
                    box['mq_print_times'], [0., 0., 0.02])
                self.assertEqual(
                    set(axes), {'x', 'dual_carriage'})
                self.assertTrue(mgr.lookaheads['q_T0'].is_empty())
                self.assertTrue(mgr.lookaheads['q_T1'].is_empty())
                self.assertTrue(th.lookahead.is_empty())
                box['evidence_ok'] = True
                printer.request_exit('exit')

            def on_connect():
                box['connect'] = True

            printer.register_event_handler('klippy:ready', on_ready)
            printer.register_event_handler('klippy:connect', on_connect)
            res = printer.run()
            state_msg, category = printer.get_state_message()
            self.assertEqual(
                category, 'ready',
                'Printer did not become ready: %s' % (state_msg,))
            self.assertEqual(res, 'exit')
            self.assertTrue(box.get('ready'), box)
            self.assertTrue(box.get('evidence_ok'), box)
            return printer, box
        finally:
            if gcode_f is not None:
                gcode_f.close()
            for path in (gcode_path, out_path):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def test_printer_load_two_queue_set_motion_multila(self):
        printer, box = self._boot_twoq_and_probe()
        mgr = box['mgr']
        th = box['th']
        self.assertIs(printer.lookup_object('mq_manager'), mgr)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        self.assertEqual(box['merged_len'], 3)
        self.assertEqual(
            box['axes'], ['x', 'dual_carriage', 'x'])
        # Ownership map present (not can_drive safety claims)
        self.assertEqual(
            mgr.get_status()['exclusive']['x'], 'q_T0')
        self.assertEqual(
            mgr.get_status()['exclusive']['dual_carriage'], 'q_T1')
        self.assertEqual(mgr.carriage_for_queue(
            mgr.lookup_queue('q_T0')), 0)
        self.assertEqual(mgr.carriage_for_queue(
            mgr.lookup_queue('q_T1')), 1)


if __name__ == '__main__':
    os.chdir(ROOT)
    unittest.main(verbosity=2)
