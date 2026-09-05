# COPY/MIRROR load-proof via real Printer._read_config (not DummyPrinter-only)
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


class _GCmd:
    def error(self, msg):
        raise AssertionError(msg)


class RealPrinterCopyMirrorLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger().setLevel(logging.WARNING)
        cls.dict_path = find_dict()
        cls.cfg_cm = os.path.join(
            os.path.dirname(__file__), 'mq_copy_mirror_loadproof.cfg')
        cls.cfg_dual = os.path.join(
            os.path.dirname(__file__), 'dual_carriage.cfg')

    def _boot(self, config_file, on_ready_extra=None):
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
                'software_version': 'mq-copy-mirror-loadproof',
                'cpu_info': 'test',
            }
            main_reactor = reactor.Reactor()
            printer = klippy.Printer(main_reactor, None, start_args)
            box = {}
            def on_ready():
                box['mgr'] = printer.lookup_object('mq_manager')
                box['mq'] = printer.lookup_object('mq_config')
                box['gcode'] = printer.lookup_object('gcode')
                box['cm'] = printer.lookup_object('copy_mirror', None)
                box['ready'] = True
                if on_ready_extra is not None:
                    on_ready_extra(printer, box)
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

    def test_commands_registered_after_boot(self):
        printer, box = self._boot(self.cfg_cm)
        gcode = box['gcode']
        self.assertIn('COPY', gcode.ready_gcode_handlers)
        self.assertIn('MIRROR', gcode.ready_gcode_handlers)
        self.assertIn('COPY_OFF', gcode.ready_gcode_handlers)
        cm = box['cm']
        self.assertIsNotNone(cm)
        self.assertIs(printer.lookup_object('copy_mirror'), cm)
        mq = box['mq']
        self.assertIsNotNone(mq.copy)
        self.assertIsNotNone(mq.mirror)
        self.assertEqual(mq.mirror.axis, 'x')
        self.assertEqual(mq.mirror.center, 100.)
        mgr = box['mgr']
        self.assertTrue(mgr.ownership.multi_queue)
        self.assertIs(mgr.ownership.exclusive['x'], mgr.primary)
        self.assertEqual(mgr.lookup_queue('q_T1').name, 'q_T1')

    def test_emit_line_order_copy_mirror_off(self):
        captured = []

        def on_ready_extra(printer, box):
            gcode = box['gcode']
            cm = box['cm']
            self.assertIsNotNone(cm)

            def capture(script):
                captured.append(script)

            gcode.run_script_from_command = capture
            gcmd = _GCmd()
            cm.cmd_COPY(gcmd)
            cm.cmd_MIRROR(gcmd)
            cm.cmd_COPY_OFF(gcmd)

        printer, box = self._boot(self.cfg_cm, on_ready_extra)
        self.assertEqual(len(captured), 3, captured)
        self.assertEqual(
            captured[0].split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        self.assertEqual(
            captured[1].split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=MIRROR',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        self.assertEqual(
            captured[2].split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder1',
            ])

    def test_stock_dual_carriage_no_copy_mirror_commands(self):
        printer, box = self._boot(self.cfg_dual)
        gcode = box['gcode']
        self.assertNotIn('COPY', gcode.ready_gcode_handlers)
        self.assertNotIn('MIRROR', gcode.ready_gcode_handlers)
        self.assertNotIn('COPY_OFF', gcode.ready_gcode_handlers)
        self.assertIsNone(box['cm'])
        self.assertIsNone(
            printer.lookup_object('copy_mirror', None))
        mgr = box['mgr']
        self.assertTrue(mgr.primary.is_implicit)
        self.assertFalse(mgr.ownership.multi_queue)


if __name__ == '__main__':
    os.chdir(ROOT)
    unittest.main(verbosity=2)
