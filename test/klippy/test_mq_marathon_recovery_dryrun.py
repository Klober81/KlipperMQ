# Marathon MQ recovery dry-run (bookmark -> recovery path).
# Cartesian recovery scaffold on tip; Marathon two-queue shape.
# CUT: prove persist + reload + script; do NOT invent z_hop
# product defaults or close ARCHITECTURE section 12 hop numbers.
# Overlay mq.cfg has LIVE [recovery] (Klober overlay-only).
# Harness z_hop_on_recover matches tip test_recovery.py (5).
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
import extras.mq_manager as mq_manager
import extras.motion_queuing as motion_queuing
import extras.recovery as recovery
import extras.toolchange as toolchange

# Config-owned Marathon MQ overlay (read-only fact check).
OVERLAY_MQ_CFG = os.path.normpath(os.path.join(
    ROOT, '..', '..', 'configs', 'mq', 'marathon', 'mq.cfg'))

# Formbot parks only (same evidence as printpath smoke).
# Harness hop: tip test_recovery.py uses 5 -- NOT product lock.
HARNESS_Z_HOP = 5.0

MARATHON_MQ_RECOVERY_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2

[queue q_T0]
owned_axes: x
extruder: extrude

[queue q_T1]
owned_axes: dual_carriage
extruder: extruder1

[toolchange T0]
park_x: 0

[toolchange T1]
park_x: 433

[recovery]
z_hop_on_recover: %.6g
filename: %%s
require_user_confirmation: False
restore_chamber: False
""" % (HARNESS_Z_HOP,)


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


class DummyPrintStats:
    def __init__(self, filename=''):
        self.filename = filename


class DummyDualCarriage:
    pass


class HostMQ:
    """motion_queuing host stub; wires note_accepted_move."""
    def __init__(self, printer):
        self.printer = printer
        self.bookmarks = motion_queuing.BookmarkSeq()
        self.drip_start_times = []
    check_drip_timing = (
        motion_queuing.PrinterMotionQueuing.check_drip_timing)
    note_accepted_move = (
        motion_queuing.PrinterMotionQueuing.note_accepted_move)


class THNoMCU:
    def register_lookahead_callback(self, cb):
        raise AssertionError('no MCU bookmark cmds expected')


def _toolchange_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl == 'toolchange' or sl.startswith('toolchange '):
            sections.append(name)
    return sections


def load_marathon_recovery(state_path, filename='job.gcode'):
    text = MARATHON_MQ_RECOVERY_CFG % (
        state_path.replace('\\', '/'),)
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    printer.objects['dual_carriage'] = DummyDualCarriage()
    printer.objects['print_stats'] = DummyPrintStats(filename)
    host = HostMQ(printer)
    printer.objects['motion_queuing'] = host
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    for name in _toolchange_sections(fileconfig):
        wrap = config.getsection(name)
        parts = name.split()
        if len(parts) > 1:
            obj = toolchange.load_config_prefix(wrap)
        else:
            obj = toolchange.load_config(wrap)
        printer.objects[name] = obj
    wrap = config.getsection('recovery')
    mq.recovery.filename = state_path
    robj = recovery.load_config(wrap)
    printer.objects['recovery'] = robj
    printer.send_event('klippy:connect')
    return printer, mq, mgr, robj, host, access


class TestMqMarathonRecoveryDryrun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix='mq_mrec_', suffix='.state', delete=False)
        self._tmp.close()
        self.state_path = self._tmp.name
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def tearDown(self):
        if os.path.exists(self.state_path):
            os.unlink(self.state_path)

    def test_overlay_mq_cfg_has_live_recovery(self):
        # Config-owned overlay: LIVE [recovery] hop/filename.
        # Klober overlay-only; does not close ARCHITECTURE sec 12.
        self.assertTrue(
            os.path.isfile(OVERLAY_MQ_CFG), OVERLAY_MQ_CFG)
        with open(OVERLAY_MQ_CFG, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('[recovery]', text.lower())
        self.assertIn('[queue]', text)
        self.assertIn('[toolchange T0]', text)
        self.assertIn('park_x: 0', text)
        self.assertIn('[toolchange T1]', text)
        self.assertIn('park_x: 433', text)
        printer = DummyPrinter()
        access = {}
        fileconfig = configfile.ConfigFileReader().build_fileconfig(
            text, OVERLAY_MQ_CFG)
        config = configfile.ConfigWrapper(
            printer, fileconfig, access, 'printer')
        mq = mq_config.load_config(config.getsection('mq_config'))
        self.assertTrue(mq.recovery.section_present)
        self.assertEqual(mq.recovery.z_hop_on_recover, 5.0)
        self.assertEqual(
            mq.recovery.filename,
            '~/printer_data/mq_recovery.state')

    def test_default_filename_convention(self):
        # mq_config default when [recovery] omits filename.
        text = (
            '[recovery]\n'
            'z_hop_on_recover: %.6g\n'
        ) % (HARNESS_Z_HOP,)
        printer = DummyPrinter()
        access = {}
        fileconfig = configfile.ConfigFileReader().build_fileconfig(
            text, 'test.cfg')
        config = configfile.ConfigWrapper(
            printer, fileconfig, access, 'printer')
        mq = mq_config.load_config(config.getsection('mq_config'))
        self.assertTrue(mq.recovery.section_present)
        self.assertEqual(
            mq.recovery.filename,
            '~/printer_data/mq_recovery.state')
        # Harness value only; product default still OPEN.
        self.assertEqual(
            mq.recovery.z_hop_on_recover, HARNESS_Z_HOP)

    def test_bookmark_persist_then_recovery_reload(self):
        # Path: note_accepted_move -> recovery.note_bookmark
        # -> state file -> reload -> clear_live keeps last.
        printer, mq, mgr, robj, host, access = (
            load_marathon_recovery(
                self.state_path, filename='marathon.gcode'))
        self.assertTrue(mgr.ownership.multi_queue)
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.assertIs(mgr.ownership.exclusive['x'], q0)
        self.assertIs(
            mgr.ownership.exclusive['dual_carriage'], q1)

        seq = host.note_accepted_move(THNoMCU())
        self.assertEqual(seq, 1)
        self.assertEqual(robj.live.seq_id, 1)
        self.assertEqual(robj.last.seq_id, 1)
        self.assertEqual(robj.live.filename, 'marathon.gcode')
        self.assertTrue(os.path.exists(self.state_path))

        again = recovery.RecoveryState.load(self.state_path)
        self.assertTrue(again.should_auto_resume())
        self.assertEqual(again.live.seq_id, 1)
        self.assertEqual(again.last.seq_id, 1)
        self.assertEqual(again.last.filename, 'marathon.gcode')

        robj.clear_live('abort')
        self.assertIsNone(robj.live)
        self.assertEqual(robj.last.seq_id, 1)
        boot = recovery.RecoveryState.load(self.state_path)
        self.assertIsNone(boot.live)
        self.assertEqual(boot.last.seq_id, 1)
        self.assertFalse(boot.should_auto_resume())

    def test_checkpoint_and_script_use_cfg_hop_only(self):
        # Script uses cfg.z_hop_on_recover; no invented hop.
        # Provisional checkpoint only (ARCHITECTURE sec 12 OPEN).
        printer, mq, mgr, robj, host, access = (
            load_marathon_recovery(self.state_path))
        gcode = printer.lookup_object('gcode')
        self.assertIn('_MQ_RECOVERY_CHECKPOINT', gcode.commands)
        self.assertNotIn('QUERY_BOOKMARK', gcode.commands)
        self.assertNotIn('RESUME_PRINT', gcode.commands)
        self.assertNotIn('RESUME_RECOVERY', gcode.commands)

        self.assertEqual(
            robj.cfg.z_hop_on_recover, HARNESS_Z_HOP)
        lines = robj.build_recovery_script()
        script = '\n'.join(lines)
        hop_line = 'G1 Z%.6g' % (HARNESS_Z_HOP,)
        self.assertIn('G91', script)
        self.assertIn(hop_line, script)
        self.assertIn('G90', script)
        self.assertIn('G28 X Y', script)
        self.assertIn('G28 Z', script)
        self.assertNotIn('TEMPERATURE_WAIT', script)
        # Sync path via checkpoint command.
        host.bookmarks.last_id = 9
        gcode.commands['_MQ_RECOVERY_CHECKPOINT'][0](None)
        self.assertEqual(robj.live.seq_id, 9)
        self.assertEqual(robj.last.seq_id, 9)
        loaded = recovery.RecoveryState.load(self.state_path)
        self.assertEqual(loaded.last.seq_id, 9)

    def test_finish_clears_live_keeps_last(self):
        printer, mq, mgr, robj, host, access = (
            load_marathon_recovery(self.state_path))
        robj.note_bookmark(4, filename='done.gcode')
        printer.send_event('print_stats:finish', 'complete')
        self.assertIsNone(robj.live)
        self.assertEqual(robj.last.seq_id, 4)
        boot = recovery.RecoveryState.load(self.state_path)
        self.assertIsNone(boot.live)
        self.assertFalse(boot.should_auto_resume())


if __name__ == '__main__':
    unittest.main(verbosity=2)
