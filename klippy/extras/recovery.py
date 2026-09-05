# Cartesian print recovery coordinator (opt-in [recovery])
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json, logging, os

# OPEN: provisional internal checkpoint command name; not a public
# QUERY_BOOKMARK / RESUME_* API. Do not close ARCHITECTURE section 12.


class BookmarkRecord:
    def __init__(self, seq_id, filename=None, file_position=None):
        self.seq_id = int(seq_id)
        self.filename = filename
        self.file_position = file_position

    def to_dict(self):
        return {
            'seq_id': self.seq_id,
            'filename': self.filename,
            'file_position': self.file_position,
        }

    @staticmethod
    def from_dict(data):
        if not data:
            return None
        return BookmarkRecord(
            data.get('seq_id', 0),
            data.get('filename'),
            data.get('file_position'))


class RecoveryState:
    """Persisted live + last bookmark; live cleared after finish."""

    def __init__(self, live=None, last=None):
        self.live = live
        self.last = last

    def should_auto_resume(self):
        return self.live is not None

    def to_dict(self):
        live = None
        if self.live is not None:
            live = self.live.to_dict()
        last = None
        if self.last is not None:
            last = self.last.to_dict()
        return {'live': live, 'last': last}

    @classmethod
    def load(cls, path):
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, 'r') as f:
                data = json.load(f)
        except (OSError, ValueError, TypeError):
            logging.exception("recovery: unable to load %s", path)
            return cls()
        return cls(
            BookmarkRecord.from_dict(data.get('live')),
            BookmarkRecord.from_dict(data.get('last')))

    def save(self, path):
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp, path)


class Recovery:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.cfg = None
        self.state_path = None
        self._state = RecoveryState()
        self._consumed = False
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is not None:
            # OPEN: provisional internal; not QUERY_BOOKMARK/RESUME_*.
            gcode.register_command(
                '_MQ_RECOVERY_CHECKPOINT',
                self.cmd_MQ_RECOVERY_CHECKPOINT,
                desc=self.cmd_MQ_RECOVERY_CHECKPOINT_help)
        register = getattr(
            self.printer, 'register_event_handler', None)
        if register is not None:
            register('klippy:connect', self._handle_connect)
            register('print_stats:finish', self._handle_finish)

    def _handle_connect(self):
        self._consume()

    def _consume(self):
        if self._consumed:
            return
        self._consumed = True
        mq = self.printer.lookup_object('mq_config')
        self.cfg = mq.recovery
        if not self.cfg.section_present:
            raise self.printer.config_error(
                "recovery loaded without [recovery] section")
        if self.cfg.z_hop_on_recover is None \
                or self.cfg.z_hop_on_recover <= 0.:
            raise self.printer.config_error(
                "Option 'z_hop_on_recover' in section 'recovery' "
                "must be a positive float")
        path = self.cfg.filename
        if not path:
            path = '~/printer_data/mq_recovery.state'
        self.state_path = os.path.expanduser(path)
        self._state = RecoveryState.load(self.state_path)

    def _handle_finish(self, state):
        if state in ('complete', 'cancelled', 'error'):
            self.clear_live(state)

    @property
    def live(self):
        return self._state.live

    @live.setter
    def live(self, value):
        self._state.live = value

    @property
    def last(self):
        return self._state.last

    @last.setter
    def last(self, value):
        self._state.last = value

    def _persist(self):
        self._consume()
        self._state.save(self.state_path)

    def _log_bookmark(self, record):
        msg = "recovery: last bookmark seq=%u" % (record.seq_id,)
        if record.filename:
            msg += " file=%s" % (record.filename,)
        logging.info(msg)
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is not None:
            gcode.respond_info(msg)

    def note_bookmark(self, seq_id, filename=None,
                      file_position=None):
        self._consume()
        if filename is None:
            ps = self.printer.lookup_object('print_stats', None)
            if ps is not None:
                filename = getattr(ps, 'filename', None) or None
        record = BookmarkRecord(seq_id, filename, file_position)
        self.live = record
        self.last = record
        self._persist()
        self._log_bookmark(record)
        return record

    def clear_live(self, reason='clear'):
        self._consume()
        if self.live is None:
            return
        logging.info("recovery: clear live (%s); last seq=%s",
                     reason,
                     None if self.last is None else self.last.seq_id)
        self.live = None
        self._persist()

    def lookup_bookmark_seq(self):
        mq = self.printer.lookup_object('motion_queuing', None)
        if mq is None:
            return None
        return mq.bookmarks.last_id

    def sync_from_motion_queuing(self):
        seq = self.lookup_bookmark_seq()
        if seq is None or seq <= 0:
            return None
        ps = self.printer.lookup_object('print_stats', None)
        filename = None
        if ps is not None:
            filename = getattr(ps, 'filename', None) or None
        return self.note_bookmark(seq, filename=filename)

    cmd_MQ_RECOVERY_CHECKPOINT_help = (
        "OPEN internal: persist BookmarkSeq last_id")
    def cmd_MQ_RECOVERY_CHECKPOINT(self, gcmd):
        self.sync_from_motion_queuing()

    def should_restore_chamber(self):
        self._consume()
        if not self.cfg.restore_chamber:
            return False
        return self._chamber_heater() is not None

    def _chamber_heater(self):
        printer = self.printer
        for name in ('heater_generic chamber', 'chamber'):
            obj = printer.lookup_object(name, None)
            if obj is not None:
                return obj
        return None

    def _run(self, lines):
        if not lines:
            return
        gcode = self.printer.lookup_object('gcode')
        gcode.run_script_from_command('\n'.join(lines))

    def build_recovery_script(self):
        """Thin Cartesian steps 1-10 via stock gcode where possible."""
        self._consume()
        cfg = self.cfg
        lines = []
        # 1: load saved state (host-side); no motion yet
        # 2: bed/chamber temps - targets from snapshot later.
        #    Thresholds are degrees below restored target, not
        #    absolute MINIMUM. Omit TEMPERATURE_WAIT until
        #    setpoints exist (do not use raw threshold).
        if cfg.bed_temp_hold_time:
            lines.append('G4 P%d' % (
                int(cfg.bed_temp_hold_time * 1000.),))
        # 3: forced positive Z hop
        lines.append('G91')
        lines.append('G1 Z%.6g' % (cfg.z_hop_on_recover,))
        lines.append('G90')
        # 4: home X and Y
        lines.append('G28 X Y')
        # 5-7: safe XY = user jog for v1; confirmation gated by cfg
        if cfg.require_user_confirmation:
            lines.append(
                'M117 Jog to safe XY then confirm recovery')
        # 8: Z home/probe at confirmed location (thin)
        lines.append('G28 Z')
        # 9-10: remaining state / resume options — host prompts
        lines.append('M117 Recovery ready: resume/pause/cancel')
        return lines

    def run_recovery_sequence(self):
        self._run(self.build_recovery_script())

    def get_status(self, eventtime=None):
        self._consume()
        live = None
        if self.live is not None:
            live = self.live.to_dict()
        last = None
        if self.last is not None:
            last = self.last.to_dict()
        return {
            'live': live,
            'last': last,
            'z_hop_on_recover': self.cfg.z_hop_on_recover,
            'require_user_confirmation':
                self.cfg.require_user_confirmation,
            'restore_chamber': self.cfg.restore_chamber,
            'should_restore_chamber': self.should_restore_chamber(),
        }


def load_config(config):
    printer = config.get_printer()
    obj = printer.lookup_object('recovery', None)
    if obj is not None:
        return obj
    obj = Recovery(config)
    printer.add_object('recovery', obj)
    return obj
