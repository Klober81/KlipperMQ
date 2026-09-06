# Prove-first harness: explicit primary-only drip/homing gate.
# Multi_queue path consults mgr.is_drip_or_homing / drip_queue /
# homing_uses_primary_only -- named policy, not a buried _in_drip
# side effect. Stock = one ToolHead one drip path; IDEX multi-X
# home is sequential via carriage switch on that path.
#
# Philosophy must-pass:
# 1) drip/homing primary-only even when active != primary
# 2) non-drip concurrent still flush/merges (inactive drain)
# 3) no toolhead.py edits; no commanded_pos; no shareable/ACTIVATE_*
# 4) no secondary drip/homing claim; flush-all stays deferred
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import collections, os, sys, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "klippy"))

import configfile
import extras.mq_config as mq_config
import extras.mq_manager as mq_manager
from extras.mq_lookahead import MultiLookAhead
import toolhead


TWO_QUEUE_CFG = """
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
"""

STOCK_CFG = """
[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
"""


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
        prefix = module + " "
        objs = [(n, self.objects[n])
                for n in self.objects if n.startswith(prefix)]
        if module in self.objects:
            return [(module, self.objects[module])] + objs
        return objs
    def register_event_handler(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)
    def send_event(self, event):
        for cb in self._handlers.get(event, []):
            cb()


class DummyGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, func, desc=None):
        self.commands[name] = (func, desc)


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


class StubToolhead:
    def __init__(self):
        self.lookahead = toolhead.LookAheadQueue()
        self.lookahead.set_flush_time(1.0)
        self._in_drip = False
        self.print_time = 0.
        self._flush_calls = 0
    def _flush_lookahead(self):
        self._flush_calls += 1
        # Mirror stock: flush then reset facade/LA.
        self.lookahead.flush(lazy=False)
        self.lookahead.reset()


class StubMove:
    def __init__(self, tag, min_move_t=0.01, print_time=None):
        self.tag = tag
        self.min_move_t = min_move_t
        if print_time is not None:
            self.print_time = print_time
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


def load_mgr(text):
    printer = DummyPrinter()
    access = {}
    fileconfig = configfile.ConfigFileReader().build_fileconfig(
        text, "test.cfg")
    config = configfile.ConfigWrapper(
        printer, fileconfig, access, "printer")
    printer.objects["gcode"] = DummyGCode()
    mq = mq_config.load_config(config.getsection("mq_config"))
    printer.objects["mq_config"] = mq
    mgr = mq_manager.load_config(config.getsection("mq_manager"))
    printer.objects["mq_manager"] = mgr
    return printer, mq, mgr


def read_src(*parts):
    path = os.path.join(ROOT, *parts)
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestPrimaryDripGateProof(unittest.TestCase):
    def test_explicit_gate_api_names_present(self):
        # Gate is named on mgr + consulted by MultiLookAhead.
        mgr_src = read_src("klippy", "extras", "mq_manager.py")
        la_src = read_src("klippy", "extras", "mq_lookahead.py")
        for name in ("is_drip_or_homing", "drip_queue",
                     "homing_uses_primary_only"):
            self.assertIn(name, mgr_src, name)
        self.assertIn("is_drip_or_homing", la_src)
        self.assertIn("drip_queue", la_src)
        self.assertIn("homing_uses_primary_only", la_src)
        # WHY comment: stock one drip path
        self.assertIn("one drip path", mgr_src)
        self.assertIn("one drip path", la_src)

    def test_stock_no_queue_unchanged(self):
        printer, mq, mgr = load_mgr(STOCK_CFG)
        self.assertFalse(mgr.ownership.multi_queue)
        self.assertEqual(mgr.lookaheads, {})
        th = StubToolhead()
        printer.objects["toolhead"] = th
        stock_la = th.lookahead
        printer.send_event("klippy:connect")
        self.assertIs(th.lookahead, stock_la)
        self.assertNotIsInstance(th.lookahead, MultiLookAhead)
        # Gate helpers still exist; non-drip returns None.
        self.assertTrue(mgr.homing_uses_primary_only)
        self.assertFalse(mgr.is_drip_or_homing(th))
        self.assertIsNone(mgr.drip_queue(th))
        th._in_drip = True
        self.assertTrue(mgr.is_drip_or_homing(th))
        self.assertIs(mgr.drip_queue(th), mgr.primary)

    def test_drip_primary_only_when_active_not_primary(self):
        # FLAG 1: active == q_T1 still routes drip to primary LA.
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects["toolhead"] = th
        printer.send_event("klippy:connect")
        self.assertIsInstance(th.lookahead, MultiLookAhead)
        q0 = mgr.lookup_queue("q_T0")
        q1 = mgr.lookup_queue("q_T1")
        la0 = mgr.lookaheads[q0.name]
        la1 = mgr.lookaheads[q1.name]

        # Seed inactive child (non-drip), then switch active to q_T1
        th.lookahead.add_move(StubMove("seed_T0"))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE="q_T1"))
        self.assertIs(mgr.active_motion_queue, q1)
        th.lookahead.add_move(StubMove("seed_T1"))
        self.assertEqual(len(la0.queue), 1)
        self.assertEqual(len(la1.queue), 1)

        # Enter drip while active != primary
        th._in_drip = True
        self.assertTrue(mgr.is_drip_or_homing(th))
        self.assertTrue(mgr.homing_uses_primary_only)
        dq = mgr.drip_queue(th)
        self.assertIs(dq, q0)
        self.assertIs(dq, mgr.primary)
        self.assertIsNot(dq, q1)

        drip_move = StubMove("drip_home")
        th.lookahead.add_move(drip_move)
        # Primary accepts; secondary not the drip target
        self.assertEqual(len(la0.queue), 2)
        self.assertEqual(len(la1.queue), 1)
        self.assertEqual(la0.queue[-1].tag, "drip_home")
        self.assertEqual(drip_move.mq_print_time, 0.01)

        out = th.lookahead.flush(lazy=False)
        # Only primary drained; no multi-merge of inactive
        self.assertEqual(
            [m.tag for m in out], ["seed_T0", "drip_home"])
        self.assertTrue(la0.is_empty())
        self.assertEqual(len(la1.queue), 1)
        self.assertEqual(la1.queue[0].tag, "seed_T1")

        th.lookahead.reset()
        self.assertTrue(la0.is_empty())
        # Inactive child remains (NOT flush-all / NOT reset-all)
        self.assertEqual(len(la1.queue), 1)

    def test_non_drip_concurrent_still_flush_merges(self):
        # FLAG 2: normal path still drains inactive children.
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects["toolhead"] = th
        printer.send_event("klippy:connect")
        th.lookahead.add_move(StubMove("T0"))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE="q_T1"))
        th.lookahead.add_move(StubMove("T1"))
        self.assertFalse(mgr.is_drip_or_homing(th))
        self.assertIsNone(mgr.drip_queue(th))
        merged = th.lookahead.flush(lazy=False)
        self.assertEqual([m.tag for m in merged], ["T0", "T1"])
        self.assertTrue(mgr.lookaheads["q_T0"].is_empty())
        self.assertTrue(mgr.lookaheads["q_T1"].is_empty())

    def test_wait_flush_during_drip_does_not_note_inactive(self):
        # FLAG 4: flush-all deferred; drip wait notes primary only.
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects["toolhead"] = th
        printer.send_event("klippy:connect")
        th.lookahead.add_move(StubMove("keep_T0"))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE="q_T1"))
        th.lookahead.add_move(StubMove("keep_T1"))
        th.print_time = 12.5
        th._in_drip = True
        # Primary has pending; inactive also pending
        self.assertFalse(mgr.lookaheads["q_T0"].is_empty())
        self.assertFalse(mgr.lookaheads["q_T1"].is_empty())
        mgr._flush_pending_for_wait(th)
        # Primary drained via facade; inactive untouched
        self.assertTrue(mgr.lookaheads["q_T0"].is_empty())
        self.assertEqual(len(mgr.lookaheads["q_T1"].queue), 1)
        # Trapq end recorded only for primary
        self.assertEqual(mgr._trapq_end.get("q_T0"), 12.5)
        self.assertNotIn("q_T1", mgr._trapq_end)

    def test_no_toolhead_edits_or_forbidden_creep(self):
        # FLAG 3: zero toolhead.py for this cut; no creep tokens.
        th = read_src("klippy", "toolhead.py")
        self.assertNotIn("MultiLookAhead", th)
        self.assertNotIn("mq_lookahead", th)
        self.assertNotIn("mq_manager", th)
        self.assertNotIn("is_drip_or_homing", th)
        self.assertNotIn("drip_queue", th)
        self.assertNotIn("homing_uses_primary_only", th)
        mgr_src = read_src("klippy", "extras", "mq_manager.py")
        la_src = read_src("klippy", "extras", "mq_lookahead.py")
        for src, label in ((mgr_src, "mq_manager"),
                           (la_src, "mq_lookahead")):
            self.assertNotIn("commanded_pos", src, label)
            self.assertNotIn("ACTIVATE_", src, label)
            # Gate must not invent non-primary drip routing.
            self.assertNotIn("drip_queue = active", src, label)
            self.assertNotIn("secondary_drip", src, label)
        # Diff hygiene: this slice must not touch toolhead.py
        # (assert source still lacks MQ drip gate symbols above).

    def test_set_motion_queue_still_no_flush_all(self):
        printer, mq, mgr = load_mgr(TWO_QUEUE_CFG)
        th = StubToolhead()
        printer.objects["toolhead"] = th
        printer.send_event("klippy:connect")
        th.lookahead.add_move(StubMove("keep"))
        mgr.cmd_SET_MOTION_QUEUE(DummyGCmd(QUEUE="q_T1"))
        self.assertEqual(len(mgr.lookaheads["q_T0"].queue), 1)
        # Entering drip must not flush-all either
        th._in_drip = True
        self.assertEqual(len(mgr.lookaheads["q_T0"].queue), 1)
        self.assertEqual(len(mgr.lookaheads["q_T1"].queue), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
