# -*- coding: utf-8 -*-
"""
Arc Length Editor  -  add-in for Autodesk Fusion (Fusion 360)

Workflow:
  1. Draw an arc in a sketch.
  2. Start the "Set Arc Length" command
     (Sketch > Modify, or Solid > Modify).
  3. Select the arc, enter the desired arc length, click OK.

The add-in reads the radius and arc length of the selected arc, computes the
required included angle  alpha = b / r  and moves one (or both) end points
along the existing circle. Center point and radius stay unchanged.

Units: Fusion works internally in cm and radians. All lengths in this module
are cm and all angles are radians unless stated otherwise.
"""

import math
import os
import traceback

import adsk.core
import adsk.fusion

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
CMD_ID = 'ArcLengthEditor_cmd'
CMD_NAME = 'Set Arc Length'
CMD_DESCRIPTION = ('Sets the included angle of a sketch arc so that the arc '
                   'has the entered length. The radius stays unchanged.')

RESOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')

ID_SELECTION = 'ale_arc'
ID_INFO = 'ale_info'
ID_LENGTH = 'ale_length'
ID_MODE = 'ale_mode'
ID_RESULT = 'ale_result'

# (internal key, text shown in the drop-down)
MODES = [
    ('auto', 'Automatic (free end moves)'),
    ('end', 'End point moves, start point stays'),
    ('start', 'Start point moves, end point stays'),
    ('center', 'Symmetric about the arc midpoint'),
]

TWO_PI = 2.0 * math.pi
LEN_EPS = 1e-9          # smallest meaningful arc length (cm)
MAX_PASSES = 3          # follow-up passes in case the solver deviates slightly
CONVERGED = 1e-8        # target accuracy of the length after moving (cm)


def _tolerance(length):
    """Allowed deviation when verifying the result (cm)."""
    return max(1e-5, 1e-7 * abs(length))


# --------------------------------------------------------------------------
# Global state (references to handlers must be kept alive)
# --------------------------------------------------------------------------
_app = None
_ui = None
_handlers = []          # live as long as the add-in
_cmd_handlers = []      # live as long as the dialog


class ArcError(Exception):
    """Error with a message that is shown to the user as-is."""


# --------------------------------------------------------------------------
# Formatting (uses Fusion's unit manager, falls back to cm)
# --------------------------------------------------------------------------
def _design():
    if _app is None:
        return None
    return adsk.fusion.Design.cast(_app.activeProduct)


def _default_units():
    try:
        return _design().unitsManager.defaultLengthUnits
    except Exception:
        return 'mm'


def format_length(value_cm):
    try:
        um = _design().unitsManager
        return um.formatInternalValue(value_cm, um.defaultLengthUnits, True)
    except Exception:
        return '{:.4f} cm'.format(value_cm)


def format_angle(value_rad):
    try:
        return _design().unitsManager.formatInternalValue(value_rad, 'deg', True)
    except Exception:
        return '{:.4f}°'.format(math.degrees(value_rad))


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
class ArcState(object):
    """Snapshot of a sketch arc (sketch coordinates)."""

    def __init__(self, cx, cy, radius, a_start, a_end, length, direction):
        self.cx = cx
        self.cy = cy
        self.radius = radius
        self.a_start = a_start      # angle of the start point around the center
        self.a_end = a_end          # angle of the end point around the center
        self.length = length        # arc length
        self.sweep = length / radius  # included angle (rad)
        self.direction = direction  # +1: start->end counterclockwise, -1: clockwise


def _wrap(angle):
    """Wrap an angle into (-pi, pi]."""
    return (angle + math.pi) % TWO_PI - math.pi


def read_arc_state(arc):
    """Reads center, radius, length and direction of rotation of the arc."""
    if getattr(arc, 'is2D', True) is False:
        raise ArcError('The arc does not lie in the sketch plane (3D sketch). '
                       'This is not supported.')

    c = arc.centerSketchPoint.geometry
    s = arc.startSketchPoint.geometry
    e = arc.endSketchPoint.geometry

    radius = math.hypot(s.x - c.x, s.y - c.y)
    if radius < 1e-9:
        raise ArcError('The arc has a radius of 0.')

    length = arc.length
    sweep = length / radius
    a_start = math.atan2(s.y - c.y, s.x - c.x)
    a_end = math.atan2(e.y - c.y, e.x - c.x)

    # According to the API, Fusion sketch arcs always run counterclockwise from
    # the start point to the end point. To be safe, the direction is derived
    # from the length and the point positions instead of just being assumed.
    gap = (a_end - a_start) % TWO_PI            # angle start -> end, counterclockwise
    err_ccw = abs(_wrap(sweep - gap))
    err_cw = abs(_wrap(sweep + gap))
    if min(err_ccw, err_cw) > 1e-3:
        raise ArcError('The arc could not be evaluated '
                       '(length and end points are inconsistent).')
    direction = 1 if err_ccw <= err_cw + 1e-9 else -1

    return ArcState(c.x, c.y, radius, a_start, a_end, length, direction)


def _target_points(st, new_sweep, mode):
    """New positions of the end points on the existing circle.

    Returns {'start': (x, y)} and/or {'end': (x, y)}.
    """
    d = st.direction

    def pos(angle):
        return (st.cx + st.radius * math.cos(angle),
                st.cy + st.radius * math.sin(angle))

    if mode == 'end':
        return {'end': pos(st.a_start + d * new_sweep)}
    if mode == 'start':
        return {'start': pos(st.a_end - d * new_sweep)}
    if mode == 'center':
        mid = st.a_start + d * st.sweep / 2.0
        return {'start': pos(mid - d * new_sweep / 2.0),
                'end': pos(mid + d * new_sweep / 2.0)}
    raise ArcError('Unknown mode: {}'.format(mode))


def _move_point(point, x, y):
    """Moves a sketch point to (x, y). Constraints are respected."""
    g = point.geometry
    dx, dy = x - g.x, y - g.y
    if math.hypot(dx, dy) < 1e-12:
        return True
    return bool(point.move(adsk.core.Vector3D.create(dx, dy, 0.0)))


def _freedom_rank(point):
    """The lower, the freer the point is (and the sooner it is moved)."""
    rank = 0
    try:
        if point.isFixed:
            rank += 1000
    except Exception:
        pass
    try:
        if point.isFullyConstrained:
            rank += 500
    except Exception:
        pass
    try:
        rank += point.connectedEntities.count      # includes the arc itself
    except Exception:
        pass
    return rank


def _auto_order(points):
    ranked = sorted(('end', 'start'), key=lambda k: _freedom_rank(points[k]))
    return ranked + ['center']


def _result_ok(arc, target_length, center0, radius0):
    st = read_arc_state(arc)
    tol = _tolerance(target_length)
    return (abs(arc.length - target_length) <= tol
            and abs(st.radius - radius0) <= tol
            and math.hypot(st.cx - center0[0], st.cy - center0[1]) <= tol)


def _attempt(arc, points, mode, target_length, center0, radius0):
    """One attempt with a given mode. True if the result is correct."""
    try:
        for _ in range(MAX_PASSES):
            st = read_arc_state(arc)
            new_sweep = target_length / st.radius
            for key, (x, y) in _target_points(st, new_sweep, mode).items():
                if not _move_point(points[key], x, y):
                    return False
            if abs(arc.length - target_length) <= CONVERGED:
                break
        return _result_ok(arc, target_length, center0, radius0)
    except (RuntimeError, ArcError):
        return False


def _restore(points, original):
    for key, (x, y) in original.items():
        try:
            _move_point(points[key], x, y)
        except RuntimeError:
            pass


def apply_arc_length(arc, target_length, mode='auto'):
    """Sets the arc length without changing radius or center point.

    target_length in cm. Raises ArcError if that is not possible; the arc is
    then reset to its original state.
    """
    if getattr(arc, 'isReference', False):
        raise ArcError('The arc is reference geometry (e.g. projected) '
                       'and cannot be modified.')

    st = read_arc_state(arc)
    full = TWO_PI * st.radius
    if target_length <= LEN_EPS:
        raise ArcError('The arc length must be greater than 0.')
    if target_length >= full - LEN_EPS:
        raise ArcError('The arc length must be less than the full circle '
                       '(2·π·r = {}).'.format(format_length(full)))
    if abs(target_length - st.length) <= _tolerance(target_length):
        return  # the arc already has the requested length

    points = {'start': arc.startSketchPoint, 'end': arc.endSketchPoint}
    original = {k: (p.geometry.x, p.geometry.y) for k, p in points.items()}
    center0 = (st.cx, st.cy)

    order = _auto_order(points) if mode == 'auto' else [mode]
    for m in order:
        if _attempt(arc, points, m, target_length, center0, st.radius):
            return
        _restore(points, original)

    raise ArcError('The arc could not be set to the requested length. '
                   'Dimensions or constraints are probably holding it in '
                   'place (e.g. an angle or length dimension, fixed or '
                   'connected end points). Remove them or choose a different '
                   'option under "Move". '
                   'The arc was reset to its original state.')


# --------------------------------------------------------------------------
# Dialog helpers
# --------------------------------------------------------------------------
def _selected_arc(inputs):
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(ID_SELECTION))
    if sel is None or sel.selectionCount < 1:
        return None
    arc = adsk.fusion.SketchArc.cast(sel.selection(0).entity)
    if arc is None:
        return None
    native = arc.nativeObject
    return native if native else arc


def _read_length(inputs):
    """Entered arc length in cm, or None if the expression is invalid."""
    vi = adsk.core.ValueCommandInput.cast(inputs.itemById(ID_LENGTH))
    if vi is None or not vi.isValidExpression:
        return None
    return vi.value


def _selected_mode(inputs):
    dd = adsk.core.DropDownCommandInput.cast(inputs.itemById(ID_MODE))
    if dd is not None and dd.selectedItem is not None:
        for key, label in MODES:
            if label == dd.selectedItem.name:
                return key
    return 'auto'


def _preview_text(st, target):
    if target is None:
        return 'Invalid input.'
    full = TWO_PI * st.radius
    if target <= LEN_EPS:
        return 'The arc length must be greater than 0.'
    if target >= full - LEN_EPS:
        return ('The arc length must be less than the full circle ({}).'
                .format(format_length(full)))
    new_sweep = target / st.radius
    return ('New angle: <b>{}</b> (was {})<br>'
            'Radius stays: <b>{}</b>'
            .format(format_angle(new_sweep), format_angle(st.sweep),
                    format_length(st.radius)))


def _inputs_valid(inputs):
    arc = _selected_arc(inputs)
    target = _read_length(inputs)
    if arc is None or target is None:
        return False
    try:
        st = read_arc_state(arc)
    except ArcError:
        return False
    return LEN_EPS < target < TWO_PI * st.radius - LEN_EPS


def _refresh(inputs, reset_length):
    """Updates the info and result fields; optionally resets the length to the current value."""
    sel = adsk.core.SelectionCommandInput.cast(inputs.itemById(ID_SELECTION))
    info = adsk.core.TextBoxCommandInput.cast(inputs.itemById(ID_INFO))
    result = adsk.core.TextBoxCommandInput.cast(inputs.itemById(ID_RESULT))
    length_in = adsk.core.ValueCommandInput.cast(inputs.itemById(ID_LENGTH))

    arc = _selected_arc(inputs)
    if arc is None:
        if sel.selectionCount > 0:          # something other than an arc
            sel.clearSelection()
            info.formattedText = 'The selection is not a sketch arc.'
        else:
            info.formattedText = 'Select an arc in a sketch.'
        result.formattedText = ''
        return

    try:
        st = read_arc_state(arc)
    except ArcError as ex:
        info.formattedText = str(ex)
        result.formattedText = ''
        return

    info.formattedText = ('Radius: <b>{}</b><br>'
                          'Included angle: <b>{}</b><br>'
                          'Arc length: <b>{}</b>'
                          .format(format_length(st.radius),
                                  format_angle(st.sweep),
                                  format_length(st.length)))
    if reset_length:
        length_in.value = st.length
    result.formattedText = _preview_text(st, _read_length(inputs))


def _report_error():
    if _ui:
        _ui.messageBox('Unexpected error:\n{}'.format(traceback.format_exc()), CMD_NAME)


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------
class PreSelectHandler(adsk.core.SelectionEventHandler):
    """Only arcs are selectable (the 'SketchCurves' filter allows all curves)."""

    def notify(self, args):
        try:
            a = adsk.core.SelectionEventArgs.cast(args)
            if adsk.fusion.SketchArc.cast(a.selection.entity) is None:
                a.isSelectable = False
        except Exception:
            _report_error()


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            a = adsk.core.InputChangedEventArgs.cast(args)
            cid = a.input.id
            if cid == ID_SELECTION:
                _refresh(a.inputs, reset_length=True)
                if _selected_arc(a.inputs) is not None:
                    try:
                        a.inputs.itemById(ID_LENGTH).hasFocus = True
                    except Exception:
                        pass
            elif cid in (ID_LENGTH, ID_MODE):
                _refresh(a.inputs, reset_length=False)
        except Exception:
            _report_error()


class ValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        a = adsk.core.ValidateInputsEventArgs.cast(args)
        try:
            a.areInputsValid = _inputs_valid(a.inputs)
        except Exception:
            a.areInputsValid = False


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            a = adsk.core.CommandEventArgs.cast(args)
            inputs = a.command.commandInputs
            arc = _selected_arc(inputs)
            target = _read_length(inputs)
            if arc is None or target is None:
                raise ArcError('Please select an arc and enter a valid '
                               'arc length.')
            apply_arc_length(arc, target, _selected_mode(inputs))
        except ArcError as ex:
            _ui.messageBox(str(ex), CMD_NAME)
        except Exception:
            _report_error()


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _cmd_handlers
        _cmd_handlers = []


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = adsk.core.CommandCreatedEventArgs.cast(args).command
            inputs = cmd.commandInputs

            sel = inputs.addSelectionInput(ID_SELECTION, 'Arc',
                                           'Select a sketch arc')
            sel.addSelectionFilter('SketchCurves')
            sel.setSelectionLimits(1, 1)

            inputs.addTextBoxCommandInput(
                ID_INFO, 'Current arc', 'Select an arc in a sketch.', 3, True)

            inputs.addValueInput(ID_LENGTH, 'New arc length', _default_units(),
                                 adsk.core.ValueInput.createByReal(1.0))

            dd = inputs.addDropDownCommandInput(
                ID_MODE, 'Move',
                adsk.core.DropDownStyles.TextListDropDownStyle)
            for i, (_, label) in enumerate(MODES):
                dd.listItems.add(label, i == 0)
            dd.tooltip = 'Which end point is moved?'
            dd.tooltipDescription = (
                'In Fusion, a sketch arc always runs counterclockwise from its '
                'start point to its end point. "Automatic" moves the end point '
                'that is less connected to other geometry. The radius and the '
                'center point always stay unchanged.')

            inputs.addTextBoxCommandInput(ID_RESULT, 'Result', '', 2, True)

            for event, handler in ((cmd.execute, ExecuteHandler()),
                                   (cmd.inputChanged, InputChangedHandler()),
                                   (cmd.validateInputs, ValidateHandler()),
                                   (cmd.preSelect, PreSelectHandler()),
                                   (cmd.destroy, DestroyHandler())):
                event.add(handler)
                _cmd_handlers.append(handler)

            # If an arc was already selected when the command started, evaluate it now.
            _refresh(inputs, reset_length=True)
        except Exception:
            _report_error()


# --------------------------------------------------------------------------
# Add-in start / stop
# --------------------------------------------------------------------------
def _find_panels():
    """Sketch Modify panel (if present) and Solid Modify panel."""
    panels = _ui.allToolbarPanels
    found = []
    for i in range(panels.count):
        panel = panels.item(i)
        pid = panel.id.lower()
        if panel.id == 'SolidModifyPanel' or ('sketch' in pid and 'modify' in pid):
            found.append(panel)
    if not found:
        fallback = panels.itemById('SolidScriptsAddinsPanel')
        if fallback:
            found.append(fallback)
    return found


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        old = _ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()

        icon_dir = RESOURCE_DIR if os.path.isdir(RESOURCE_DIR) else ''
        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME, CMD_DESCRIPTION, icon_dir)

        handler = CommandCreatedHandler()
        cmd_def.commandCreated.add(handler)
        _handlers.append(handler)

        for panel in _find_panels():
            leftover = panel.controls.itemById(CMD_ID)
            if leftover:
                leftover.deleteMe()
            control = panel.controls.addCommand(cmd_def)
            if control and 'sketch' in panel.id.lower():
                try:
                    control.isPromoted = True
                except Exception:
                    pass
    except Exception:
        if _ui:
            _ui.messageBox('Failed to start the add-in:\n{}'
                           .format(traceback.format_exc()), CMD_NAME)


def stop(context):
    try:
        panels = _ui.allToolbarPanels
        for i in range(panels.count):
            control = panels.item(i).controls.itemById(CMD_ID)
            if control:
                control.deleteMe()
        cmd_def = _ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
    except Exception:
        if _ui:
            _ui.messageBox('Failed to stop the add-in:\n{}'
                           .format(traceback.format_exc()), CMD_NAME)
