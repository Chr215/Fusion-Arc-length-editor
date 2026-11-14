# Arc Length Editor for Fusion

A small add-in for Autodesk Fusion (Fusion 360) that sets the **length of a sketch arc** by changing its **included angle**, while the **radius and center point stay exactly the same**.

Draw an arc, select it, type the arc length you want, and the add-in works out the angle for you (α = b / r).

## Features

- Select an existing sketch arc; the dialog shows its radius, included angle and current length.
- Enter the new arc length in your document units. Expressions and parameters work like in any other Fusion field.
- The resulting angle is previewed in the dialog before you confirm.
- Choose which end point moves: automatic, end point, start point, or symmetric about the arc midpoint.
- Respects existing constraints: if a move is not possible, the arc is restored and you get a clear message instead of a broken sketch.

## Installation

1. Download the latest release ZIP (or clone this repository).
2. Copy the `ArcLengthEditor` folder (the one containing `ArcLengthEditor.py`) to Fusion's add-ins directory:
   - **Windows:** `%appdata%\Autodesk\Autodesk Fusion 360\API\AddIns`
   - **macOS:** `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns`

   Alternatively, keep the folder wherever you like and link it from Fusion: open **Scripts and Add-Ins**, click the green **+**, choose **Script or add-in from device**, and select the folder.
3. In Fusion press `Shift + S` (**Utilities → Add-Ins → Scripts and Add-Ins**), open the **Add-Ins** tab, select **ArcLengthEditor** and click **Run**. Tick **Run on Startup** to load it automatically.

The command **Set Arc Length** appears in **Sketch → Modify** and in **Solid → Modify**.

## Usage

1. Draw an arc in a sketch.
2. Start **Set Arc Length**.
3. Click the arc. The dialog shows its radius, included angle and arc length.
4. Enter the new arc length.
5. Pick which end point should move under **Move** and click **OK**.

The arc length must be greater than 0 and smaller than the full circle (2·π·r).

### The "Move" options

In Fusion a sketch arc always runs counterclockwise from its start point to its end point, so "start" and "end" may be the opposite of what you expect from how you drew it.

| Option | Behavior |
| --- | --- |
| Automatic | Moves the end point that is less connected to other geometry. If that fails, the other end point is tried, then the symmetric option. |
| End point moves, start point stays | Start point is kept in place. |
| Start point moves, end point stays | End point is kept in place. |
| Symmetric about the arc midpoint | Both end points move; the arc's bisector stays where it is. |

## How it works

The add-in reads the radius `r` and the length `b` of the arc and computes the new included angle `α = b / r`. It then moves the end point(s) with `SketchPoint.move()` to the matching position on the existing circle. Because the targets lie exactly on that circle, the sketch solver should not need to change anything else. After each attempt the add-in verifies length, radius and center point; if anything is off, the points are moved back and the next option is tried.

## Limitations

- Dimensions that fix the arc's angle or length (angular or arc-length dimensions), fixed end points, or tight constraints on both ends can prevent the change. The add-in then restores the arc and tells you so.
- Projected or otherwise referenced arcs cannot be modified.
- 3D sketches are not supported.
- Elliptical arcs and splines are not supported.

## Status

The calculation logic is covered by unit tests that run against a simulated sketch solver (see below). Behavior inside Fusion itself depends on your sketch's constraints, so please report anything unexpected together with a short description of the sketch.

## Development

The tests need no Fusion installation, only Python 3:

```
python tests/test_arc_length.py
```

The add-in itself uses the Fusion Python API (`adsk.core`, `adsk.fusion`). Relevant reference pages: [SketchArc](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchArc.htm), [SketchPoint.move](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SketchPoint_move.htm), [Selection Filters](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/SelectionFilters_UM.htm).

## License

MIT, see [LICENSE](LICENSE).

## AI disclosure 
The code of this plugin was written by Anthropic Claude Sonnet 5.5 and partially modified by the author.
