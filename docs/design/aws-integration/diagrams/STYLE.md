# Diagram style

Every rule below is read off the eight SVGs that sit next to this file. They were
drawn one at a time, and the shared conventions were never written down — so this
note records what the set already does, for the next diagram that joins it.

This is the canonical list of the set:

| File | What it illustrates |
| --- | --- |
| `constraint-map.svg` | the two graph representations, and the absence of a path between them |
| `deployment-topology.svg` | the one managed component between the client and the container |
| `options-considered.svg` | indicative monthly cost of the hosting options weighed |
| `persistence-lifecycle.svg` | the three moments at which state is captured |
| `snapshot-context.svg` | who changes the graph, and where its only durable copy lives |
| `snapshot-containers.svg` | the two entry points, the in-memory graph, and the record |
| `snapshot-components.svg` | the components inside the subsystem, and the single announcement slot |
| `snapshot-dataflow.svg` | a change reaching the record, and the next boot reading it back |

The first four are embedded by the hosting README in the parent directory; the four
`snapshot-*` files are the C4 levels and the runtime view in `../snapshot-persistence.md`.
Add a row here when you add a diagram.

---

## The canvas

The root element carries the font stack and nothing else that a renderer has to
interpret:

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 940 470" width="940" height="470" font-family="Helvetica, Arial, sans-serif">
  <rect width="940" height="470" fill="#fbfbfd"/>
```

- **Width is always 940.** Height is whatever the content needs — the set runs 470,
  532, 540, 560, 566, 604, 716. `viewBox`, `width` and `height` agree.
- **The ground rect comes first**, `#fbfbfd`, full canvas, no stroke. It is a shade
  off white so the white content boxes read as objects sitting on a surface.
- **Font stack is inherited from the root**, declared once, never repeated on a
  child. Sizes run 10 to 13.5 for content, weight `600` for anything emphasised and
  `700` for the two or three loudest band labels.
- **Title** at `y="34"` (`36` where the subtitle needs a little more air),
  `font-size="19"`, `font-weight="600"`, `fill="#1a202c"`, `text-anchor="middle"` on
  `x="470"`. A chart whose plot area starts at the left edge left-anchors it
  instead at `x="24"`, `font-size="18"`.
- **Subtitle** at `y="56"` (or `58`), `font-size="13"` — `12` when left-anchored —
  `fill="#4a5568"`, no bold, no terminal period. It states the claim the picture
  makes, in a sentence a reader can carry away: *"State is captured at three
  moments; between them it exists only in memory."*

---

## Palette by role

### The common core

Twelve colours appear in six or more of the eight diagrams. Reach for these
freely — they are the house palette.

**Ink greys — text weight and neutral structure.** Each step down the scale is a
step further from the reader's eye:

| Colour | Role |
| --- | --- |
| `#1a202c` | titles and the primary label inside a box |
| `#4a5568` | subtitles, secondary lines under a label, neutral box strokes |
| `#718096` | captions, ordinals, column headers, neutral connectors |
| `#ffffff` | box fill — the default for anything that is a thing |
| `#fbfbfd` | the ground |

**Blue `#2b6cb0`, tint `#ebf4ff` — what ships.** The deployed path, the primary
flow, the component that exists today. Blue is the spine of a diagram: the write
path, the request path, the sequential foundation.

**Green `#2f855a`, tint `#f0fff4` — what is durable or proven.** The datastore, the
restored copy, the library capability, a verified claim. Green is the answer to
"and then it survives".

**Amber `#b7791f`, tint `#fffaf0`, text `#744210` — the accepted limitation.**
Not a warning of a mistake: the cost you have chosen and can name. In-memory state
that dies with the process, the components deliberately absent from the topology,
a caveat riding alongside an arrow.

### Extras from a single diagram

The remaining twelve colours belong to the four diagrams the hosting README
embeds. They are legitimate — extend them if you are drawing the same kind of
picture, and stay in the core otherwise.

**Red `#c53030`, tints `#fff5f5` / `#fed7d7`, text `#742a2a`** marks the loss
boundary: the dashed barrier between two graph representations, the unprotected
window between snapshots, the expensive end of a cost axis. Red appears in three
diagrams and always means *this is where something is lost*. It never decorates.

**Extra greys** widen the scale where a diagram needs more than three steps:
`#cbd5e0` for the outline of an outer band or a chart axis, `#a0aec0` for the
faintest connector and de-emphasised outlines, `#e2e8f0` for chart gridlines,
`#f7fafc` for a box that is present but inert, `#2d3748` for a paragraph of body
text inside a note band.

**Chart fills** `#fed7d7` / `#feebc8` / `#9ae6b4` with dark labels `#742a2a` /
`#744210` / `#22543d` are the bar ramp in the cost comparison — red at the
expensive end, green on the baseline.

Every colour named above is used by at least one diagram in the set — this list is
read off the files, so a colour that stops appearing anywhere should leave the page
with the diagram that used it.

---

## Shapes

- **Content box:** `rx="6"` is the workhorse; `4` to `8` covers the range. Stroke
  `1.5`–`1.8`, dropping to `1.2`–`1.6` for a box nested inside another.
- **Band or group container:** `rx="9"`–`12`, stroke `1.5`, spanning nearly the full
  canvas (`x="24"`, `width="892"`, or `x="28"`, `width="884"`).
- **Pill:** `rx="16"`, used once, for a label riding on top of the barrier line.
- **Dashed grouping boundary:** `rx="12"`, `fill="#ffffff"`, stroke in blue or green
  at `stroke-width="2.2"`, `stroke-dasharray="7 5"`. Its label sits inside the top
  edge, 24px below it, in caps, in the boundary's own colour:

```xml
<rect x="20" y="96" width="536" height="424" rx="12" fill="#ffffff" stroke="#2b6cb0" stroke-width="2.2" stroke-dasharray="7 5"/>
<text x="34" y="120" font-size="11.5" font-weight="600" fill="#2b6cb0" letter-spacing="0.5">THE ANNOUNCEMENT CHAIN</text>
<text x="34" y="136" font-size="10.5" fill="#718096">read top to bottom, the order the announcement travels</text>
```

  The caps label names the group; the lowercase gloss underneath tells the reader
  how to read it. Caps labels always carry `letter-spacing="0.5"` or `0.6`.

- **Callout band at the foot:** the idiom for the one thing the geometry cannot
  show. A full-width tinted rect, a caps label in the dark tone of that tint, then
  one to three body lines at `12.5`–`13`. The topology diagram uses it for
  `DELIBERATELY ABSENT`; the lifecycle diagram stacks two, `THE UNPROTECTED WINDOW`
  in red and `WHAT SETS THE FLOOR ON THE INTERVAL` in grey. Put the sentence you
  most want remembered on the last line, in bold, in the stroke colour.
- **Barrier:** a dashed rule across the whole canvas, `stroke-dasharray="10 7"` at
  `stroke-width="2.5"` in red, with a pill label at its midpoint. One diagram needs
  this; it is what "no path between them" looks like.

---

## Arrows and markers

Edges are `<path d="M x y L x y">` with `fill="none"`, `stroke-width` `1.6`–`2`, and
a `marker-end`. There is no `marker-start` anywhere in the set — direction is
carried by the single head. An arrow takes the colour of the role it serves, so a
blue box feeding a green store draws the blue arrow.

Dash a connector when the edge is conditional, deferred, or a notification rather
than a data move: `stroke-dasharray="5 4"` for a full-size edge, `"4 3"` for a thin
one.

Markers live in a `<defs>` block at the **end** of the file, after the artwork:

```xml
<defs>
  <marker id="deployment-topology-blue" markerWidth="10" markerHeight="10" refX="9" refY="3.5" orient="auto"><path d="M0,0 L0,7 L9,3.5 z" fill="#2b6cb0"/></marker>
</defs>
```

**Marker ids are namespaced with the diagram's own filename stem** —
`snapshot-dataflow-green`, `constraint-map-amber`, `persistence-lifecycle-ink`.
This matters: a docs site that inlines two SVGs into one HTML page puts both
`<defs>` in the same id space, and a bare `id="blue"` in the second diagram loses
to the first, so every arrow in one of them silently takes the wrong colour. The
suffix is the role, not the hex: `-blue`, `-green`, `-grey`, `-amber`, `-ink`. Give
each diagram only the markers it actually references.

One head geometry per diagram, matched to its stroke weights — `10/10, refX 9,
refY 3.5, M0,0 L0,7 L9,3.5 z` for most, or the slightly smaller `9/9, refX 8,
refY 3, M0,0 L0,6 L8,3 z`.

---

## Plain shapes and text, everywhere

Every diagram in this set is built from `<rect>`, `<line>`, `<path>`, `<text>` and
one `<marker>` per arrow colour. Presentation is spelled out as attributes on each
element, the font stack resolves to something already installed on any machine, and
no element depends on an HTML layout engine, an external file, or a network fetch.

That is what makes the rendering identical in GitHub's markdown viewer, in Mintlify,
and in an editor preview — the property that lets these files be committed once and
embedded anywhere. It holds because the whole set stays inside that small
vocabulary, and it is worth keeping: text is real text, so it is searchable,
selectable, diffable in review, and legible when someone rasterises the file at 3x
for a slide.

---

## Fitting a label

Overflowing text was the single most common defect found reviewing this set — a
label that runs past its box edge, or a second line that collides with the
connector beneath it. Budget the width before you place the text.

For Helvetica at `font-size` F, mixed-case sentence text averages **0.43 F** per
character; caps with `letter-spacing="0.5"` average **0.6 F**:

| font-size | mixed-case | caps |
| --- | --- | --- |
| 10 | 4.3 px/char | 6.0 px/char |
| 11 | 4.7 | 6.6 |
| 12 | 5.2 | 7.2 |
| 13 | 5.6 | 7.8 |
| 19 (title) | 8.2 | — |

So a 244px box with 20px of padding on each side holds about 36 characters at
`font-size="13"`, or 24 characters of caps at `12`. These are averages: a line of
capitals, digits, or `W`s runs wider, so leave a few characters of headroom on
anything near the limit.

When a label does not fit, **split it onto another `<text>` line** at 20–22px of
leading, rather than shrinking the font below 10 or letting it run past the edge.
Two stacked lines at the same size read better than one line at 8.5, and the box
can always grow.

---

## Checking your work

Rasterise the file and open the PNG:

```bash
./venv/bin/python -m cairosvg docs/design/aws-integration/diagrams/your-diagram.svg -o /tmp/your-diagram.png -s 2
```

`cairosvg` fails loudly on malformed markup, so a clean run already tells you the
file parses and every `marker-end` resolves. Then **look at the image**. A script
that checks box geometry will happily pass a diagram whose label is struck through
by a connector, whose two arrows overlap into one ambiguous line, or whose caps
band label has run off the end of its band — all three were found by eye, not by
arithmetic, and none of them are visible in the source.

Render at `-s 2` and read it at 50%: that is roughly how it lands on a docs page,
and it is where a too-small caption stops being legible.
