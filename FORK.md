# Fork: PC2 animated scenery authoring

A fork of [cobra-tools](https://github.com/OpenNaja/cobra-tools) adding the ability
to **write** Planet Coaster 2 motiongraphs, and to author the animation dropdown that
appears on animated scenery.

Based on upstream `c65ebddf7`. 17 commits, 98 files, +14625 lines.

> **Status: working, and verified in game — with the limits in "What is not done"
> below.** Published as a fork so it can be tried without waiting on a PR.

---

## What it adds

**Motiongraph writing.** Upstream cannot write a `.motiongraph` at all — its
`create()` raises `NotImplementedError`, because the round-trip dropped roughly a
third of the pointer relocations and produced a file the engine walked into wild
reads rather than an obviously broken one. Fixing that needed pointer aliasing
(shared DAG targets survive a write), array inference (a plain pointer can target an
array with no count field anywhere), and root-tail preservation.

**The animation dropdown.** Append entries to an animated prop's in-game animation
list — including clips the donor graph never knew about — from a spec exported out
of Blender, or from the command line.

**New format support:** `.motiongraphvars` and `.sceneryanimchoices` loaders,
`.datastreams` corrected for PC2 (its `DataStreamsSettings` is 56 bytes, not the
JWE1 48), and a fix for `add_files` silently dropping every `.datastreams` file.

**Generating a whole new asset, no donor.** `buildprop` (`modules/prop_builder.py`,
`modules/motiongraph_generator.py`) builds a scenery prop's `.ovl` from scratch: a
container CREATED rather than cloned, a motiongraph emitted from a clip list with
no retail file read at generation time, and audio/VFX events armed on those clips
from the same spec. This is a different, newer path from the donor-rename workflow
described below - see "Two ways to get an asset" for which one fits.

**Offline typo catching for events.** An audio event name or VFX `particle` name
is silent in game if it is wrong - no exception, no log line, nothing to grep for.
Both are now checked against mined catalogues (`constants/<game>/particles.py`,
`constants/<game>/audio_hashes.py` + its `.bin`) whenever `-g/--game` names a
measured game, in both `buildprop` and `animspec`. An unmeasured game skips the
check rather than rejecting - see `parse_events` in `modules/motiongraph_author.py`.

**Authoring effects in Blender, not a hand-typed Python list.** The "Cobra Asset
information" panel (Object Properties, closed by default - it is PC2 scenery
authoring specifically, not something every user needs) gained an Effects section:
one row per audio/VFX event on the selected animation choice, with a searchable
particle dropdown sourced from the same mined catalogue `buildprop` validates
against. WHEN an event fires is authored on the Action's own timeline via pose
markers (`fx.<row>.on` / `.off`) rather than a typed number, so the timing can
never drift from what is actually scrubbed and previewed. Export via File > Export
> Cobra Animation Spec now writes `spec_version: 2` with `weight` and `events` per
clip - both were already accepted by the reader (`parse_spec`) before the exporter
caught up to write them.

---

## Does it break anything?

**Not currently known — the table below that used to answer "no" is not reproducible,
and a real bug was found (and partly fixed) while trying to reproduce it.** This
section previously read "No" backed by a 506-asset preserved/changed/unreadable table
(upstream `2641/31/6`, fork `2665/13/0`). Re-running that same measurement
(2026-08-09) could not reproduce those numbers, and while investigating why, found a
genuine wild-pointer bug that had been present since the fork's first commit.

**What's confirmed fixed:** a stranded relocation (the pool it pointed at got
replaced during a rewrite) round-tripped through disk as a `-1` sentinel in two
fields with different signedness — `pool_index` (signed, reads back `-1`) and
`data_offset` (unsigned, reads back `4294967295`). The reload path indexed
`ovs.pools[-1]` with no guard against `-1` (an equivalent guard already existed four
lines above it, for a sibling table, and was simply missing here) — Python silently
returns the *last* pool instead of raising, so the pointer resolved to a real but
unrelated pool at a garbage offset instead of being dropped. Fixed in
`source/formats/ovl/__init__.py` (+ regenerated `generated/`); full local test suite
unaffected (594 passed). Details: `.scratch/byte-identical-ovl-writer/issues/45-fragments-reload-unguarded-minus-one.md`
in the workspace root.

**What's confirmed still broken:** fixing the above stopped the wild pointer, but
exposed a second, separate, currently-open issue — `stream_info` (a `LookupPointer`,
used by `.ms2` mesh data) can pick the wrong, or a silently-defaulted, buffer index
after a round trip, because it identifies its target by comparing raw in-memory read
positions across two independent parses rather than anything stable like a name. A
100-asset local sample (fork tip, post-fix) showed a 9.7% CHANGED rate (57 of 587
entries, all `.ms2`) — roughly 8x the rate upstream itself shows on its own full
corpus (31 of 2672 ≈ 1.2%, remeasured the same session). That gap is this issue, not
noise. Not yet fixed: `.scratch/byte-identical-ovl-writer/issues/46-stream-info-lookup-index-mismatch.md`.

**What this means for the old table's other claims:** the "18 files fixed, 6
previously-unreadable now load" part cannot currently be re-confirmed either way — no
full 506-asset run with the ticket-45 fix applied has been completed yet, only the
100-asset local sample above. The `.ms2`/`.fgm` pre-existing-upstream-failure
characterisation two paragraphs below is unrelated to either issue found here and is
believed still accurate, but hasn't been re-verified alongside this.

**What's unaffected:** the 2026-08-09 motiongraph test-coverage pass (14 fixes,
`append_clips`/`apply_clip_prefix`/root-tail write) is unrelated to both issues above —
bisection confirmed the wild-pointer bug predates that pass entirely (present already
in the fork's very first commit), and neither issue touches authoring-only code paths.
The 24-asset before/after sample for that pass (byte-for-byte identical both times)
still stands on its own terms.

The `.ms2`/`.fgm` pre-existing-upstream failures (4 animatronic `.fgm`, 9 `.ms2`) are
characterised but not fixed; see `docs/animated-scenery.md` §13 in the companion repo
for why a `.ms2` fix needs a carrier through the standalone format.

**Next step before this section can honestly say anything stronger than "unknown":** a
fresh, full 506-asset upstream-vs-fork run, both arms on the ticket-45-fixed code, same
script, same machine — matching the rigor the original table claimed but that could not
be reproduced this session.

---

## Verified in game

Planet Coaster 2 build 1.10.3, custom asset built end to end from a donor:

- the prop places, renders and **animates**
- the dropdown shows **8 entries in the right order** — 5 donor rows at indices 0-4,
  3 appended at 5-7
- entries display **text**, not raw `[InfoPanel_...]` symbols, so the game resolves
  newly shipped localisation files
- all three playback modes work: continuous loop, "play once every N seconds", and
  the sequence trigger
- the built asset contains **zero donor naming** — verified by scanning every entry
  plus the graph, vars, choices, manis and enumnamer. That was achieved with a
  second `rename` pass for the donor's differently-named sub-resources; see "Two
  things to check afterwards"

**This run predates the 2026-08-09 fix pass**, and it exercised exactly the code
that pass changed: the 3 appended entries went through `append_clips`, and building
the asset went through `apply_clip_prefix`. Four real bugs in those two functions
were found and fixed after this verification ran (id/ref handling on a cloned
state, a duplicate-name check that only compared against the asset's past entries
not the current batch, a defaulted enum name skipping a charset check, and
`apply_clip_prefix` corrupting an unrelated loader name when one clip's name was a
substring of another's) - none reachable from this specific asset, or this
verification would have shown it, but the coverage story for this section was
weaker than it read.

**Re-verified 2026-08-09, after the fix pass, on a second asset (`AT_Medusa` in the
companion `mod_animtest` repo), append_clips specifically:** rebuilt, redeployed,
selected and placed with no crash, dropdown carries all 9 entries (5 original + 4
newly appended) with correct authored label text, both a looping and a one-shot
entry over the same clip play correctly, and a clip's duration in the timeline
matches its true `.manis` length. This confirms `append_clips`' fixes on real data
end to end. **`apply_clip_prefix` (F3) was not re-verified in game** - `AT_Medusa`'s
build script calls `append_clips` directly and does not go through it, and no
existing real-data workflow does either. It still rests on synthetic tests only;
treat the "zero donor naming" bullet above as unconfirmed against the current code
until it is.

## Tested against the whole corpus

`cleanasset` was run over **every one of the 506 PC2 scenery assets**, checking the
*result* rather than the exit code — the cleaned asset still loads, its vars
reference resolves to a real entry, no audio/VFX events survive, and every choice
label is one of our own loc symbols:

```
ok        49   cleaned and verified
refused   14   multi-prop, correctly declined
skip     443   no motiongraph
FAIL       0
```

The 14 refusals are assets holding several props — `PC_Targets` has 9 graphs,
`SC_Spider` 5, `AQ_SubParts` 4 — where cleaning needs a name per prop and guessing
would be worse than declining. Repeatable via `test_cleanasset_corpus.py`.

Worth stating plainly: an earlier version of that same command passed three
hand-picked assets and failed **49 of 63** on the full corpus. Broad testing is the
only reason this is trustworthy.

---

## What is not done

Nothing event-related is left unsupported. Both paths arm audio/VFX events from
the same spec format now: `buildprop` generates them into a from-scratch graph,
and `animspec` arms them on a clip appended to an *existing* graph - a State
cloned from a donor still has its inherited events stripped first (otherwise the
donor's sounds would fire on your clip's timing), then the spec's own events fill
the empty list. Neither needs a retail curve to clone; both use the same measured
step shape (see "Two ways to get an asset").

**Event catalogue coverage.** The particle list is exhaustive (every
`.particleeffect` in Content0, the game's only particle carrier). The audio-event
hash set is scoped to `*_Events` bank OVLs specifically - a real event living
outside that scope would false-negative (rejected as unknown when it is not).
Measured against three known-good, in-game-verified names with zero misses; not
proven exhaustive beyond that scope.

**The `.ms2` block tail.** 9 corpus files lose roughly half their blocks on
round-trip. Pre-existing upstream, characterised, not fixed.

**Byte-exactness on the richer `datastreamsonly.motiongraph` variants.** The four
larger variants of this stub file (FT_Jester, FT_Knight, PR_Decorations,
PR_Pirates) now round-trip with every block and fragment preserved and all
content identical, except that one all-zero block is written as 32 bytes where
retail has 24. Nothing references the extra zeros; the corpus gate's semantic
measure passes all four. Noted for completeness, not a known breakage.

**In-game testing is narrow.** One asset, one park, one game build. The corpus gate
is broad; the in-game evidence is not.

**`apply_clip_prefix` has no real-data verification, only synthetic tests.** The
2026-08-09 fix pass corrected a real bug in it (an unanchored rename corrupting an
unrelated loader's name when one clip's name was a substring of another's,
`bend.mani`/`bendy.mani`-shaped), verified against the actual rename mechanism in
unit tests, but no existing build script in the companion `mod_animtest` repo
calls this function, so it has not been exercised on a real asset or in game since.
`append_clips`, which the same pass also fixed, HAS been re-verified this way (see
"Verified in game" above) - `apply_clip_prefix` is the one specific gap left.

---

## Two ways to get an asset

**Generate one with `buildprop`** if you are starting a new prop from scratch. It
creates the container, the motiongraph and its events from a Blender-exported
animation spec, with no donor at all:

```bash
python ovl_tool_cmd.py buildprop YourAsset -g "Planet Coaster 2" \
    --art ./art --dest ./out --vars-ref SomeVarsEntry
```

`--art` holds your exported `.ms2`/`.manis`, any `.fgm`/textures, and
`<yourasset>.animspec.json` (Blender: *File → Export → Animation Spec*, v2 adds
per-clip `weight` and `events`). `--vars-ref` names a `.motiongraphvars` entry your
art folder supplies - it is not a free choice, see the module docstring. The build
prints exactly what your mod must still provide: loc text for each choice, and for
any armed VFX event, the prefab child it expects to find.

**Rename a donor** (below) if you want to edit an *existing* graph - append a state
to a shipped asset, or reuse one wholesale under a new name. This path predates
`buildprop` and is more hands-on, but `animspec` arms events on an appended clip
the same way `buildprop` does - the v2 spec's per-clip `events` list.

### Why the rename workflow starts from a donor

Upstream's motiongraph writer lost roughly a third of the pointer relocations on
an extract-to-XML round-trip, producing a file the engine walks into wild reads
rather than an obviously broken one. That is why upstream's `create()` refuses
outright. **This fork fixed that**: the round-trip measures 100/100 semantic
across the corpus (re-measured 2026-07-31 on this branch), and a rebuilt graph
(including one with added branches) is confirmed loading and animating in-game -
the same `append_clips` verification as "Verified in game" above, both the
pre-fix run and the 2026-08-09 re-verification after it.

The flow still starts from a donor because **renaming is exact by construction**:
it edits strings in the existing pools and re-points fragments, so nothing is
rebuilt and the graph is preserved bit-for-bit. Measured on one donor: 250 blocks
/ 521 fragments before and after, identical. A rebuild is semantically verified;
a rename never even raises the question. When the goal is a new asset rather than
editing graph internals, there is no reason to spend the risk.

### In Blender, one session

1. **Model, rig, animate.** Name each Action for the **clip alone** — `Wobble`,
   `Bend`. No `$`, no asset name; the build adds those.
2. **Author the dropdown.** Object Properties → *Cobra Asset information*. One row
   per entry: pick the Action, type a label, tick loop. **Append-only** — placed
   props store the index, so never reorder or delete a shipped row.
3. **Export three things:** `File → Export →` Cobra MS2, Cobra Manis, and Cobra
   Animation Spec (`.json`).

The spec carries only **intent** — which Action, what label, loops or not. Clip
durations and qualified names come from the built asset later, so exporting it early
is fine.

### On the command line

```bash
# 4. retarget a retail donor to your asset name (entries AND references inside files)
python ovl_tool_cmd.py rename Donor.ovl -g "Planet Coaster 2" \
    --from MY_DonorAsset --to YourAsset -o YourAsset.ovl

# 5. inject your art, dropping the donor's animations
python ovl_tool_cmd.py inject YourAsset.ovl -g "Planet Coaster 2" \
    -f art/YourAsset.ms2 -f art/YourAsset.manis --replace-clips --in-place

# 6. remove the donor's residue (events, vars wiring, its loc symbols)
python ovl_tool_cmd.py cleanasset YourAsset.ovl -g "Planet Coaster 2" \
    --loc-dir ./loc --in-place

# 7. append the dropdown entries from the Blender spec
python ovl_tool_cmd.py animspec YourAsset.ovl -g "Planet Coaster 2" \
    -s spec.json --loc-dir ./loc --in-place

# 8. point your prefab at the asset (or --check it in a build gate)
python ovl_tool_cmd.py syncprefab prefab_yourasset.lua \
    -a YourAsset.ovl -g "Planet Coaster 2"

# 9. pack the mod: loc .txt files + your prefab .lua
python ovl_tool_cmd.py new -g "Planet Coaster 2" -i ./main_files -o Main.ovl -c ZLIB
```

Then copy `YourAsset.ovl` and `Main.ovl` into `…/Win64/ovldata/<YourMod>/`, restart
the game, and place the prop.

### The one ordering rule

**`animspec` must run after the art is injected.** It reads clip durations out of the
asset's own `.manis` and refuses if a clip is not there — durations are never taken
from anything typed. Everything else can be reordered.

### Why each step exists

| step | without it |
|---|---|
| `rename` | a motiongraph authored from scratch loses ~⅓ of its relocations |
| `--replace-clips` | a `.manis` replaces only under the entry name the asset already uses — an opaque donor hash. Under any other name **both** sets survive and the asset ships the donor's animations |
| `cleanasset` | the asset keeps the donor's audio events, its `motiongraphvars` wiring, and loc symbols that only work by borrowing retail's translations |
| `syncprefab` | `MotionGraphName` resolves by basename; a mismatch is silent in the tool **and** the game — the prop places and never animates |

### Two things to check afterwards

**`rename` is substring-based**, so a sub-resource whose name is not literally the
asset name survives untouched. This is not rare: `AQ_SingingEels` names its clips
`AQSingingEels$Idle01` — no underscore — so renaming `AQ_SingingEels → ZZ_DocDemo`
leaves every clip carrying the donor's name.

**This is cosmetic, not breakage.** Those clips and the graph's references to them
agree with each other, so the asset works; it just ships someone else's name. Run it
again for each distinct sub-name the command reports as left over:

```bash
python ovl_tool_cmd.py rename YourAsset.ovl -g "Planet Coaster 2" \
    --from AQSingingEels --to ZZDocDemo --in-place
```

Case does not matter. OVL entry names are lowercased while references *inside* files
keep display case, so a single spelling can only match one of the two — `rename`
therefore looks the other spelling up in the graph and renames both. Before that, a
lowercase `--from` renamed the 9 entries and left all 9 clip references pointing at
the old name, producing a **silently broken** asset.

It also refuses to write anything if any clip reference would be left dangling,
naming the spelling to use. A rename either completes or changes nothing.

**`syncprefab` leaves `ModelName` alone on a multi-model asset**, because retail
assets hold many `.mdl2` and picking one would be a guess. Set it yourself, and
`--check` will confirm the rest.

### Names are generated, not typed

You author the Action name, a label, and loop yes/no. The build generates the
qualified clip name, the enum name, the localisation symbol, and the duration. Any
prefix typed by hand is **discarded rather than trusted**, so `Wobble`,
`YourAsset$Wobble` and `StaleOldName$Wobble` all produce the same correct result.

---

## Relationship to upstream

Intended to be split into two pull requests once it has had some use:

1. **General OVL fixes** — the first commit on this branch, applies cleanly to
   `origin/master` on its own, fixes 18 retail files with no motiongraph code
   involved
2. **Motiongraph support** — everything after it, including the 2026-08-09 test
   and fix pass, which is entirely inside the motiongraph/authoring domain

Both are validated independently; the split is deferred until real feedback exists.

Bug reports welcome, particularly from anyone building an animated prop that is not
the one this was developed against.
