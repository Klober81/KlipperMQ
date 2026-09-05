# Multi-queue configuration (single validated object at startup)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

DEFAULT_MAX_QUEUES = 5
PRIMARY_NAME = 'q_T0'
PRIMARY_ALIASES = ('primary', 'q_T0', 'queue_T0')
_PRIMARY_KEYS = frozenset([a.lower() for a in PRIMARY_ALIASES])


def _iter_prefix_sections(config, prefix):
    prefix_sp = prefix + ' '
    for section in config.get_prefix_sections(prefix):
        name = section.get_name()
        if name == prefix or name.startswith(prefix_sp):
            yield section

def _section_suffix(section_name, prefix):
    if section_name == prefix:
        return None
    return section_name[len(prefix) + 1:]

def _is_primary_token(token):
    if token is None:
        return True
    return token.lower() in _PRIMARY_KEYS


class QueueConfig:
    def __init__(self, name, aliases, is_primary, is_implicit, owned_axes,
                 extruder, section_name):
        self.name = name
        self.aliases = tuple(aliases)
        self.is_primary = is_primary
        self.is_implicit = is_implicit
        self.owned_axes = tuple(owned_axes)
        self.extruder = extruder
        self.section_name = section_name


class ToolchangeConfig:
    def __init__(self, name, section_name, park_x, park_y, park_z_hop):
        self.name = name
        self.section_name = section_name
        self.park_x = park_x
        self.park_y = park_y
        self.park_z_hop = park_z_hop


class CopyConfig:
    def __init__(self, section_name, source):
        self.section_name = section_name
        self.source = source


class MirrorConfig:
    def __init__(self, section_name, source, axis, center):
        self.section_name = section_name
        self.source = source
        self.axis = axis
        self.center = center


class RecoveryConfig:
    def __init__(self, bed_temp_hold_time, require_user_confirmation,
                 restore_chamber, bed_temp_threshold, chamber_temp_threshold,
                 z_hop_on_recover, section_present, filename=None):
        self.bed_temp_hold_time = bed_temp_hold_time
        self.require_user_confirmation = require_user_confirmation
        self.restore_chamber = restore_chamber
        self.bed_temp_threshold = bed_temp_threshold
        self.chamber_temp_threshold = chamber_temp_threshold
        self.z_hop_on_recover = z_hop_on_recover
        self.section_present = section_present
        self.filename = filename


class MQConfig:
    def __init__(self, config):
        self.printer = config.get_printer()
        self._check_max_queues_placement(config)
        self.max_queues = self._parse_max_queues(config)
        self.queues, self.queue_map = self._parse_queues(config)
        self.primary = self.queue_map[PRIMARY_NAME.lower()]
        self._check_axis_ownership(config)
        if len(self.queues) > self.max_queues:
            raise config.error(
                "Too many queues (%d) - max_queues is %d"
                % (len(self.queues), self.max_queues))
        self.toolchanges = self._parse_toolchanges(config)
        self.copy = self._parse_copy(config)
        self.mirror = self._parse_mirror(config)
        self.recovery = self._parse_recovery(config)

    def lookup_queue(self, name):
        q = self.queue_map.get(name.lower())
        if q is None:
            raise self.printer.config_error("Unknown queue '%s'" % (name,))
        return q

    def get_status(self, eventtime=None):
        queues = {}
        for q in self.queues:
            queues[q.name] = {
                'aliases': list(q.aliases),
                'is_primary': q.is_primary,
                'is_implicit': q.is_implicit,
                'owned_axes': list(q.owned_axes),
                'extruder': q.extruder,
            }
        return {
            'max_queues': self.max_queues,
            'primary': self.primary.name,
            'queues': queues,
        }

    def _check_max_queues_placement(self, config):
        fileconfig = config.fileconfig
        for section in fileconfig.sections():
            if not fileconfig.has_option(section, 'max_queues'):
                continue
            if section.lower() != 'printer':
                raise config.error(
                    "Option 'max_queues' is only valid in section 'printer'")

    def _parse_max_queues(self, config):
        printer_cfg = config.getsection('printer')
        if not config.fileconfig.has_option('printer', 'max_queues'):
            return DEFAULT_MAX_QUEUES
        return printer_cfg.getint('max_queues', minval=1)

    def _parse_queues(self, config):
        queues = []
        queue_map = {}
        have_primary = False
        for qsection in _iter_prefix_sections(config, 'queue'):
            section_name = qsection.get_name()
            token = _section_suffix(section_name, 'queue')
            is_primary = _is_primary_token(token)
            owned_axes = self._parse_owned_axes(qsection, section_name)
            extruder = qsection.get('extruder', None)
            if is_primary:
                if have_primary:
                    raise config.error(
                        "Duplicate primary queue in section '%s'"
                        % (section_name,))
                have_primary = True
                name = PRIMARY_NAME
                aliases = PRIMARY_ALIASES
            else:
                name = token
                aliases = (token,)
            q = QueueConfig(name, aliases, is_primary, False, owned_axes,
                            extruder, section_name)
            self._register_queue(config, queue_map, q, section_name)
            queues.append(q)
        if not have_primary:
            q = QueueConfig(PRIMARY_NAME, PRIMARY_ALIASES, True, True, (),
                            None, None)
            self._register_queue(config, queue_map, q, None)
            queues.insert(0, q)
        return queues, queue_map

    def _parse_owned_axes(self, qsection, section_name):
        owned = qsection.getlist('owned_axes')
        axes = tuple([a.strip().lower() for a in owned])
        if not axes or any(not a for a in axes):
            raise qsection.error(
                "Section '%s' must specify a non-empty owned_axes"
                % (section_name,))
        return axes

    def _register_queue(self, config, queue_map, q, section_name):
        for alias in q.aliases:
            key = alias.lower()
            if key in queue_map:
                where = section_name or q.name
                raise config.error(
                    "Duplicate queue name '%s' in section '%s'"
                    % (alias, where))
            queue_map[key] = q

    def _check_axis_ownership(self, config):
        owned_by = {}
        for q in self.queues:
            for axis in q.owned_axes:
                prev = owned_by.get(axis)
                if prev is not None:
                    raise config.error(
                        "Axis '%s' is owned by both '%s' and '%s'"
                        % (axis, prev.name, q.name))
                owned_by[axis] = q

    def _parse_toolchanges(self, config):
        toolchanges = []
        for tsection in _iter_prefix_sections(config, 'toolchange'):
            section_name = tsection.get_name()
            token = _section_suffix(section_name, 'toolchange')
            name = '' if token is None else token
            park_x = tsection.getfloat('park_x', None)
            park_y = tsection.getfloat('park_y', None)
            park_z_hop = tsection.getfloat('park_z_hop', 0., minval=0.)
            if park_x is None and park_y is None:
                raise tsection.error(
                    "Section '%s' must specify park_x or park_y"
                    % (section_name,))
            toolchanges.append(ToolchangeConfig(
                name, section_name, park_x, park_y, park_z_hop))
        return toolchanges


    def _parse_copy(self, config):
        if not config.has_section("copy"):
            return None
        section = config.getsection("copy")
        section_name = section.get_name()
        source = section.get("source", None)
        return CopyConfig(section_name, source)

    def _parse_mirror(self, config):
        if not config.has_section("mirror"):
            return None
        section = config.getsection("mirror")
        section_name = section.get_name()
        # Fail-fast: axis and center are required on [mirror]
        if not config.fileconfig.has_option(section_name, "axis"):
            raise section.error(
                "Section '%s' must specify axis" % (section_name,))
        if not config.fileconfig.has_option(section_name, "center"):
            raise section.error(
                "Section '%s' must specify center" % (section_name,))
        axis = section.get("axis")
        center = section.getfloat("center")
        source = section.get("source", None)
        return MirrorConfig(section_name, source, axis, center)

    def _parse_recovery(self, config):
        if not config.has_section('recovery'):
            return RecoveryConfig(
                0., True, True, None, None, None, False, None)
        rsection = config.getsection('recovery')
        return RecoveryConfig(
            rsection.getfloat('bed_temp_hold_time', 0., minval=0.),
            rsection.getboolean('require_user_confirmation', True),
            rsection.getboolean('restore_chamber', True),
            rsection.getfloat('bed_temp_threshold', None),
            rsection.getfloat('chamber_temp_threshold', None),
            rsection.getfloat('z_hop_on_recover', above=0.),
            True,
            rsection.get('filename', '~/printer_data/mq_recovery.state'))


def load_config(config):
    return MQConfig(config)
