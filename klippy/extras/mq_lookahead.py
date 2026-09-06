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

    def _in_drip(self):
        # Prefer toolhead._in_drip: covers add_move/flush/reset inside
        # _drip_load_trapq. motion_queuing.check_drip_timing() is only
        # set after drip_update_time (after that window) - too late.
        th = self.toolhead
        return th is not None and getattr(th, "_in_drip", False)

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
        if self._in_drip():
            # Drip/homing: primary-only even if active != primary.
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
        # Drip: always primary-only even if active != primary.
        if self._in_drip():
            qname = self.mgr.primary.name
            self._stamp_move(qname, move)
            return self._primary_la().add_move(move)
        qname = self._active_name()
        self._stamp_move(qname, move)
        return self._active_la().add_move(move)

    def flush(self, lazy=False):
        # Drip/homing: primary-only / single-LA; no multi-merge.
        if self._in_drip():
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
