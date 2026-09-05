# Ownership load-proof via real Printer._read_config (not DummyPrinter-only)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, tempfile, unittest, logging

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import reactor
import klippy

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


class RealPrinterLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger().setLevel(logging.WARNING)
        cls.dict_path = find_dict()
        cls.cfg_twoq = os.path.join(
            os.path.dirname(__file__), 'mq_ownership_loadproof.cfg')
        cls.cfg_dual = os.path.join(
            os.path.dirname(__file__), 'dual_carriage.cfg')

    def _boot(self, config_file):
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
                'config_file': config_file,
                'apiserver': None,
                'start_reason': 'startup',
                'debuginput': gcode_path,
                'gcode_fd': gcode_f.fileno(),
                'debugoutput': out_path,
                'dictionary': self.dict_path,
                'software_version': 'mq-ownership-loadproof',
                'cpu_info': 'test',
            }
            main_reactor = reactor.Reactor()
            printer = klippy.Printer(main_reactor, None, start_args)
            box = {}
            def on_ready():
                box['mgr'] = printer.lookup_object('mq_manager')
                box['mq'] = printer.lookup_object('mq_config')
                box['ready'] = True
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
            return printer, box['mgr'], box['mq']
        finally:
            if gcode_f is not None:
                gcode_f.close()
            for path in (gcode_path, out_path):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def test_two_queue_ownership_via_read_config(self):
        printer, mgr, mq = self._boot(self.cfg_twoq)
        self.assertIs(printer.lookup_object('mq_manager'), mgr)
        self.assertIs(printer.lookup_object('mq_config'), mq)
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertTrue(mgr.get_status()['multi_queue'])
        q0 = mgr.primary
        q1 = mgr.lookup_queue('q_T1')
        self.assertEqual(q0.name, 'q_T0')
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(mgr.ownership.exclusive['dual_carriage'], q1)
        st = mgr.get_status()
        self.assertEqual(st['exclusive']['x'], 'q_T0')
        self.assertEqual(st['exclusive']['dual_carriage'], 'q_T1')
        self.assertTrue(mgr.can_drive(q0, 'x'))
        self.assertTrue(mgr.can_drive(q1, 'dual_carriage'))
        self.assertFalse(mgr.can_drive(q0, 'dual_carriage'))
        self.assertFalse(mgr.can_drive(q1, 'x'))
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))
        mgr.ownership.claim(q0, 'y')
        self.assertTrue(mgr.can_drive(q0, 'y'))
        self.assertFalse(mgr.can_drive(q1, 'y'))
        self.assertEqual(mgr.get_status()['shareable']['y'], 'q_T0')
        mgr.ownership.release(q0, 'y')
        self.assertFalse(mgr.can_drive(q0, 'y'))
        self.assertIsNone(mgr.get_status()['shareable']['y'])

    def test_stock_dual_carriage_still_single_queue(self):
        printer, mgr, mq = self._boot(self.cfg_dual)
        self.assertIs(printer.lookup_object('mq_manager'), mgr)
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertFalse(mgr.get_status()['multi_queue'])
        self.assertEqual(len(mgr.queues), 1)
        self.assertEqual(mgr.get_status()['exclusive'], {})
        self.assertTrue(mgr.can_drive(mgr.primary, 'x'))
        self.assertTrue(mgr.can_drive(mgr.primary, 'dual_carriage'))


if __name__ == '__main__':
    os.chdir(ROOT)
    unittest.main(verbosity=2)
