# COPY / MIRROR dual-carriage sync (native emit)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import extras.mq_config as mq_config


class CopyMirror:
    def __init__(self, config):
        self.printer = config.get_printer()
        self._consumed = False
        self.copy_cfg = None
        self.mirror_cfg = None
        self._active = None
        register = getattr(
            self.printer, 'register_event_handler', None)
        if register is not None:
            register('klippy:connect', self._handle_connect)

    def _handle_connect(self):
        self._consume()

    def _consume(self):
        if self._consumed:
            return
        self._consumed = True
        mq = self.printer.lookup_object('mq_config', None)
        if mq is None:
            return
        self.copy_cfg = mq.copy
        self.mirror_cfg = mq.mirror
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is None:
            return
        if self.copy_cfg is not None:
            gcode.register_command(
                "COPY", self.cmd_COPY, desc=self.cmd_COPY_help)
        if self.mirror_cfg is not None:
            gcode.register_command(
                "MIRROR", self.cmd_MIRROR, desc=self.cmd_MIRROR_help)
        if self.copy_cfg is not None or self.mirror_cfg is not None:
            gcode.register_command(
                "OFF", self.cmd_OFF, desc=self.cmd_OFF_help)

    def _resolve_source_queue(self, source_token, error):
        mgr = self.printer.lookup_object('mq_manager', None)
        if mgr is None:
            raise error("COPY/MIRROR requires mq_manager")
        if mq_config._is_primary_token(source_token):
            return mgr.primary
        try:
            return mgr.lookup_queue(source_token)
        except self.printer.config_error as e:
            raise error(str(e))

    def _resolve_pair(self, source_queue, error):
        dc = self.printer.lookup_object('dual_carriage', None)
        if dc is None:
            raise error("COPY/MIRROR requires [dual_carriage]")
        mgr = self.printer.lookup_object('mq_manager', None)
        if mgr is None:
            raise error("COPY/MIRROR requires mq_manager")
        if not mgr.ownership.multi_queue:
            raise error(
                "COPY/MIRROR requires multi-queue IDEX ownership"
                " (x and dual_carriage)")
        primary = mgr.carriage_for_queue(source_queue)
        if primary is None:
            raise error(
                "Queue '%s' owns neither x nor dual_carriage"
                % (source_queue.name,))
        follower = 1 - primary
        follower_queue = mgr.queue_for_carriage(follower)
        if follower_queue is None:
            raise error(
                "No queue owns follower carriage %d" % (follower,))
        source_ext = source_queue.extruder
        follower_ext = follower_queue.extruder
        if not source_ext:
            raise error(
                "Queue '%s' has no extruder" % (source_queue.name,))
        if not follower_ext:
            raise error(
                "Queue '%s' has no extruder" % (follower_queue.name,))
        return primary, follower, source_ext, follower_ext

    def _emit_on(self, mode, source_token, gcmd):
        self._consume()
        source_queue = self._resolve_source_queue(
            source_token, gcmd.error)
        primary, follower, source_ext, follower_ext = self._resolve_pair(
            source_queue, gcmd.error)
        lines = [
            'SET_DUAL_CARRIAGE CARRIAGE=%d MODE=PRIMARY' % (primary,),
            'SET_DUAL_CARRIAGE CARRIAGE=%d MODE=%s' % (follower, mode),
            'SYNC_EXTRUDER_MOTION EXTRUDER=%s MOTION_QUEUE=%s'
            % (follower_ext, source_ext),
        ]
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command('\n'.join(lines))
        self._active = mode

    def _emit_off(self, source_token, gcmd):
        self._consume()
        source_queue = self._resolve_source_queue(
            source_token, gcmd.error)
        primary, follower, source_ext, follower_ext = self._resolve_pair(
            source_queue, gcmd.error)
        lines = [
            'SET_DUAL_CARRIAGE CARRIAGE=%d MODE=PRIMARY' % (primary,),
            'SYNC_EXTRUDER_MOTION EXTRUDER=%s MOTION_QUEUE=%s'
            % (follower_ext, follower_ext),
        ]
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command('\n'.join(lines))
        self._active = None

    def _source_for_off(self):
        if self._active == 'COPY' and self.copy_cfg is not None:
            return self.copy_cfg.source
        if self._active == 'MIRROR' and self.mirror_cfg is not None:
            return self.mirror_cfg.source
        if self.copy_cfg is not None:
            return self.copy_cfg.source
        if self.mirror_cfg is not None:
            return self.mirror_cfg.source
        return None

    cmd_COPY_help = "Enable dual_carriage COPY mode"
    def cmd_COPY(self, gcmd):
        source = None
        if self.copy_cfg is not None:
            source = self.copy_cfg.source
        self._emit_on('COPY', source, gcmd)

    cmd_MIRROR_help = "Enable dual_carriage MIRROR mode"
    def cmd_MIRROR(self, gcmd):
        source = None
        if self.mirror_cfg is not None:
            source = self.mirror_cfg.source
        self._emit_on('MIRROR', source, gcmd)

    cmd_OFF_help = "Disable COPY/MIRROR and unsync follower extruder"
    def cmd_OFF(self, gcmd):
        self._emit_off(self._source_for_off(), gcmd)

    def get_status(self, eventtime=None):
        return {
            'active': self._active,
            'has_copy': self.copy_cfg is not None,
            'has_mirror': self.mirror_cfg is not None,
        }


def load_config(config):
    printer = config.get_printer()
    obj = printer.lookup_object('copy_mirror', None)
    if obj is not None:
        return obj
    obj = CopyMirror(config)
    printer.add_object('copy_mirror', obj)
    return obj
