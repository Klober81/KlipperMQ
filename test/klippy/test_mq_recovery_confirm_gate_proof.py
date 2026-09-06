# Prove-first: [recovery] require_user_confirmation gate.
# Ordered recovery steps 5-7 = user-jog safe XY for v1;
# confirmation required by default; opt-in skip via cfg False.
# Do NOT invent auto clear-zone, QUERY_*/RESUME_*, or close
# ARCHITECTURE section 12.
#
# Philosophy must-pass:
# 1) default True emits confirm M117 between G28 X Y and G28 Z
# 2) explicit False omits confirm prompt (opt-in skip)
# 3) no auto clear-zone / auto safe-XY move invent
# 4) QUERY_BOOKMARK / RESUME_* stay OPEN; no sec 12 close
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
import extras.recovery as recovery

CONFIRM_M117 = 'M117 Jog to safe XY then confirm recovery'
READY_M117 = 'M117 Recovery ready: resume/pause/cancel'


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


def load_recovery(text, state_path=None):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    printer.objects['gcode'] = DummyGCode()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    wrap = config.getsection('recovery')
    if state_path is not None:
        mq.recovery.filename = state_path
    obj = recovery.load_config(wrap)
    printer.objects['recovery'] = obj
    printer.send_event('klippy:connect')
    return printer, obj


def read_src(*parts):
    path = os.path.join(ROOT, *parts)
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestRecoveryConfirmGateProof(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix='mq_rec_confirm_', suffix='.state',
            delete=False)
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

    def test_cfg_default_true_and_status(self):
        # Default require_user_confirmation is True (opt-in skip).
        printer, obj = load_recovery(self._cfg())
        cfg = printer.lookup_object('mq_config').recovery
        self.assertTrue(cfg.require_user_confirmation)
        self.assertTrue(obj.cfg.require_user_confirmation)
        st = obj.get_status()
        self.assertTrue(st['require_user_confirmation'])

    def test_default_emits_confirm_between_xy_and_z(self):
        # FLAG 1: ordered steps 5-7 gated prompt between XY/Z home.
        printer, obj = load_recovery(self._cfg())
        lines = obj.build_recovery_script()
        script = '\n'.join(lines)
        self.assertIn('G28 X Y', script)
        self.assertIn(CONFIRM_M117, script)
        self.assertIn('G28 Z', script)
        self.assertIn(READY_M117, script)
        i_xy = lines.index('G28 X Y')
        i_confirm = lines.index(CONFIRM_M117)
        i_z = lines.index('G28 Z')
        i_ready = lines.index(READY_M117)
        self.assertTrue(
            i_xy < i_confirm < i_z < i_ready, lines)
        # User-jog v1: no auto XY move between XY home and Z.
        between = lines[i_xy + 1:i_z]
        for line in between:
            self.assertFalse(
                line.startswith('G0 ') or line.startswith('G1 '),
                line)
        self.assertEqual(between, [CONFIRM_M117])

    def test_opt_in_false_omits_confirm_prompt(self):
        # FLAG 2: explicit False skips confirmation M117.
        text = self._cfg(
            "require_user_confirmation: False\n")
        printer, obj = load_recovery(text)
        self.assertFalse(obj.cfg.require_user_confirmation)
        st = obj.get_status()
        self.assertFalse(st['require_user_confirmation'])
        lines = obj.build_recovery_script()
        script = '\n'.join(lines)
        self.assertNotIn(CONFIRM_M117, script)
        self.assertNotIn('Jog to safe XY', script)
        self.assertIn('G28 X Y', script)
        self.assertIn('G28 Z', script)
        self.assertIn(READY_M117, script)
        i_xy = lines.index('G28 X Y')
        i_z = lines.index('G28 Z')
        self.assertEqual(i_z, i_xy + 1)
        # Still no invented auto clear-zone XY motion.
        self.assertEqual(lines[i_xy + 1:i_z], [])

    def test_no_auto_clear_zone_invent_in_source(self):
        # FLAG 3: v1 safe XY = user jog; no clear-zone invent.
        src = read_src('klippy', 'extras', 'recovery.py')
        self.assertIn('require_user_confirmation', src)
        self.assertIn('user jog', src.lower())
        for token in ('clear_zone', 'clear-zone', 'clearzone',
                      'auto_safe_xy', 'candidate_safe_xy',
                      'safe_xy_x', 'safe_xy_y'):
            self.assertNotIn(token, src.lower(), token)
        # Gate branch must exist (not dead config only).
        self.assertIn('if cfg.require_user_confirmation:', src)
        self.assertIn(CONFIRM_M117, src)

    def test_no_query_resume_or_sec12_close(self):
        # FLAG 4: provisional checkpoint only; sec 12 stays OPEN.
        src = read_src('klippy', 'extras', 'recovery.py')
        self.assertIn('_MQ_RECOVERY_CHECKPOINT', src)
        # Mentioned only as OPEN / not-public in comments.
        self.assertIn('not a public', src.lower())
        self.assertIn('QUERY_BOOKMARK / RESUME_*', src)
        self.assertIn('Do not close ARCHITECTURE section 12.', src)
        self.assertNotIn('register_command(\n'
                         "                'QUERY_BOOKMARK'", src)
        self.assertNotIn("register_command('QUERY_BOOKMARK'", src)
        self.assertNotIn("register_command('RESUME_", src)
        gcode = load_recovery(self._cfg())[0].lookup_object(
            'gcode')
        self.assertIn('_MQ_RECOVERY_CHECKPOINT', gcode.commands)
        self.assertNotIn('QUERY_BOOKMARK', gcode.commands)
        for name in gcode.commands:
            self.assertFalse(
                name.startswith('RESUME_'), name)
            self.assertFalse(
                name.startswith('QUERY_'), name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
