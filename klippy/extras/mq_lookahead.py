# MultiLookAhead facade: one toolhead pointer, many child LAs
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class MultiLookAhead:
    """LookAheadQueue-compatible facade over per-queue child LAs.

    Public surface matches stock LookAheadQueue methods toolhead uses:
    add_move, flush, set_flush_time, reset, is_empty, get_last.

    Merge key is move.mq_print_time (stamped on add_move). Never use
    Move.print_time - stock Move has no print_time until trapq schedule.

    Drip/homing gate: stock has one ToolHead and one drip path; IDEX
    multi-X home is sequential via carriage switch on that path. MQ
    keeps that shape -- drip/homing always uses the primary queue LA
    via mgr.is_drip_or_homing / drip_queue (explicit policy, not a
    buried toolhead._in_drip side effect alone).
    """

    def __init__(self, mgr, toolhead=None):
        self.mgr = mgr
        self.toolhead = toolhead
        # Per-queue synthetic time; advances by min_move_t on each add.
        self._next_t = {}

    def _active_name(self):
        q = self.mgr.active_motion_queue
        if q is None:
            q = self.mgr.primary
        return q.name

    def _active_la(self):
        return self.mgr.lookaheads[self._active_name()]

    def _primary_la(self):
        return self.mgr.lookaheads[self.mgr.primary.name]

    def _drip_or_homing(self):
        # Explicit mgr gate (homing_uses_primary_only / drip_queue).
        return self.mgr.is_drip_or_homing(self.toolhead)

    def _drip_queue_name(self):
        dq = self.mgr.drip_queue(self.toolhead)
        if dq is None:
            return None
        return dq.name

    def _stamp_move(self, qname, move):
        t = self._next_t.get(qname, 0.)
        move.mq_print_time = t
        dt = getattr(move, "min_move_t", None)
        if dt is None or dt <= 0.:
            dt = 0.001
        self._next_t[qname] = t + dt

    def set_flush_time(self, flush_time):
        for la in self.mgr.lookaheads.values():
            la.set_flush_time(flush_time)

    def reset(self):
        if self._drip_or_homing():
            # Drip/homing: primary-only even if active != primary.
            # Does not flush-all / reset inactive children.
            self._primary_la().reset()
            return
        for la in self.mgr.lookaheads.values():
            la.reset()

    def is_empty(self):
        return all(la.is_empty()
                   for la in self.mgr.lookaheads.values())

    def get_last(self):
        return self._active_la().get_last()

    def add_move(self, move):
        # Active child only; SET_MOTION_QUEUE changes active.
        # Drip/homing: always primary-only even if active != primary.
        qname = self._drip_queue_name()
        if qname is not None:
            self._stamp_move(qname, move)
            return self._primary_la().add_move(move)
        qname = self._active_name()
        self._stamp_move(qname, move)
        return self._active_la().add_move(move)

    def flush(self, lazy=False):
        # Drip/homing: primary-only / single-LA; no multi-merge,
        # no flush-all of inactive children (stock one drip path).
        if self._drip_or_homing():
            return self._primary_la().flush(lazy=lazy)
        # Drain every child LA; merge by ascending mq_print_time.
        # Stable tie-break: primary first, then queue name.
        primary = self.mgr.primary.name
        tagged = []
        for qname, la in self.mgr.lookaheads.items():
            moves = la.flush(lazy=lazy)
            pri_rank = 0 if qname == primary else 1
            for seq, move in enumerate(moves):
                pt = move.mq_print_time
                tagged.append((pt, pri_rank, qname, seq, move))
        tagged.sort()
        return [t[-1] for t in tagged]
