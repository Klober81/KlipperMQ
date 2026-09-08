# TOOLCHANGE uses QUEUE_WAIT / QUEUE_SYNC for queue rendezvous.
# Overlap path: wait outgoing idle, park, wait incoming idle,
# activate incoming, QUEUE_SYNC barrier. Stock no-[queue]
# sequential TOOLCHANGE is unchanged (no wait/sync emit).
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                    '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import configfile
import extras.mq_config as mq_config
import extras.mq_manager as mq_manager
import extras.toolchange as toolchange
from extras.mq_lookahead import MultiLookAhead
import toolhead


# Marathon evidence (PARK_extruder / PARK_extruder1): X0 / X433.
# Do not invent parks.
TWO_QUEUE_TC_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_queues: 2

[queue q_T0]
owned_axes: x
extruder: extruder

[queue q_T1]
owned_axes: dual_carriage
extruder: extruder1

[toolchange T0]
park_x: 0

[toolchange T1]
park_x: 433
"""

STOCK_TC_CFG = (
    "[toolchange T0]\npark_x: 0\n"
    "[toolchange T1]\npark_x: 433\n"
)


class DummyPrinter:
    config_error = configfile.error
    def __init__(self):
        self.objects = collections.OrderedDict()
        self._handlers = {}
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
    def register_event_handler(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)
    def send_event(self, event, *params):
        for cb in self._handlers.get(event, []):
            cb(*params)


class DummyGCode:
    def __init__(self):
        self.commands = {}
        self.scripts = []
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)
    def run_script_from_command(self, script):
        self.scripts.append(script)


class DispatchGCode:
    # Record the script and dispatch registered commands so
    # QUEUE_WAIT / QUEUE_SYNC / SET_MOTION_QUEUE actually run.
    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.dispatched = []
        self.before_cmd = None
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)
    def run_script_from_command(self, script):
        self.scripts.append(script)
        for raw in script.split('\n'):
            line = raw.strip()
            if not line or line.startswith(';'):
                continue
            parts = line.split()
            cmd = parts[0]
            params = {}
            for p in parts[1:]:
                if '=' not in p:
                    continue
                key, val = p.split('=', 1)
                params[key] = val
            self.dispatched.append(cmd)
            if self.before_cmd is not None:
                self.before_cmd(cmd, params)
            handler = self.commands.get(cmd)
            if handler is None:
                continue
            func, _desc = handler
            func(DummyGCmd(**params))


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


class DummyDualCarriage:
    pass


class StubMCU:
    def __init__(self, est=0.):
        self._est = est
    def estimated_print_time(self, eventtime):
        return self._est


class StubReactor:
    def __init__(self, on_pause=None):
        self.pauses = 0
        self._on_pause = on_pause
        self._mono = 0.
    def monotonic(self):
        return self._mono
    def pause(self, waketime):
        self.pauses += 1
        self._mono = waketime
        if self._on_pause is not None:
            self._on_pause(self)
        return waketime


class StubToolhead:
    def __init__(self, reactor=None, est=0.):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False
        self.reactor = reactor if reactor is not None else StubReactor()
        self.mcu = StubMCU(est=est)
        self.print_time = 0.
        self.can_pause = True


class StubMove:
    def __init__(self, tag, min_move_t=0.01):
        self.tag = tag
        self.min_move_t = min_move_t
        self.timing_callbacks = []
        self.delta_v2 = 1.0
        self.max_start_v2 = 0.
        self.max_cruise_v2 = 1.0
        self.mcr_delta_v2 = 1.0
        self.max_mcr_start_v2 = 0.
        self.is_kinematic_move = False
    def calc_junction(self, prev):
        return
    def set_junction(self, start_v2, cruise_v2, end_v2):
        self.start_v = 0.
        self.cruise_v = 1.
        self.end_v = 0.
        self.accel_t = 0.
        self.cruise_t = self.min_move_t
        self.decel_t = 0.


def _toolchange_sections(fileconfig):
    sections = []
    for name in fileconfig.sections():
        sl = name.lower()
        if sl == 'toolchange' or sl.startswith('toolchange '):
            sections.append(name)
    return sections


def load_tc(text, with_toolhead=False, dispatch=False, reactor=None):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, 'test.cfg')
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, 'printer')
    if dispatch:
        printer.objects['gcode'] = DispatchGCode()
    else:
        printer.objects['gcode'] = DummyGCode()
    printer.objects['dual_carriage'] = DummyDualCarriage()
    mq = mq_config.load_config(config.getsection('mq_config'))
    printer.objects['mq_config'] = mq
    mgr = mq_manager.load_config(config.getsection('mq_manager'))
    printer.objects['mq_manager'] = mgr
    th = None
    if with_toolhead:
        th = StubToolhead(reactor=reactor)
        printer.objects['toolhead'] = th
    obj = None
    for name in _toolchange_sections(fileconfig):
        wrap = config.getsection(name)
        parts = name.split()
        if len(parts) > 1:
            obj = toolchange.load_config_prefix(wrap)
        else:
            obj = toolchange.load_config(wrap)
        printer.objects[name] = obj
    printer.send_event('klippy:connect')
    return printer, mq, mgr, obj, th


def read_src(*parts):
    path = os.path.join(ROOT, *parts)
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestToolchangeWaitSyncProof(unittest.TestCase):
    def toolchange(self, obj, tool):
        obj.cmd_TOOLCHANGE(DummyGCmd(TOOL=tool))

    def test_stock_no_queue_no_wait_sync_emit(self):
        # Stock / no-[queue]: sequential TOOLCHANGE stays valid;
        # QUEUE_WAIT / QUEUE_SYNC are not emitted.
        printer, mq, mgr, obj, th = load_tc(STOCK_TC_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        gcode = printer.lookup_object('gcode')
        self.assertNotIn('QUEUE_WAIT', gcode.commands)
        self.assertNotIn('QUEUE_SYNC', gcode.commands)
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('SET_MOTION_QUEUE', script)
        self.assertNotIn('QUEUE_WAIT', script)
        self.assertNotIn('QUEUE_SYNC', script)
        self.assertEqual(obj.current.name, 'T1')

    def test_first_activate_no_rendezvous(self):
        # First TOOLCHANGE has no outgoing; no wait/sync.
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        script = gcode.scripts[-1] if gcode.scripts else ''
        self.assertNotIn('QUEUE_WAIT', script)
        self.assertNotIn('QUEUE_SYNC', script)
        self.assertNotIn('G1 X0', script)
        self.assertEqual(obj.current.name, 'T0')

    def test_overlap_emits_wait_park_wait_sync(self):
        # T0->T1: wait outgoing, park X0, wait incoming, SYNC.
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        self.assertIn('G1 X0', script)
        self.assertNotIn('X434', script)
        self.assertIn('QUEUE_WAIT QUEUE=q_T0', script)
        self.assertIn('QUEUE_WAIT QUEUE=q_T1', script)
        self.assertIn('QUEUE_SYNC', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T0', script)
        self.assertIn('SET_MOTION_QUEUE QUEUE=q_T1', script)
        # Wait outgoing before selecting it / parking.
        self.assertLess(
            script.find('QUEUE_WAIT QUEUE=q_T0'),
            script.find('SET_MOTION_QUEUE QUEUE=q_T0'))
        self.assertLess(
            script.find('QUEUE_WAIT QUEUE=q_T0'),
            script.find('G1 X0'))
        # Park still before incoming queue switch (overlap).
        self.assertLess(
            script.find('SET_MOTION_QUEUE QUEUE=q_T0'),
            script.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        self.assertLess(
            script.find('G1 X0'),
            script.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        # Wait incoming after park, before incoming selector.
        self.assertLess(
            script.find('G1 X0'),
            script.find('QUEUE_WAIT QUEUE=q_T1'))
        self.assertLess(
            script.find('QUEUE_WAIT QUEUE=q_T1'),
            script.find('SET_MOTION_QUEUE QUEUE=q_T1'))
        # SYNC after both selectors (rendezvous).
        self.assertLess(
            script.find('SET_MOTION_QUEUE QUEUE=q_T1'),
            script.find('QUEUE_SYNC'))
        self.assertLess(
            script.find('ACTIVATE_EXTRUDER'),
            script.find('QUEUE_SYNC'))

    def test_overlap_t1_to_t0_wait_sync_park_x433(self):
        printer, mq, mgr, obj, th = load_tc(TWO_QUEUE_TC_CFG)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T1')
        self.toolchange(obj, 'T0')
        script = gcode.scripts[-1]
        self.assertIn('G1 X433', script)
        self.assertNotIn('X434', script)
        self.assertIn('QUEUE_WAIT QUEUE=q_T1', script)
        self.assertIn('QUEUE_WAIT QUEUE=q_T0', script)
        self.assertIn('QUEUE_SYNC', script)
        self.assertLess(
            script.find('QUEUE_WAIT QUEUE=q_T1'),
            script.find('G1 X433'))
        self.assertLess(
            script.find('G1 X433'),
            script.find('QUEUE_WAIT QUEUE=q_T0'))
        self.assertLess(
            script.find('QUEUE_WAIT QUEUE=q_T0'),
            script.find('QUEUE_SYNC'))

    def test_hop_down_before_sync(self):
        # Relative hop is motion; SYNC is after hop_down.
        text = TWO_QUEUE_TC_CFG.replace(
            "park_x: 0\n", "park_x: 0\npark_z_hop: 2\n", 1)
        printer, mq, mgr, obj, th = load_tc(text)
        gcode = printer.lookup_object('gcode')
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        script = gcode.scripts[-1]
        self.assertIn('G1 Z2', script)
        self.assertIn('G1 Z-2', script)
        self.assertLess(
            script.find('G1 Z-2'), script.find('QUEUE_SYNC'))
        self.assertLess(
            script.find('G1 X0'), script.find('G1 Z-2'))

    def test_toolchange_wait_blocks_while_outgoing_busy(self):
        def on_pause(reactor):
            if reactor.pauses >= 2:
                mgr.lookaheads[q0.name].reset()

        reactor = StubReactor(on_pause=on_pause)
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True,
            dispatch=True, reactor=reactor)
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        gcode = printer.lookup_object('gcode')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.toolchange(obj, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T0'))
        th.lookahead.add_move(StubMove('q_T0'))
        self.assertTrue(mgr.queue_is_busy(q0, th))
        self.assertFalse(mgr.queue_is_busy(q1, th))
        self.toolchange(obj, 'T1')
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q0, th))
        self.assertIn('QUEUE_WAIT', gcode.dispatched)
        self.assertIn('QUEUE_SYNC', gcode.dispatched)
        self.assertEqual(obj.current.name, 'T1')

    def test_toolchange_wait_blocks_while_incoming_busy(self):
        def on_pause(reactor):
            if reactor.pauses >= 2:
                mgr.lookaheads[q1.name].reset()

        reactor = StubReactor(on_pause=on_pause)
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True,
            dispatch=True, reactor=reactor)
        gcode = printer.lookup_object('gcode')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')
        self.toolchange(obj, 'T0')
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE='q_T1'))
        th.lookahead.add_move(StubMove('q_T1'))
        self.assertTrue(mgr.queue_is_busy(q1, th))
        self.assertFalse(mgr.queue_is_busy(q0, th))
        self.toolchange(obj, 'T1')
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q1, th))
        waits = [c for c in gcode.dispatched if c == 'QUEUE_WAIT']
        self.assertGreaterEqual(len(waits), 2)
        self.assertIn('QUEUE_SYNC', gcode.dispatched)

    def test_toolchange_sync_barrier_waits_both(self):
        # QUEUE_SYNC at the end must wait remaining busy queues
        # (rendezvous after park + incoming select).
        def on_pause(reactor):
            if reactor.pauses == 1:
                mgr.lookaheads[q0.name].reset()
            if reactor.pauses >= 2:
                mgr.lookaheads[q1.name].reset()

        reactor = StubReactor(on_pause=on_pause)
        printer, mq, mgr, obj, th = load_tc(
            TWO_QUEUE_TC_CFG, with_toolhead=True,
            dispatch=True, reactor=reactor)
        gcode = printer.lookup_object('gcode')
        q0 = mgr.lookup_queue('q_T0')
        q1 = mgr.lookup_queue('q_T1')

        def before_cmd(cmd, params):
            if cmd != 'QUEUE_SYNC':
                return
            mgr.lookaheads[q0.name].add_move(StubMove('q_T0'))
            mgr.lookaheads[q1.name].add_move(StubMove('q_T1'))

        gcode.before_cmd = before_cmd
        self.toolchange(obj, 'T0')
        self.toolchange(obj, 'T1')
        self.assertGreaterEqual(reactor.pauses, 2)
        self.assertFalse(mgr.queue_is_busy(q0, th))
        self.assertFalse(mgr.queue_is_busy(q1, th))
        self.assertIn('QUEUE_SYNC', gcode.dispatched)

    def test_no_new_public_commands_and_no_toolhead_edits(self):
        src = read_src('klippy', 'extras', 'toolchange.py')
        self.assertIn('QUEUE_WAIT', src)
        self.assertIn('QUEUE_SYNC', src)
        self.assertNotIn('QUERY_BOOKMARK', src)
        self.assertNotIn('RESUME_', src)
        self.assertNotIn('ACTIVATE_MIRROR', src)
        self.assertNotIn('can_drive', src)
        th_src = read_src('klippy', 'toolhead.py')
        self.assertNotIn('QUEUE_WAIT', th_src)
        self.assertNotIn('QUEUE_SYNC', th_src)
        self.assertNotIn('TOOLCHANGE', th_src)
        self.assertNotIn('mq_manager', th_src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
