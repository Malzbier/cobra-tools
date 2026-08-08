"""Generate a complete .motiongraph (and its enumnamer) from a choice spec.

The state-machine emitter for ONE motiongraph family: a dropdown of
selectable looping clips with sequencer support ("anim_choices"). Retail
motiongraphs come in structurally different families - doors, platforms,
sequence-chain animatronics - whose trees differ in shape, not parameters; a
clip list cannot express those, so they are refused at the spec layer rather
than mis-generated here. From a clip list this emits the full MRF decision
tree, states with their transition tables, the lua_results the tree indexes,
and the constant sections - no retail input at generation time. The
structural knowledge below was measured across the full retail scenery
corpus and verified in game.

API:
    from modules.motiongraph_generator import generate
    graph_xml, enumnamer_xml_str = generate(
        asset="MyProp",
        choices=[("Default", None, None, "Auto"),
                 ("Static", "Rest", 1, "Rest"),
                 ("Idle01", "MyClip", 3, "MyClip")],
        vars_ref="MyPropVars",
        blend_time=0.15)

Choice tuples are (enum_name, clip, weight, label); label is unused here (it
belongs to the choice table / loc layer) but accepted so one spec row can
drive every artefact. Choice 0 must be the Auto/random choice (clip None);
the first clip-bearing choice is the REST state - deactivate blends to it.

Emission invariants (measured, not designed, and confirmed in game rather than
assumed): header count_0/count_1 and first_non_transition_state are corpus
constants; ptr_0 raw="N,0" indexes lua_results t[N] on dtype=7 nodes and refs
a structure on dtype=8 leaves; every EndDecisionScope closes one construct and
back-references it through a count_1="7" child; blend tables are the fixed
100/10/1000 tier trio; the multi-trigger play slot binds
Trigger="SequenceTrigger"; strings and shared blocks intern by first use.
"""

import re

from modules.motiongraph_author import CURVE_TYPE_FOR

# Identifiers interpolated into the emitted XML; matches the spec layer's rule
# (motiongraph_author._SAFE_NAME) so the two boundaries cannot drift apart in
# what they accept
_SAFE_IDENT = re.compile(r"\A[A-Za-z0-9_]+\Z")

# Set by generate() before the emitters run; the emitters reference these as
# module globals so their bodies stay byte-comparable with the proven
# mod-side originals they were migrated from
ASSET = None
VARS_REF = None
BLEND_TIME = None
CHOICES = None
GAME = None


class Ids:
    """id/ref allocator: first mention defines, later mentions reference."""

    def __init__(self):
        self.n = 0
        self.seen = {}

    def define(self, key):
        # a raise, not an assert: a duplicate definition would emit two blocks
        # under one id and corrupt every ref to it, and asserts vanish under -O
        if key in self.seen:
            raise ValueError(f"id for {key!r} defined twice")
        self.n += 1
        self.seen[key] = self.n
        return f'id="{self.n}"'

    def ref(self, key):
        return f'ref="{self.seen[key]}"'

    def has(self, key):
        return key in self.seen


def lua_results(n_choices, weights):
    """t[1..5]: the parameter tables the tree's raw="N,0" fields index."""
    parts = ["local t = {}; "]
    parts.append('t[1] = { ResultParams = { [1] = {  }, [2] = {  },  }, '
                 'VariableName = "IsSequenceTriggerControlled",  }; ')
    parts.append("t[2] = { ResultParams = { [1] = {  },  },  }; ")
    parts.append('t[3] = { ResultParams = { [1] = {  }, [2] = { '
                 'Trigger = "SequenceTrigger",  },  },  }; ')
    rp = ", ".join(f"[{i + 1}] = {{  }}" for i in range(n_choices))
    parts.append(f't[4] = {{ ResultParams = {{ {rp},  }}, '
                 f'VariableName = "LoopAnimSelection",  }}; ')
    wp = ", ".join(f"[{i + 1}] = {{ Weight = {w},  }}" for i, w in enumerate(weights))
    parts.append(f"t[5] = {{ AvoidRepeats = true, ResultParams = {{ {wp},  }},  }}; ")
    parts.append("return t; ")
    return "".join(parts)


def enumnamer_xml(choices):
    rows = "\n".join(f"\t\t<pointer>{e}</pointer>" for e, _, _, _ in choices)
    return (f'<EnumnamerRoot game="{GAME}">\n'
            f'\t<strings pool_type="4">\n{rows}\n\t</strings>\n</EnumnamerRoot>\n')


def motiongraphvars_xml(game="Planet Coaster 2"):
    """The vars resource m_g_two names, for THIS family.

    Nothing here varies: the anim_choices family is the selectable-looping-clip
    dropdown, and the dropdown IS LoopAnimSelection - one enum variable, the
    same declaration in every retail example of the family. Other families
    declare other variables (DoorState, the trigger flags), but a clip list
    cannot express those and the spec layer refuses them, so there is no case
    where this content depends on the author's input.

    var_name and enum_name deliberately reference ONE string (id/ref): the
    file is a DAG, and MotiongraphvarsLoader.collect relies on that identity
    rather than merging by value - emitting the name twice writes two blocks
    and puts the block count out by one.

    Takes `game` as a plain argument rather than reading the module global the
    migrated emitters share, so it can be called without generate() having run
    first - the caller only needs it when the art supplies no vars file.
    """
    return (f'<MotiongraphVarsRoot count="1" game="{game}">\n'
            '\t<vars pool_type="4">\n'
            '\t\t<motiongraphvarref>\n'
            '\t\t\t<var pool_type="4" kind="3" zero_16="0" one_f="1.0" ten="10" '
            'default_value="-1" zero_36="0">\n'
            '\t\t\t\t<var_name id="1">LoopAnimSelection</var_name>\n'
            '\t\t\t\t<enum_name ref="1">LoopAnimSelection</enum_name>\n'
            '\t\t\t</var>\n'
            '\t\t</motiongraphvarref>\n'
            '\t</vars>\n</MotiongraphVarsRoot>\n')


def fixed_sections(ids):
    """m_g_two, lua_modules, lua_results, first_non_transition_state."""
    weights = [w for _, _, w, _ in CHOICES if w is not None]
    return (
        '\t<m_g_two pool_type="3" count="2">\n'
        '\t\t<ptr pool_type="3">\n'
        '\t\t\t<nestedzstr>\n\t\t\t\t<bc>\n\t\t\t\t\t<ab>TriggerSequence</ab>\n'
        '\t\t\t\t</bc>\n\t\t\t</nestedzstr>\n'
        '\t\t\t<nestedzstr>\n\t\t\t\t<bc>\n\t\t\t\t\t<ab>' + VARS_REF + '</ab>\n'
        '\t\t\t\t</bc>\n\t\t\t</nestedzstr>\n'
        '\t\t</ptr>\n\t</m_g_two>\n'
        '\t<lua_modules pool_type="3">\n'
        '\t\t<motion_graph>MotionGraph</motion_graph>\n'
        '\t\t<motion_graph_event_handling>MotionGraphEventHandling'
        '</motion_graph_event_handling>\n'
        '\t\t<motion_graph_actions>MotionGraphActions</motion_graph_actions>\n'
        '\t</lua_modules>\n'
        f'\t<lua_results>{lua_results(len(CHOICES), weights)}</lua_results>\n'
        '\t<first_non_transition_state curve_length="-1.0" count_3_c="0" '
        'count_6_a="1" count_6_b="-1" count_6_c="0" />\n')


# The event curve is a STEP, and it is fully specified by these two levels
# plus where the step sits. Measured across every armed, in-game-verified
# entry: the curve always spans the whole clip (x 0.0 -> 1.0), y is 16384
# before the trigger and 16448 from the trigger onward, and both
# subsequent_curve_param fields are 16384. Audio and VFX entries are
# identical in shape - only curve_type, location and the trigger differ - so
# nothing here needs a retail curve to clone
_Y_OFF, _Y_ON, _SCP = 16384, 16448, 16384


def _curve_point(x, y):
    return (f'<curvedatapoint x="{x}" y="{y}" '
            f'sub_curve_type="SubCurveType.CONSTANT" '
            f'subsequent_curve_param="{_SCP}" '
            f'subsequent_curve_param_b="{_SCP}" />')


def datastreams_xml(ids, events, sync_id):
    """<additional_data_streams> for one state's events, or the empty tag.

    bone_i_d refs the state's own sync_prop_through_variable - the same id
    every armed retail-shaped entry points at - rather than naming a bone:
    placement comes from `location` for audio and from the named prefab child
    for VFX.
    """
    if not events:
        return '<additional_data_streams />\n'
    entries = []
    for e in events:
        pts = "\n".join((_curve_point(repr(0.0), _Y_OFF),
                         _curve_point(repr(float(e["at"])), _Y_ON),
                         _curve_point(repr(1.0), _Y_ON)))
        entries.append(
            f'<datastreamresourcedata curve_type="{CURVE_TYPE_FOR[e["type"]]}">\n'
            f'<ds_name>{e["name"]}</ds_name>\n'
            f'<type>{e["type"]}</type>\n'
            f'<bone_i_d ref="{sync_id}" />\n'
            f'<location>{e["location"]}</location>\n'
            f'<curve count="3">\n<points pool_type="3">\n{pts}\n'
            f'</points>\n</curve>\n</datastreamresourcedata>\n')
    return ('<additional_data_streams>\n'
            '<data_stream_resource_data pool_type="3">\n'
            + "".join(entries)
            + '</data_stream_resource_data>\n</additional_data_streams>\n')


def activity_xml(ids, choice_i, clip, events=(), flags=17):
    """One AnimationActivity per CHOICE, not per clip: two dropdown entries
    may legitimately share one clip (a looping and a one-shot entry over the
    same animation), so the sync-variable id is keyed by the choice."""
    dt = ids.ref("data_type") if ids.has("data_type") else ids.define("data_type")
    sync = ids.define(("sync", choice_i))
    sync_id = ids.seen[("sync", choice_i)]
    return (
        f'<activityreference>\n<activity>\n'
        f'<data_type {dt}>AnimationActivity</data_type>\n'
        f'<data pool_type="3" animation_flags="{flags}" priorities="0" '
        f'starting_prop_through="0.0" lead_out_time="0.0" count_6="0">\n'
        f'<mani>{ASSET}${clip}</mani>\n'
        f'<weight float="1.0" />\n<speed float="1.0" />\n'
        f'<sync_prop_through_variable {sync} />\n'
        f'<output_prop_through_variable ref="{sync_id}" />\n'
        f'{datastreams_xml(ids, events, sync_id)}'
        f'</data>\n</activity>\n</activityreference>\n')


def state_xml(ids, choice_i, all_choices, defined):
    """A state WITH its transition structure, defined at first mention.

    Retail states are not a flat list: each carries array_2/transstruct holding
    (a) another_mrf_reference_2 rows - one per OTHER state, the transitions out -
    and (b) a nested states list naming those targets, defined DEPTH-FIRST at
    first appearance and referenced thereafter (state A defines state B inside
    its own list, B defines C, and so on). BlendToState's ptr_0 refs the
    TARGET state's rows. A flat list without this loads fine in game but
    drives no animation and no attached effects.
    """
    key = ("state", choice_i)
    if key in ids.seen:
        return f'<statereference>\n<state {ids.ref(key)} />\n</statereference>\n'
    defined.add(choice_i)
    sid = ids.define(key)
    clip, events = None, ()
    for i, (_, c, _, ev) in enumerate(CHOICES):
        if i == choice_i:
            clip, events = c, ev
    others = [j for j in all_choices if j != choice_i]
    # THE FIXED TIER TRIO. Three sightings agree - a retail graph's state
    # tables, its mt inline table, and a stub graph's unmodelled tail (which
    # has NO states at all) all carry exactly 3 rows at 100/10/1000. A
    # one-row-per-other-state reading only looked right where N-1 happened to
    # equal 3; a 1-row inline table crashed the game on park load
    rows = "\n".join(
        f'<mrfmember2 curve_length="0.0" count_3_c="0" count_6_a="0" '
        f'count_6_b="{b}" count_6_c="0" num_activities="65536" />'
        for b in (100, 10, 1000))
    nested = "".join(state_xml(ids, j, all_choices, defined) for j in others)
    return (
        f'<statereference>\n<state unk="0" {sid}>\n'
        f'<activities pool_type="3">\n'
        f'{activity_xml(ids, choice_i, clip, events)}</activities>\n'
        f'<array_2 pool_type="3">\n<transstruct>\n'
        f'<another_mrf_reference_2 multi="3" '
        f'{ids.define(("amr2", choice_i))}>\n{rows}\n</another_mrf_reference_2>\n'
        f'<states>\n<states pool_type="3">\n{nested}</states>\n</states>\n'
        f'</transstruct>\n</array_2>\n'
        f'</state>\n</statereference>\n')


# --- the tree ----------------------------------------------------------------
# Interned strings and the shared empty-vars block define once (first EDS) and
# ref thereafter. Every wrapper is (opening member, closing EDS), and the
# EDS's child is the back-edge to the wrapper, carrying a ptr_1 to the shared
# block

def _method(ids, name):
    key = ("lua", name)
    if ids.has(key):
        return f'<lua_method {ids.ref(key)}>{name}</lua_method>'
    return f'<lua_method {ids.define(key)}>{name}</lua_method>'


def _shared_vars(ids):
    """The empty motiongraph_vars ptr block every EDS shares."""
    key = "empty_vars"
    if ids.has(key):
        return (f'<motiongraph_vars pool_type="3" count="0">'
                f'<ptr pool_type="3" {ids.ref(key)} /></motiongraph_vars>')
    return (f'<motiongraph_vars pool_type="3" count="0">'
            f'<ptr pool_type="3" {ids.define(key)} /></motiongraph_vars>')


def eds_xml(ids, back_key):
    """EndDecisionScope closing `back_key`'s wrapper: back-edge child + ptr_1."""
    return (
        f'<mrfmember1 count_0="2" dtype="7" count_4="1">\n'
        f'{_method(ids, "MotionGraphEventHandling.EndDecisionScope")}\n'
        f'<ptr_0 raw="2,0" />\n{_shared_vars(ids)}\n'
        f'<children pool_type="3">\n<mrfchild count_0="1" count_1="7">\n'
        f'<m_r_f_member {ids.ref(back_key)} />\n'
        f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" {ids.ref("empty_vars")} />'
        f'</ptr_1>\n</mrfchild>\n</children>\n</mrfmember1>\n')


def _vartag(ids, name):
    key = ("varname", name)
    if ids.has(key):
        return f'<var_name {ids.ref(key)}>{name}</var_name>'
    return f'<var_name {ids.define(key)}>{name}</var_name>'


def var_node(ids, method, raw, varname):
    """Opening member with a NAMED variable binding (bool/enum heads, unk=5)."""
    return (
        f'<mrfmember1 count_0="2" dtype="7" count_4="1">\n'
        f'{_method(ids, method)}\n<ptr_0 raw="{raw}" />\n'
        f'<motiongraph_vars pool_type="3" count="1">\n<ptr pool_type="3">\n'
        f'<motiongraphvar unk="5">\n{_vartag(ids, "VariableName")}\n'
        f'<target_name>{varname}</target_name>\n</motiongraphvar>\n</ptr>\n'
        f'</motiongraph_vars>\n')


def avoid_vars(ids):
    """The Random* nodes' vars form: unk=1, AvoidRepeats bound to t[1]."""
    return (f'<motiongraph_vars pool_type="3" count="1">\n<ptr pool_type="3">\n'
            f'<motiongraphvar unk="1">\n{_vartag(ids, "AvoidRepeats")}\n'
            f'<target_name raw="1,0" />\n</motiongraphvar>\n</ptr>\n'
            f'</motiongraph_vars>')


def avoid_node(ids, method, raw):
    """Opening member for RandomSequence: AvoidRepeats form, not VariableName."""
    return (f'<mrfmember1 count_0="2" dtype="7" count_4="1">\n'
            f'{_method(ids, method)}\n<ptr_0 raw="{raw}" />\n{avoid_vars(ids)}\n')


def plain_node(ids, method, raw):
    """Opening member with EMPTY vars (MultiTriggerHandler - measured, its vars
    are the shared empty block; an earlier probe reported LoopAnimSelection
    there by recursively collecting DESCENDANT vars, which was wrong)."""
    return (f'<mrfmember1 count_0="2" dtype="7" count_4="1">\n'
            f'{_method(ids, method)}\n<ptr_0 raw="{raw}" />\n{_shared_vars(ids)}\n')


def named_vars(ids, varname):
    """A named-variable vars block (unk=5 VariableName form), standalone."""
    return (f'<motiongraph_vars pool_type="3" count="1">\n<ptr pool_type="3">\n'
            f'<motiongraphvar unk="5">\n{_vartag(ids, "VariableName")}\n'
            f'<target_name>{varname}</target_name>\n</motiongraphvar>\n</ptr>\n'
            f'</motiongraph_vars>')


def blendrecords_ptr0(ids, key, n):
    """BlendToState's INLINE blend table: ptr_0 multi=N of blendrecord rows.

    blend_time 0.4s and tier 65536*x are retail's default values; tier is the
    same 65536-family constant used by the transition-table trio elsewhere.
    Reading this field as raw/ref alone can miss it: the mt-branch BlendToState
    carries its blend table inline, it does not ref a state's rows.
    """
    zeros = " ".join(f'zero_{i:02d}="0"' for i in
                     (0, 1, 4, 5, 6, 7, 9, 10, 11, 12, 13, 15, 16, 17))
    tiers = [6553600, 655360, 65536000]
    rows = "\n".join(
        f'<blendrecord blend_time="{BLEND_TIME}" tier="{tiers[i % 3]}" '
        f'{zeros} />' for i in range(n))
    return (f'<ptr_0 {ids.define(("brec", key))} multi="{n}">\n{rows}\n</ptr_0>')


def trigger_ptr1(ids):
    """The multi-trigger play slot's REAL annotation: unk=5, binding the
    variable 'Trigger' to the literal trigger name 'SequenceTrigger'.

    Not a Weight->t[N] binding - that guess came from a payload scan that
    printed only the first six matches; this one was the seventh. This is the
    line that connects the sequencer's trigger to the play subtree.
    """
    return (f'<ptr_1 pool_type="3" unk="1"><ptr pool_type="3" unk="5">\n'
            f'{_vartag(ids, "Trigger")}\n'
            f'<target_name>SequenceTrigger</target_name>\n</ptr></ptr_1>')


def weight_ptr1(ids, raw):
    """A slot's unk=1 annotation: Weight bound to t[N]. raw='3,0' is the
    SequenceTrigger table - THIS is what connects a trigger to its play slot;
    unk=0 empty payloads left triggered play disconnected (measured in game)."""
    return (f'<ptr_1 pool_type="3" unk="1"><ptr pool_type="3" unk="2">\n'
            f'{_vartag(ids, "Weight")}\n<target_name raw="{raw}" />\n</ptr></ptr_1>')


def leaf_unit(ids, choice_i, amr2_key, slot=1, tag="ch"):
    """BlendToState(StateOutput+EDS)+EDS - the per-choice unit, wrapper form.

    `slot` is the 1-based mrfchild count_0 index. It is how the enum maps a
    choice VALUE to a branch - all-slots-1 made every dropdown entry play the
    first branch (measured in game: "Still" swayed). `tag` keeps wrapper ids
    unique when the same state is targeted from two places (enum + multi-trigger).
    """
    w = ("wrap_bts", tag, choice_i)
    x = ("wrap_so", tag, choice_i)
    sref = ids.ref(("state", choice_i))
    if amr2_key is None:
        # the multi-trigger's blend unit: blend table INLINE, and the vars REF
        # that same table - motiongraph_vars points at the ptr_0 blendrecord
        # block's own id. An inline empty-vars list here fails create() with
        # "Transition.to_stream failed on None" - dtype=8 members' vars are a
        # shared-block ref, not a list
        p0 = blendrecords_ptr0(ids, ("mt", tag, choice_i), 3)
        mv = f'<motiongraph_vars {ids.ref(("brec", ("mt", tag, choice_i)))} refcount="8" />'
    else:
        p0 = f'<ptr_0 {ids.ref(amr2_key)} />'
        mv = f'<motiongraph_vars {ids.ref(amr2_key)} refcount="8" />'
    return (
        f'<mrfchild count_0="{slot}" count_1="7">\n'
        f'<m_r_f_member multi="2" {ids.define(w)}>\n'
        f'<mrfmember1 count_0="1" dtype="8" count_4="1">\n'
        f'{_method(ids, "MotionGraph.BlendToState")}\n'
        f'{p0}\n{mv}\n'
        f'<children pool_type="3">\n<mrfchild count_0="1" count_1="0">\n'
        f'<m_r_f_member multi="2" {ids.define(x)}>\n'
        f'<mrfmember1 count_0="1" dtype="8" count_4="1">\n'
        f'{_method(ids, "MotionGraph.StateOutput")}\n'
        f'<ptr_0 {sref} />\n'
        f'<motiongraph_vars {sref} refcount="8" />\n'
        f'<children pool_type="3" {ids.ref("empty_vars")} />\n'
        f'</mrfmember1>\n{eds_xml(ids, x)}'
        f'</m_r_f_member>\n</mrfchild>\n</children>\n'
        f'</mrfmember1>\n{eds_xml(ids, w)}'
        f'</m_r_f_member>\n'
        f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" {ids.ref("empty_vars")} />'
        f'</ptr_1>\n</mrfchild>\n')


def so_slot(ids, choice_i, slot, tag, trig=False):
    """StateOutput+EDS wrapper in an mrfchild slot - the dispatch leaf.

    trig=True gives the slot the Weight->t[3] annotation that binds it to the
    SequenceTrigger; without it triggered play is disconnected.
    """
    x = ("wrap_soslot", tag, slot)
    sref = ids.ref(("state", choice_i))
    p1 = (weight_ptr1(ids, "3,0") if trig else
          f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
          f'{ids.ref("empty_vars")} /></ptr_1>')
    return (
        f'<mrfchild count_0="{slot}" count_1="7">\n'
        f'<m_r_f_member multi="2" {ids.define(x)}>\n'
        f'<mrfmember1 count_0="1" dtype="8" count_4="1">\n'
        f'{_method(ids, "MotionGraph.StateOutput")}\n'
        f'<ptr_0 {sref} />\n<motiongraph_vars {sref} refcount="8" />\n'
        f'<children pool_type="3" {ids.ref("empty_vars")} />\n'
        f'</mrfmember1>\n{eds_xml(ids, x)}'
        f'</m_r_f_member>\n'
        f'{p1}\n</mrfchild>\n')


def bare_open(ids, method, raw, vars_xml, children_xml):
    """A BARE-form member: lua_method directly on m_r_f_member, no closing EDS."""
    return (
        f'<m_r_f_member count_0="2" dtype="7" count_4="1">\n'
        f'{_method(ids, method)}\n<ptr_0 raw="{raw}" />\n{vars_xml}\n'
        f'<children pool_type="3">\n{children_xml}</children>\n'
        f'</m_r_f_member>\n')


def mt_play_unit(ids, clip_choices, weighted):
    """Slot 2 of the multi-trigger branch: TRIGGERED PLAY.

    Without this, deactivate and loop both work but 'play once every X seconds'
    and the sequencer trigger do nothing - confirmed in game. Shape: EDS
    wrapping a VariableResultEnum over LoopAnimSelection, choice 0 dispatching
    OnEntry -> RandomChoice over the WEIGHTED states, choices 1..N a
    StateOutput at the chosen state - every clip choice dispatches, only
    weighted ones join the random pool, mirroring the loop-mode enum.
    """
    rand = "".join(so_slot(ids, c, s + 1, "mtrand", trig=True)
                   for s, c in enumerate(weighted))
    inner = bare_open(ids,
                      "MotionGraphEventHandling.RandomChoiceEndDecisionScope",
                      "5,0", avoid_vars(ids), rand)
    onentry = bare_open(
        ids, "MotionGraphEventHandling.OnEntryBeginDecisionScope", "2,0",
        _shared_vars(ids),
        f'<mrfchild count_0="1" count_1="7">\n{inner}'
        f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
        f'{ids.ref("empty_vars")} /></ptr_1>\n</mrfchild>\n')
    choices = "".join(so_slot(ids, c, c + 1, "mtch") for c in clip_choices)
    # BARE form, like every single-member node in this subtree - the wrapper
    # multi="1" form did not exist in retail and the engine never dispatched
    # through it
    enum_full = (
        f'<mrfchild count_0="1" count_1="7">\n'
        + bare_open(ids, "MotionGraph.VariableResultEnum", "4,0",
                    named_vars(ids, "LoopAnimSelection"),
                    f'<mrfchild count_0="1" count_1="7">\n{onentry}'
                    f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
                    f'{ids.ref("empty_vars")} /></ptr_1>\n</mrfchild>\n{choices}')
        + f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" {ids.ref("empty_vars")} />'
          f'</ptr_1>\n</mrfchild>\n')
    return (
        f'<mrfchild count_0="2" count_1="7">\n'
        + bare_open(ids, "MotionGraphEventHandling.EndDecisionScope", "2,0",
                    _shared_vars(ids), enum_full)
        + trigger_ptr1(ids) + '\n</mrfchild>\n')


def random_slot(ids, seq_i, choice_i):
    """One weighted sequence slot of the random branch: EDS->StateOutput chain
    with a Weight annotation. Defines empty_vars on the first emission."""
    e = ("wrap_rseq", seq_i)
    sref = ids.ref(("state", choice_i))
    wn = ("varname", "Weight")
    wtag = (f'<var_name {ids.ref(wn)}>Weight</var_name>' if ids.has(wn)
            else f'<var_name {ids.define(wn)}>Weight</var_name>')
    return (
        f'<mrfchild count_0="{seq_i + 1}" count_1="7">\n'
        f'<m_r_f_member multi="2" {ids.define(e)}>\n'
        f'<mrfmember1 count_0="2" dtype="7" count_4="1">\n'
        f'{_method(ids, "MotionGraphEventHandling.EndDecisionScope")}\n'
        f'<ptr_0 raw="2,0" />\n{_shared_vars(ids)}\n'
        f'<children pool_type="3">\n<mrfchild count_0="1" count_1="7">\n'
        f'<m_r_f_member count_0="1" dtype="8" count_4="1">\n'
        f'{_method(ids, "MotionGraph.StateOutput")}\n'
        f'<ptr_0 {sref} />\n<motiongraph_vars {sref} refcount="8" />\n'
        f'<children pool_type="3" {ids.ref("empty_vars")} />\n'
        f'</m_r_f_member>\n'
        f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" {ids.ref("empty_vars")} />'
        f'</ptr_1>\n</mrfchild>\n</children>\n</mrfmember1>\n'
        f'{eds_xml(ids, e)}</m_r_f_member>\n'
        f'<ptr_1 pool_type="3" unk="1"><ptr pool_type="3" unk="2">\n'
        f'{wtag}\n<target_name raw="1,0" />\n</ptr></ptr_1>\n</mrfchild>\n')


def tree_xml(ids):
    top = "wrap_top"
    enum = "wrap_enum"
    rnd = "wrap_rnd"
    mth = "wrap_mt"
    # Two DIFFERENT subsets, deliberately: every clip-bearing choice gets an
    # enum branch (its dropdown entry must dispatch somewhere), while only
    # weighted choices join the Auto choice's random pool. Conflating them
    # made weight-null rows selectable-but-undispatched and shifted every
    # later slot number
    clip_choices = [i for i, (_, c, _, _) in enumerate(CHOICES) if c is not None]
    weighted = [i for i, (_, c, w, _) in enumerate(CHOICES)
                if c is not None and w is not None]
    parts = [f'\t<m_r_f_member_1 multi="2" {ids.define(top)}>\n',
             var_node(ids, "MotionGraph.VariableResultBool", "1,0",
                      "IsSequenceTriggerControlled"),
             '<children pool_type="3">\n',
             # branch 1: the enum over LoopAnimSelection
             f'<mrfchild count_0="1" count_1="7">\n'
             f'<m_r_f_member multi="2" {ids.define(enum)}>\n',
             var_node(ids, "MotionGraph.VariableResultEnum", "4,0",
                      "LoopAnimSelection"),
             '<children pool_type="3">\n',
             # choice 0: random over the weighted choices
             f'<mrfchild count_0="1" count_1="7">\n'
             f'<m_r_f_member multi="2" {ids.define(rnd)}>\n',
             avoid_node(ids,
                        "MotionGraphEventHandling.RandomSequenceBeginDecisionScope",
                        "5,0"),
             '<children pool_type="3">\n',
             *[random_slot(ids, s, c) for s, c in enumerate(weighted)],
             '</children>\n</mrfmember1>\n', eds_xml(ids, rnd),
             '</m_r_f_member>\n'
             f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
             f'{ids.ref("empty_vars")} /></ptr_1>\n</mrfchild>\n',
             # every clip-bearing choice gets its blend unit, each ref'ing its
             # TARGET's rows; slot number is the CHOICE index + 1 (slot 1 is
             # the Auto/random choice at index 0), so a weight-null row keeps
             # its place and later slots do not shift
             *[leaf_unit(ids, c, ("amr2", c), slot=c + 1)
               for c in clip_choices],
             '</children>\n</mrfmember1>\n', eds_xml(ids, enum),
             '</m_r_f_member>\n'
             f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
             f'{ids.ref("empty_vars")} /></ptr_1>\n</mrfchild>\n',
             # branch 2: sequence play via multi-trigger
             f'<mrfchild count_0="2" count_1="7">\n'
             f'<m_r_f_member multi="2" {ids.define(mth)}>\n',
             plain_node(ids,
                        "MotionGraphEventHandling.MultiTriggerHandlerBeginDecisionScope",
                        "3,0"),
             '<children pool_type="3">\n',
             # the sequencer branch. Deactivating hands the prop to sequencer
             # control, which walks THIS branch: an empty branch crashes the
             # game, and targeting the wrong state leaves deactivate with no
             # visible effect. The correct semantics are BLEND TO REST -
             # retail graphs target their rest/still state here, which by
             # this spec's convention is the first clip-bearing choice
             leaf_unit(ids, clip_choices[0], None, slot=1, tag="mt"),
             mt_play_unit(ids, clip_choices, weighted),
             '</children>\n</mrfmember1>\n', eds_xml(ids, mth),
             '</m_r_f_member>\n'
             f'<ptr_1 pool_type="3" unk="0"><ptr pool_type="3" '
             f'{ids.ref("empty_vars")} /></ptr_1>\n</mrfchild>\n',
             '</children>\n</mrfmember1>\n', eds_xml(ids, top),
             '\t</m_r_f_member_1>\n']
    return "".join(parts)


def assemble():
    ids = Ids()
    # every clip-bearing choice needs a state, weighted or not - transitions
    # target the full state set
    clip_choices = [i for i, (_, c, _, _) in enumerate(CHOICES) if c is not None]
    # depth-first define chain from the first clip state; the rest of the
    # top-level list is refs, matching the shape retail graphs use
    defined = set()
    states = "".join(state_xml(ids, i, clip_choices, defined)
                     for i in clip_choices)
    body = (
        f'<MotiongraphHeader name_root_tail="0000000000000000" count_0="9" '
        f'count_1="3" game="{GAME}">\n'
        '\t<state_output_entries pool_type="3">\n'
        f'\t\t<states pool_type="3">\n{states}\t\t</states>\n'
        '\t</state_output_entries>\n'
        f'{fixed_sections(ids)}'
        f'{tree_xml(ids)}'
        '</MotiongraphHeader>\n')
    return body




def generate(asset, choices, vars_ref, blend_time, game="Planet Coaster 2"):
    """(graph_xml, enumnamer_xml) for one asset. See module docstring.

    `game` follows the same runtime-parameter convention as every other
    module (no game names in filenames; specificity is a parameter). The
    structural knowledge emitted here was measured on Planet Coaster 2's
    retail corpus only, so other games are refused loudly rather than given
    an unverified graph - extend this guard only after measuring their
    corpora the same way.
    """
    if game != "Planet Coaster 2":
        raise ValueError(
            f"motiongraph generation is only measured and verified for "
            f"'Planet Coaster 2', not {game!r} - refusing to emit an "
            f"unverified graph")
    # Everything below is interpolated into XML and becomes graph identifiers,
    # so it is validated HERE at the API boundary, not only on the spec path:
    # asset and vars_ref arrive from CLI arguments and never pass parse_spec,
    # and a '<' or '&' in them yields malformed XML that fails far away
    for label_, value in (("asset", asset), ("vars_ref", vars_ref)):
        if not isinstance(value, str) or not _SAFE_IDENT.match(value):
            raise ValueError(
                f"{label_} {value!r} may only contain letters, digits and _ - "
                f"it becomes a graph identifier and an OVL entry name")
    global ASSET, VARS_REF, BLEND_TIME, CHOICES, GAME
    ASSET, VARS_REF, BLEND_TIME, GAME = asset, vars_ref, blend_time, game
    # (enum, clip, weight, events) - the label at index 3 is the caller's, for
    # its choice table and loc layer, and is deliberately not used here
    CHOICES = [(c[0], c[1], c[2], tuple(c[4]) if len(c) > 4 else ())
               for c in choices]
    if not CHOICES or CHOICES[0][1] is not None:
        raise ValueError("choice 0 must be the Auto/random choice (clip None)")
    for i, (enum, clip, _w, _ev) in enumerate(CHOICES):
        if i and clip is None:
            raise ValueError(
                f"choice {i} ({enum!r}) has no clip - only choice 0 may be "
                f"the clipless Auto choice")
        for what, value in (("enum_name", enum), ("clip", clip)):
            if value is not None and not _SAFE_IDENT.match(value):
                raise ValueError(
                    f"choice {i} {what} {value!r} may only contain letters, "
                    f"digits and _ - it becomes a graph identifier")
    if len(CHOICES) < 2:
        raise ValueError("no clip-bearing choices - nothing to generate")
    if not any(w is not None for _, c, w, _ in CHOICES if c is not None):
        raise ValueError(
            "every clip opted out of the random pool (weight null) - the Auto "
            "choice needs at least one weighted clip to pick from")
    return assemble(), enumnamer_xml(CHOICES)
