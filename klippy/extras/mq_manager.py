# Multi-queue runtime ownership (queues + exclusive/shareable axes)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class Queue:
    def __init__(self, qcfg):
        self.name = qcfg.name
        self.aliases = tuple(qcfg.aliases)
        self.is_primary = qcfg.is_primary
        self.is_implicit = qcfg.is_implicit
        self.exclusive_axes = tuple(qcfg.owned_axes)
        self.extruder = qcfg.extruder


# IDEX: owned_axes token -> SET_DUAL_CARRIAGE CARRIAGE index
CARRIAGE_AXES = ('x', 'dual_carriage')


class OwnershipMap:
    def __init__(self, queues):
        self.exclusive = {}
        self.shareable = {}
        self.multi_queue = any(not q.is_primary for q in queues)
        if not self.multi_queue:
            return
        for q in queues:
            for axis in q.exclusive_axes:
                self.exclusive[axis] = q

    def can_drive(self, queue, axis):
        axis = axis.lower()
        if not self.multi_queue:
            return bool(queue.is_primary)
        owner = self.exclusive.get(axis)
        if owner is not None:
            return owner is queue
        owner = self.shareable.get(axis)
        if owner is not None:
            return owner is queue
        return False

    def carriage_for_queue(self, queue):
        if not self.multi_queue:
            return None
        for idx, axis in enumerate(CARRIAGE_AXES):
            if self.exclusive.get(axis) is queue:
                return idx
        return None

    def queue_for_carriage(self, carriage):
        if not self.multi_queue:
            return None
        try:
            idx = int(carriage)
        except (TypeError, ValueError):
            return None
        if idx < 0 or idx >= len(CARRIAGE_AXES):
            return None
        return self.exclusive.get(CARRIAGE_AXES[idx])

    def claim(self, queue, axis):
        axis = axis.lower()
        owner = self.exclusive.get(axis)
        if owner is not None:
            raise ValueError(
                "Axis '%s' is exclusive to queue '%s' and cannot be claimed"
                % (axis, owner.name))
        owner = self.shareable.get(axis)
        if owner is not None and owner is not queue:
            raise ValueError(
                "Axis '%s' is already owned by queue '%s'"
                % (axis, owner.name))
        self.shareable[axis] = queue

    def release(self, queue, axis):
        axis = axis.lower()
        owner = self.exclusive.get(axis)
        if owner is not None:
            raise ValueError(
                "Cannot release exclusive axis '%s' owned by queue '%s'"
                % (axis, owner.name))
        owner = self.shareable.get(axis)
        if owner is not queue:
            raise ValueError(
                "Queue '%s' does not own shareable axis '%s'"
                % (queue.name, axis))
        self.shareable[axis] = None


class MQManager:
    def __init__(self, config):
        self.printer = config.get_printer()
        mq = self.printer.lookup_object('mq_config')
        self.queues = []
        self._by_name = {}
        self.primary = None
        for qcfg in mq.queues:
            q = Queue(qcfg)
            self.queues.append(q)
            if q.is_primary:
                self.primary = q
            for alias in q.aliases:
                self._by_name[alias.lower()] = q
        self.ownership = OwnershipMap(self.queues)
        self.pause_all_queues_on_error = self._parse_pause_all(config)
        # Per-queue lookaheads when multi_queue;
        # stock path keeps toolhead LA only.
        self.lookaheads = {}
        self.active_motion_queue = self.primary
        # Drip/homing policy: stock = one ToolHead one drip path;
        # IDEX multi-X home is sequential via carriage switch on
        # that path. MQ primary-only keeps that shape (explicit
        # gate; not an accidental _in_drip side effect).
        self.homing_uses_primary_only = True
        # Last toolhead.print_time observed after a flush that
        # drained this queue's LA (trapq busy until est catches up).
        self._trapq_end = {}
        if self.ownership.multi_queue:
            self._init_lookaheads()
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is not None:
            gcode.register_command("QUEUE_CLAIM", self.cmd_QUEUE_CLAIM,
                                   desc=self.cmd_QUEUE_CLAIM_help)
            gcode.register_command("QUEUE_RELEASE", self.cmd_QUEUE_RELEASE,
                                   desc=self.cmd_QUEUE_RELEASE_help)
            if self.ownership.multi_queue:
                gcode.register_command(
                    "SET_MOTION_QUEUE", self.cmd_SET_MOTION_QUEUE,
                    desc=self.cmd_SET_MOTION_QUEUE_help)
                gcode.register_command(
                    "QUEUE_WAIT", self.cmd_QUEUE_WAIT,
                    desc=self.cmd_QUEUE_WAIT_help)
                gcode.register_command(
                    "QUEUE_SYNC", self.cmd_QUEUE_SYNC,
                    desc=self.cmd_QUEUE_SYNC_help)
        # Install MultiLookAhead facade on connect when multi_queue.
        # ARCH sec 7: pause all queue LAs on gcode/toolhead command_error.
        if hasattr(self.printer, 'register_event_handler'):
            self.printer.register_event_handler("klippy:connect",
                                               self._handle_connect)
            self.printer.register_event_handler(
                "gcode:command_error", self._handle_command_error)

    def _parse_pause_all(self, config):
        fileconfig = config.fileconfig
        for section in fileconfig.sections():
            if not fileconfig.has_option(section, 'pause_all_queues_on_error'):
                continue
            if section.lower() != 'printer':
                raise config.error(
                    "Option 'pause_all_queues_on_error' is only valid"
                    " in section 'printer'")
        printer_cfg = config.getsection('printer')
        if not fileconfig.has_option('printer', 'pause_all_queues_on_error'):
            return True
        return printer_cfg.getboolean('pause_all_queues_on_error')

    def lookup_queue(self, name):
        q = self._by_name.get(name.lower())
        if q is None:
            raise self.printer.config_error(
                "Unknown queue '%s'. Valid names: %s"
                % (name, self._valid_queue_names()))
        return q

    def _valid_queue_names(self):
        names = []
        seen = set()
        for q in self.queues:
            for alias in q.aliases:
                key = alias.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(alias)
        return ', '.join(names)

    def can_drive(self, queue, axis):
        return self.ownership.can_drive(queue, axis)

    def carriage_for_queue(self, queue):
        return self.ownership.carriage_for_queue(queue)

    def queue_for_carriage(self, carriage):
        return self.ownership.queue_for_carriage(carriage)

    def get_status(self, eventtime=None):
        queues = {}
        for q in self.queues:
            queues[q.name] = {
                'aliases': list(q.aliases),
                'is_primary': q.is_primary,
                'is_implicit': q.is_implicit,
                'exclusive_axes': list(q.exclusive_axes),
                'extruder': q.extruder,
            }
        exclusive = dict((axis, q.name)
                         for axis, q in self.ownership.exclusive.items())
        shareable = dict((axis, None if q is None else q.name)
                         for axis, q in self.ownership.shareable.items())
        ownership = {}
        ownership.update(exclusive)
        ownership.update(shareable)
        return {
            'queues': queues,
            'primary': self.primary.name,
            'multi_queue': self.ownership.multi_queue,
            'pause_all_queues_on_error': self.pause_all_queues_on_error,
            'ownership': ownership,
            'exclusive': exclusive,
            'shareable': shareable,
        }

    def _queue_from_gcmd(self, gcmd):
        name = gcmd.get('QUEUE')
        q = self._by_name.get(name.lower())
        if q is None:
            raise gcmd.error(
                "Unknown queue '%s'. Valid names: %s"
                % (name, self._valid_queue_names()))
        return q

    cmd_QUEUE_CLAIM_help = "Claim a shareable axis for a queue"
    def cmd_QUEUE_CLAIM(self, gcmd):
        queue = self._queue_from_gcmd(gcmd)
        axis = gcmd.get('AXIS')
        try:
            self.ownership.claim(queue, axis)
        except ValueError as e:
            raise gcmd.error(str(e))

    cmd_QUEUE_RELEASE_help = "Release a shareable axis claimed by a queue"
    def cmd_QUEUE_RELEASE(self, gcmd):
        queue = self._queue_from_gcmd(gcmd)
        axis = gcmd.get('AXIS')
        try:
            self.ownership.release(queue, axis)
        except ValueError as e:
            raise gcmd.error(str(e))

    def _init_lookaheads(self):
        # Reuse stock LookAheadQueue; one instance per configured queue.
        import toolhead
        for q in self.queues:
            self.lookaheads[q.name] = toolhead.LookAheadQueue()

    def _handle_connect(self):
        if not self.ownership.multi_queue:
            return
        toolhead_obj = self.printer.lookup_object('toolhead', None)
        if toolhead_obj is None:
            return
        # Preserve flush timing from the stock-constructed LA
        # onto each queue LA.
        stock_la = toolhead_obj.lookahead
        flush_time = stock_la.junction_flush
        for q in self.queues:
            la = self.lookaheads[q.name]
            la.set_flush_time(flush_time)
        # One facade pointer; SET_MOTION_QUEUE only changes active.
        self.active_motion_queue = self.primary
        from extras.mq_lookahead import MultiLookAhead
        self.multi_lookahead = MultiLookAhead(self, toolhead_obj)
        toolhead_obj.lookahead = self.multi_lookahead

    def _handle_command_error(self):
        # Stock gcode.py sends gcode:command_error on CommandError
        # (covers toolhead move_error raised as command_error).
        self.pause_queues_for_error()

    def pause_queues_for_error(self):
        # ARCH sec 7: when enabled, idle every queue LA and enter
        # NeedPrime special_queuing. Selective resume later is OK.
        # No new error G-codes; do not touch recovery.
        if not self.pause_all_queues_on_error:
            return
        if not self.ownership.multi_queue:
            return
        for la in self.lookaheads.values():
            la.reset()
        mla = getattr(self, 'multi_lookahead', None)
        if mla is not None:
            mla._next_t.clear()
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return
        # Match stock _flush_lookahead NeedPrime transition without
        # emitting pending moves (discard planned motion on error).
        toolhead.special_queuing_state = "NeedPrime"
        toolhead.need_check_pause = -1.
        toolhead.check_stall_time = 0.
        la = getattr(toolhead, 'lookahead', None)
        if la is not None:
            # toolhead.BUFFER_TIME_HIGH == 1.0
            la.set_flush_time(1.0)

    def _select_motion_queue(self, queue):
        # Active name only; do not swap toolhead.lookahead.
        # SET_MOTION_QUEUE does not flush-all.
        self.active_motion_queue = queue

    cmd_SET_MOTION_QUEUE_help = (
        "Select active motion queue for subsequent moves")
    def cmd_SET_MOTION_QUEUE(self, gcmd):
        queue = self._queue_from_gcmd(gcmd)
        self._select_motion_queue(queue)

    def is_drip_or_homing(self, toolhead=None):
        # Explicit drip/homing gate for multi_queue path.
        # Stock drip_move sets toolhead._in_drip around _drip_load_trapq.
        # Prefer that flag: covers add_move/flush/reset inside the
        # drip window. motion_queuing.check_drip_timing() is only set
        # after drip_update_time -- too late for LA routing.
        if toolhead is None:
            toolhead = self.printer.lookup_object("toolhead", None)
        if toolhead is None:
            return False
        return bool(getattr(toolhead, "_in_drip", False))

    def drip_queue(self, toolhead=None):
        # Queue that owns drip/homing motion, or None if not dripping.
        # When dripping under multi_queue + homing_uses_primary_only,
        # always primary -- never non-primary. Does not flush-all.
        if not self.is_drip_or_homing(toolhead):
            return None
        if not self.homing_uses_primary_only:
            # Policy flag off: still primary (never non-primary drip).
            return self.primary
        return self.primary

    def lookahead_for(self, queue):
        if isinstance(queue, str):
            queue = self.lookup_queue(queue)
        if not self.ownership.multi_queue:
            return None
        return self.lookaheads.get(queue.name)

    def queue_is_busy(self, queue, toolhead=None, eventtime=None):
        # Idle = child LA empty and stock print_time past trapq end.
        if isinstance(queue, str):
            queue = self.lookup_queue(queue)
        la = self.lookaheads.get(queue.name)
        if la is not None and not la.is_empty():
            return True
        end = self._trapq_end.get(queue.name, 0.)
        if end <= 0.:
            return False
        if toolhead is None:
            toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return True
        mcu = getattr(toolhead, 'mcu', None)
        reactor = getattr(toolhead, 'reactor', None)
        if mcu is None or reactor is None:
            return True
        if eventtime is None:
            eventtime = reactor.monotonic()
        return end > mcu.estimated_print_time(eventtime)

    def _note_trapq_ends(self, toolhead, had_pending):
        # After stock flush, print_time is end of merged stream.
        pt = getattr(toolhead, 'print_time', None)
        if pt is None:
            return
        for qname, pending in had_pending.items():
            if not pending:
                continue
            prev = self._trapq_end.get(qname, 0.)
            if pt > prev:
                self._trapq_end[qname] = pt

    def _flush_pending_for_wait(self, toolhead):
        # Reuse stock toolhead flush; never invent a second planner.
        # Stubs without _flush_lookahead keep child LA state so tests
        # can prove wait blocks while LA busy.
        # Drip/homing: primary-only via explicit gate -- do not treat
        # inactive children as drained (no flush-all during drip).
        if not self.lookaheads:
            return
        if not hasattr(toolhead, '_flush_lookahead'):
            return
        if (self.is_drip_or_homing(toolhead)
                and self.homing_uses_primary_only):
            pname = self.primary.name
            la = self.lookaheads.get(pname)
            had = {pname: la is not None and not la.is_empty()}
        else:
            had = dict((n, not la.is_empty())
                       for n, la in self.lookaheads.items())
        if not any(had.values()):
            return
        toolhead._flush_lookahead()
        self._note_trapq_ends(toolhead, had)

    def _wait_queues_idle(self, queues):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return
        reactor = getattr(toolhead, 'reactor', None)
        can_pause = getattr(toolhead, 'can_pause', True)
        eventtime = None
        if reactor is not None:
            eventtime = reactor.monotonic()
        while True:
            self._flush_pending_for_wait(toolhead)
            busy = False
            for q in queues:
                if self.queue_is_busy(q, toolhead, eventtime):
                    busy = True
                    break
            if not busy:
                return
            if reactor is None or not can_pause:
                return
            eventtime = reactor.pause(eventtime + 0.100)

    cmd_QUEUE_WAIT_help = (
        "Block until the named motion queue is idle")
    def cmd_QUEUE_WAIT(self, gcmd):
        queue = self._queue_from_gcmd(gcmd)
        self._wait_queues_idle([queue])

    cmd_QUEUE_SYNC_help = (
        "Barrier: block until all motion queues are idle")
    def cmd_QUEUE_SYNC(self, gcmd):
        self._wait_queues_idle(list(self.queues))


def load_config(config):
    return MQManager(config)
