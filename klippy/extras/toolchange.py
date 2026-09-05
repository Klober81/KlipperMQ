# Sequential TOOLCHANGE (park then activate)
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class ToolSpec:
    def __init__(self, name, section_name, park_x, park_y, park_z_hop,
                 aliases, queue_name):
        self.name = name
        self.section_name = section_name
        self.park_x = park_x
        self.park_y = park_y
        self.park_z_hop = park_z_hop
        self.aliases = tuple(aliases)
        self.queue_name = queue_name


class Toolchange:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.current = None
        self.tools = []
        self._by_name = {}
        self._consumed = False
        gcode = self.printer.lookup_object('gcode', None)
        if gcode is not None:
            gcode.register_command(
                "TOOLCHANGE", self.cmd_TOOLCHANGE,
                desc=self.cmd_TOOLCHANGE_help)
        register = getattr(
            self.printer, 'register_event_handler', None)
        if register is not None:
            register('klippy:connect', self._handle_connect)

    def _handle_connect(self):
        self._consume()

    def _matching_queue(self, mq, name):
        if not name:
            return None
        q = mq.queue_map.get(name.lower())
        if q is not None:
            return q
        return mq.queue_map.get(('q_' + name).lower())

    def _consume(self):
        if self._consumed:
            return
        self._consumed = True
        mq = self.printer.lookup_object('mq_config', None)
        if mq is None:
            return
        for tc in mq.toolchanges:
            if tc.park_x is None and tc.park_y is None:
                raise self.printer.config_error(
                    "Section '%s' must specify park_x or park_y"
                    % (tc.section_name,))
            if not tc.name:
                continue
            aliases = [tc.name]
            q = self._matching_queue(mq, tc.name)
            qname = None
            if q is not None:
                qname = q.name
                have = set([a.lower() for a in aliases])
                for alias in q.aliases:
                    if alias.lower() in have:
                        continue
                    have.add(alias.lower())
                    aliases.append(alias)
            spec = ToolSpec(
                tc.name, tc.section_name, tc.park_x, tc.park_y,
                tc.park_z_hop, aliases, qname)
            self.tools.append(spec)
            for alias in aliases:
                self._by_name[alias.lower()] = spec

    def _valid_names(self):
        names = []
        seen = set()
        for spec in self.tools:
            for alias in spec.aliases:
                key = alias.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(alias)
        return ', '.join(names)

    def _lookup_tool(self, name, error):
        spec = self._by_name.get(name.lower())
        if spec is None:
            raise error(
                "Unknown tool '%s'. Valid names: %s"
                % (name, self._valid_names()))
        return spec

    def _queue_extruder(self, spec):
        mq = self.printer.lookup_object('mq_config', None)
        if mq is None:
            return None
        q = self._matching_queue(mq, spec.name)
        if q is None:
            return None
        return q.extruder

    def _carriage_index(self, spec):
        # Multi-queue: CARRIAGE from owned_axes (x=0, dual_carriage=1).
        # Stock / no queues: tools-list index (unchanged).
        mgr = self.printer.lookup_object('mq_manager', None)
        if mgr is not None and mgr.ownership.multi_queue:
            if not spec.queue_name:
                return None
            try:
                queue = mgr.lookup_queue(spec.queue_name)
            except self.printer.config_error:
                return None
            return mgr.carriage_for_queue(queue)
        for i, tool in enumerate(self.tools):
            if tool is spec:
                if i > 1:
                    return None
                return i
        return None

    def _dual_carriage_cmd(self, spec, gcmd=None):
        # IDEX: DualCarriages SET_DUAL_CARRIAGE CARRIAGE=0|1 (primary/dual).
        # Fail-fast when multi_queue and the queue owns neither carriage axis.
        dc = self.printer.lookup_object('dual_carriage', None)
        if dc is None:
            return None
        idx = self._carriage_index(spec)
        if idx is None:
            mgr = self.printer.lookup_object('mq_manager', None)
            if (gcmd is not None and mgr is not None
                    and mgr.ownership.multi_queue and spec.queue_name):
                raise gcmd.error(
                    "Queue '%s' owns neither x nor dual_carriage"
                    % (spec.queue_name,))
            return None
        return 'SET_DUAL_CARRIAGE CARRIAGE=%d' % (idx,)

    def _activate_lines(self, spec, gcmd=None):
        lines = []
        dc = self._dual_carriage_cmd(spec, gcmd)
        if dc is not None:
            lines.append(dc)
        extruder = self._queue_extruder(spec)
        if extruder:
            lines.append(
                'ACTIVATE_EXTRUDER EXTRUDER=%s' % (extruder,))
        return lines

    def _park_lines(self, spec):
        lines = []
        hop = spec.park_z_hop
        if hop:
            lines.append('G91')
            lines.append('G1 Z%.6g' % (hop,))
            lines.append('G90')
        move = []
        if spec.park_x is not None:
            move.append('X%.6g' % (spec.park_x,))
        if spec.park_y is not None:
            move.append('Y%.6g' % (spec.park_y,))
        lines.append('G1 ' + ' '.join(move))
        return hop, lines

    cmd_TOOLCHANGE_help = (
        "Park the current tool and activate TOOL")
    def cmd_TOOLCHANGE(self, gcmd):
        self._consume()
        name = gcmd.get('TOOL')
        spec = self._lookup_tool(name, gcmd.error)
        if self.current is spec:
            return
        gcode = self.printer.lookup_object('gcode')
        lines = []
        hop = 0.
        outgoing = self.current
        if outgoing is not None:
            dc = self._dual_carriage_cmd(outgoing, gcmd)
            if dc is not None:
                lines.append(dc)
            hop, park = self._park_lines(outgoing)
            lines.extend(park)
        lines.extend(self._activate_lines(spec, gcmd))
        if hop:
            lines.append('G91')
            lines.append('G1 Z%.6g' % (-hop,))
            lines.append('G90')
        if lines:
            gcode.run_script_from_command('\n'.join(lines))
        self.current = spec

    def get_status(self, eventtime=None):
        cur = None
        if self.current is not None:
            cur = self.current.name
        tools = {}
        for spec in self.tools:
            tools[spec.name] = {
                'park_x': spec.park_x,
                'park_y': spec.park_y,
                'park_z_hop': spec.park_z_hop,
                'queue': spec.queue_name,
            }
        return {'current': cur, 'tools': tools}


def load_config(config):
    printer = config.get_printer()
    obj = printer.lookup_object('toolchange', None)
    if obj is not None:
        return obj
    obj = Toolchange(config)
    printer.add_object('toolchange', obj)
    return obj

def load_config_prefix(config):
    return load_config(config)
