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
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is not None:
            gcode.register_command("QUEUE_CLAIM", self.cmd_QUEUE_CLAIM,
                                   desc=self.cmd_QUEUE_CLAIM_help)
            gcode.register_command("QUEUE_RELEASE", self.cmd_QUEUE_RELEASE,
                                   desc=self.cmd_QUEUE_RELEASE_help)

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


def load_config(config):
    return MQManager(config)
