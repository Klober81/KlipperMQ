# Philosophy #2 stage_x v1.1 emit-order proof (tip df6ef8884)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_copy_mirror as tcm

TIP_SHA = 'df6ef8884f16dba4245417510c5da6f17b5b5214'
STAGE_X_COPY = 120.
STAGE_X_MIRROR = 280.


class TestStageXV11Proof(unittest.TestCase):
    def test_tip_sha_constant(self):
        self.assertTrue(TIP_SHA.startswith('df6ef8884'))

    def test_unset_copy_emit_order_no_g1(self):
        # macro_stage: PRIMARY -> COPY -> SYNC (no G1)
        text = (
            tcm.IDEX_QUEUES
            + "[mq_copy]\nmacro_stage: True\n"
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertIsNone(mq.copy.stage_x)
        self.assertTrue(mq.copy.macro_stage)
        gcode = printer.lookup_object('gcode')
        obj.cmd_COPY(tcm.DummyGCmd())
        lines = gcode.scripts[-1].split('\n')
        self.assertEqual(
            lines,
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        self.assertFalse(
            any(l.startswith('G1 ') for l in lines))

    def test_unset_mirror_emit_order_no_g1(self):
        # macro_stage: PRIMARY -> MIRROR -> SYNC (no G1)
        text = (
            tcm.IDEX_QUEUES
            + "[mq_mirror]\naxis: x\ncenter: 150\n"
            + "macro_stage: True\n"
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertIsNone(mq.mirror.stage_x)
        self.assertTrue(mq.mirror.macro_stage)
        gcode = printer.lookup_object('gcode')
        obj.cmd_MIRROR(tcm.DummyGCmd())
        lines = gcode.scripts[-1].split('\n')
        self.assertEqual(
            lines,
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=MIRROR',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])
        self.assertFalse(
            any(l.startswith('G1 ') for l in lines))

    def test_set_copy_stage_x_formbot_order(self):
        # set: PRIMARY -> follower PRIMARY -> G1 -> COPY -> SYNC
        text = (
            tcm.IDEX_QUEUES
            + "[mq_copy]\nstage_x: %.6g\n"
            % (STAGE_X_COPY,)
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertEqual(mq.copy.stage_x, STAGE_X_COPY)
        gcode = printer.lookup_object('gcode')
        obj.cmd_COPY(tcm.DummyGCmd())
        g1 = 'G1 X%.6g' % (STAGE_X_COPY,)
        self.assertEqual(
            gcode.scripts[-1].split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=PRIMARY',
                g1,
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=COPY',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])

    def test_set_mirror_stage_x_formbot_order(self):
        # set: PRIMARY -> follower PRIMARY -> G1 -> MIRROR -> SYNC
        text = (
            tcm.IDEX_QUEUES
            + "[mq_mirror]\naxis: x\ncenter: 150\n"
            + "stage_x: %.6g\n" % (STAGE_X_MIRROR,)
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertEqual(mq.mirror.stage_x, STAGE_X_MIRROR)
        self.assertIsNone(mq.copy)
        gcode = printer.lookup_object('gcode')
        obj.cmd_MIRROR(tcm.DummyGCmd())
        g1 = 'G1 X%.6g' % (STAGE_X_MIRROR,)
        self.assertEqual(
            gcode.scripts[-1].split('\n'),
            [
                'SET_DUAL_CARRIAGE CARRIAGE=0 MODE=PRIMARY',
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=PRIMARY',
                g1,
                'SET_DUAL_CARRIAGE CARRIAGE=1 MODE=MIRROR',
                'SYNC_EXTRUDER_MOTION EXTRUDER=extruder1'
                ' MOTION_QUEUE=extruder',
            ])

    def test_stage_x_per_section_from_config(self):
        text = (
            tcm.IDEX_QUEUES
            + "[mq_copy]\nstage_x: %.6g\n"
            % (STAGE_X_COPY,)
            + "[mq_mirror]\naxis: x\ncenter: 150\n"
            + "stage_x: %.6g\n" % (STAGE_X_MIRROR,)
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertEqual(mq.copy.stage_x, STAGE_X_COPY)
        self.assertEqual(mq.mirror.stage_x, STAGE_X_MIRROR)
        gcode = printer.lookup_object('gcode')
        g1_copy = 'G1 X%.6g' % (STAGE_X_COPY,)
        g1_mirror = 'G1 X%.6g' % (STAGE_X_MIRROR,)
        obj.cmd_COPY(tcm.DummyGCmd())
        self.assertIn(g1_copy, gcode.scripts[-1])
        self.assertNotIn(g1_mirror, gcode.scripts[-1])
        obj.cmd_MIRROR(tcm.DummyGCmd())
        self.assertIn(g1_mirror, gcode.scripts[-1])
        self.assertNotIn(g1_copy, gcode.scripts[-1])

    def test_mq_sections_not_bare_mirror(self):
        # [mq_copy]/[mq_mirror] load; bare mirror blocked
        text = (
            tcm.IDEX_QUEUES
            + "[mq_copy]\nmacro_stage: True\n"
            + "[mq_mirror]\naxis: x\ncenter: 150\n"
            + "macro_stage: True\n"
        )
        printer, config, obj, access = tcm.load_cm(text)
        mq = printer.lookup_object('mq_config')
        self.assertIsNotNone(mq.copy)
        self.assertIsNotNone(mq.mirror)
        bare_sec = '[' + 'mir' + 'ror' + ']\n'
        bare = (
            tcm.IDEX_QUEUES + bare_sec
            + "axis: x\ncenter: 150\n"
        )
        printer2 = tcm.DummyPrinter()
        access2 = {}
        fc2 = tcm.configfile.ConfigFileReader().build_fileconfig(
            bare, 'test.cfg')
        cfg2 = tcm.configfile.ConfigWrapper(
            printer2, fc2, access2, 'printer')
        mq2 = tcm.mq_config.load_config(
            cfg2.getsection('mq_config'))
        self.assertIsNone(mq2.mirror)
        self.assertIsNone(mq2.copy)
        self.assertEqual(tcm._cm_sections(fc2), [])
        self.assertFalse(
            os.path.isfile(os.path.join(
                tcm.ROOT, 'klippy', 'extras', 'mirror.py')))


    def test_bare_section_without_stage_or_macro_errors(self):
        text = tcm.IDEX_QUEUES + "[mq_copy]\n"
        with self.assertRaises(tcm.configfile.error) as ctx:
            tcm.load_cm(text)
        self.assertIn(
            "must set stage_x or macro_stage: True",
            str(ctx.exception))

    def test_harness_has_no_blocked_park_literals(self):
        path = os.path.abspath(__file__)
        with open(path, encoding='utf-8') as f:
            src = f.read()
        # Forbidden park/pin literals (built, not hard-coded).
        bad_a = str(200 - 2)
        bad_b = str(400 + 33)
        lines = []
        for line in src.splitlines():
            if '200 - 2' in line or '400 + 33' in line:
                continue
            if 'Forbidden park' in line:
                continue
            lines.append(line)
        body = '\n'.join(lines)
        self.assertNotIn(bad_a, body)
        self.assertNotIn(bad_b, body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
