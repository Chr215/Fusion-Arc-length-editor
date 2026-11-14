"""Tests for the calculation logic using a simulated Fusion sketch solver (no Fusion needed).

Run with:  python tests/test_arc_length.py
"""
import importlib.util
import math
import os
import sys
import types

# ---------------------------------------------------------------- fake API
adsk = types.ModuleType('adsk')
core = types.ModuleType('adsk.core')
fusion = types.ModuleType('adsk.fusion')
for name in ('CommandCreatedEventHandler', 'CommandEventHandler', 'InputChangedEventHandler',
             'ValidateInputsEventHandler', 'SelectionEventHandler'):
    setattr(core, name, type(name, (object,), {}))


class Vector3D:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    @staticmethod
    def create(x, y, z):
        return Vector3D(x, y, z)


core.Vector3D = Vector3D
adsk.core, adsk.fusion = core, fusion
sys.modules.update({'adsk': adsk, 'adsk.core': core, 'adsk.fusion': fusion})

ADDIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                     'ArcLengthEditor', 'ArcLengthEditor.py')
spec = importlib.util.spec_from_file_location('addin', ADDIN)
addin = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(addin)

TWO_PI = 2 * math.pi


class Pt:
    def __init__(self, x, y):
        self.x, self.y, self.z = x, y, 0.0


class Coll:
    def __init__(self, n):
        self.count = n


class FakePoint:
    def __init__(self, arc, x, y):
        self.arc, self.x, self.y = arc, x, y
        self.locked = False       # move() returns False
        self.silent_lock = False  # move() returns True, but the solver reverts the move
        self.isFixed = False
        self.isFullyConstrained = False
        self.connectedEntities = Coll(1)

    @property
    def geometry(self):
        return Pt(self.x, self.y)

    def move(self, v):
        return self.arc.solver_move(self, v)


class FakeArc:
    """Arc with center, start and end point; length depends on the direction of rotation."""

    def __init__(self, cx, cy, r, start_deg, sweep_deg, cw=False, noise=0.0):
        self.cw = cw
        self.noise = noise
        self.is2D = True
        self.isReference = False
        a0 = math.radians(start_deg)
        a1 = a0 + (-1 if cw else 1) * math.radians(sweep_deg)
        self.centerSketchPoint = FakePoint(self, cx, cy)
        self.startSketchPoint = FakePoint(self, cx + r * math.cos(a0), cy + r * math.sin(a0))
        self.endSketchPoint = FakePoint(self, cx + r * math.cos(a1), cy + r * math.sin(a1))
        self.moves = 0

    @property
    def length(self):
        c, s, e = self.centerSketchPoint, self.startSketchPoint, self.endSketchPoint
        r = math.hypot(s.x - c.x, s.y - c.y)
        a_s = math.atan2(s.y - c.y, s.x - c.x)
        a_e = math.atan2(e.y - c.y, e.x - c.x)
        sweep = (a_s - a_e) % TWO_PI if self.cw else (a_e - a_s) % TWO_PI
        return r * sweep

    def solver_move(self, pt, v):
        self.moves += 1
        if pt.locked:
            return False
        c = self.centerSketchPoint
        other = self.endSketchPoint if pt is self.startSketchPoint else self.startSketchPoint
        old = (pt.x, pt.y, other.x, other.y)
        tx, ty = pt.x + v.x, pt.y + v.y
        r_new = math.hypot(tx - c.x, ty - c.y)
        r_old = math.hypot(other.x - c.x, other.y - c.y)
        pt.x, pt.y = tx + self.noise, ty - self.noise
        if abs(r_new - r_old) > 1e-9:       # a radial move changes the radius
            ang = math.atan2(other.y - c.y, other.x - c.x)
            other.x, other.y = c.x + r_new * math.cos(ang), c.y + r_new * math.sin(ang)
        if pt.silent_lock:
            pt.x, pt.y, other.x, other.y = old
        return True


# ---------------------------------------------------------------- helpers
def state(arc):
    return addin.read_arc_state(arc)


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def snapshot(arc):
    return [(p.x, p.y) for p in (arc.centerSketchPoint, arc.startSketchPoint, arc.endSketchPoint)]


def check_result(arc, before, target, r, mode, label):
    after = snapshot(arc)
    st = state(arc)
    assert close(arc.length, target, 1e-8), (label, arc.length, target)
    assert close(st.radius, r, 1e-9), (label, 'radius', st.radius, r)
    assert close(after[0][0], before[0][0]) and close(after[0][1], before[0][1]), (label, 'center')
    if mode == 'end':
        assert after[1] == before[1], (label, 'start point must stay')
    if mode == 'start':
        assert after[2] == before[2], (label, 'end point must stay')


def expect_arc_error(func, message):
    try:
        func()
    except addin.ArcError:
        return
    raise AssertionError(message)


# ---------------------------------------------------------------- tests
def test_state_reading():
    for cw in (False, True):
        arc = FakeArc(2, 3, 6.0, 40, 130, cw=cw)
        st = state(arc)
        assert st.direction == (-1 if cw else 1)
        assert close(st.sweep, math.radians(130), 1e-12)
        assert close(st.radius, 6.0, 1e-12)
    print('ok   reads radius, angle and direction of rotation')


def test_modes_and_angles():
    r = 5.0
    count = 0
    for cw in (False, True):
        for start in (10, 170, -170, 350):          # includes the jump at +-180 degrees
            for old, new in ((60, 100), (60, 20), (60, 300), (200, 40), (100, 179.5), (100, 180.5)):
                for mode in ('end', 'start', 'center'):
                    arc = FakeArc(1.0, -2.0, r, start, old, cw=cw)
                    before = snapshot(arc)
                    target = r * math.radians(new)
                    addin.apply_arc_length(arc, target, mode)
                    label = 'cw={} start={} {}->{} deg {}'.format(cw, start, old, new, mode)
                    check_result(arc, before, target, r, mode, label)
                    if mode == 'center':
                        # the bisector of the arc must be preserved
                        d = -1 if cw else 1
                        s = state(arc)
                        mid_new = s.a_start + d * s.sweep / 2
                        a_start_old = math.atan2(before[1][1] - before[0][1],
                                                 before[1][0] - before[0][0])
                        mid_old = a_start_old + d * math.radians(old) / 2
                        assert close(addin._wrap(mid_new - mid_old), 0.0, 1e-9), label
                    count += 1
    print('ok   {} combinations of direction, start angle, old/new angle and mode'.format(count))


def test_auto_prefers_free_end():
    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.endSketchPoint.connectedEntities = Coll(2)     # end is attached to a line
    before = snapshot(arc)
    addin.apply_arc_length(arc, 3.0 * math.radians(90), 'auto')
    assert snapshot(arc)[2] == before[2], 'connected end point must not move'
    check_result(arc, before, 3.0 * math.radians(90), 3.0, 'start', 'auto')

    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.startSketchPoint.isFixed = True
    before = snapshot(arc)
    addin.apply_arc_length(arc, 3.0 * math.radians(90), 'auto')
    check_result(arc, before, 3.0 * math.radians(90), 3.0, 'end', 'auto')
    print('ok   "Automatic" keeps connected / fixed points in place')


def test_auto_fallback_when_first_fails():
    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.endSketchPoint.locked = True
    before = snapshot(arc)
    addin.apply_arc_length(arc, 3.0 * math.radians(80), 'auto')
    check_result(arc, before, 3.0 * math.radians(80), 3.0, 'start', 'fallback')
    print('ok   "Automatic" falls back to the other end point')


def test_failure_rolls_back():
    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.startSketchPoint.locked = arc.endSketchPoint.locked = True
    before = snapshot(arc)
    expect_arc_error(lambda: addin.apply_arc_length(arc, 3.0 * math.radians(80), 'auto'),
                     'ArcError expected for locked points')
    assert snapshot(arc) == before
    print('ok   locked points -> error, arc unchanged')

    # the solver silently reverts the move (e.g. a driving length dimension)
    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.startSketchPoint.silent_lock = arc.endSketchPoint.silent_lock = True
    before = snapshot(arc)
    expect_arc_error(lambda: addin.apply_arc_length(arc, 3.0 * math.radians(80), 'auto'),
                     'ArcError expected when the solver reverts the move')
    assert snapshot(arc) == before
    print('ok   silent revert by the solver is detected')

    arc = FakeArc(0, 0, 3.0, 20, 50)
    arc.endSketchPoint.locked = True
    expect_arc_error(lambda: addin.apply_arc_length(arc, 3.0 * math.radians(80), 'end'),
                     'ArcError expected for explicit mode with locked point')
    print('ok   explicit mode with a locked point -> error')


def test_invalid_targets():
    arc = FakeArc(0, 0, 2.0, 0, 90)
    for bad in (0.0, -1.0, TWO_PI * 2.0, TWO_PI * 2.0 + 1):
        expect_arc_error(lambda b=bad: addin.apply_arc_length(arc, b, 'end'),
                         'ArcError expected for {}'.format(bad))
    arc.isReference = True
    expect_arc_error(lambda: addin.apply_arc_length(arc, 1.0, 'end'),
                     'reference arcs must be rejected')
    print('ok   invalid lengths and reference geometry are rejected')


def test_solver_noise_is_corrected():
    arc = FakeArc(0, 0, 4.0, 30, 70, noise=3e-7)      # solver is only accurate to ~1e-7
    target = 4.0 * math.radians(120)
    addin.apply_arc_length(arc, target, 'end')
    assert abs(arc.length - target) <= addin._tolerance(target)
    print('ok   imprecise solver is tolerated ({} moves)'.format(arc.moves))


def test_noop_when_already_correct():
    arc = FakeArc(0, 0, 4.0, 30, 70)
    addin.apply_arc_length(arc, arc.length, 'end')
    assert arc.moves == 0
    print('ok   same length -> nothing is changed')


if __name__ == '__main__':
    test_state_reading()
    test_modes_and_angles()
    test_auto_prefers_free_end()
    test_auto_fallback_when_first_fails()
    test_failure_rolls_back()
    test_invalid_targets()
    test_solver_noise_is_corrected()
    test_noop_when_already_correct()
    print('\nAll tests passed.')
