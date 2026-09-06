from pathlib import Path

from srhd_modkit.runtime_lint import lint_rson_runtime
from srhd_modkit.scripts import RsonProject


def codes(lines, *, variables=None):
    project = RsonProject({
        'Visual.Objects': [{
            'Variables': variables if variables is not None else [
                {'Type': 'TVar', 'Name': 'queue', '#': 0, 'Var.Type': 'Array', 'Init': '1'},
            ],
            'Operations': [{'Type': 'Top', '#': 1, 'Code.Type': 'Init', 'Code': lines}],
        }],
        'Visual.Links': [],
    }, Path('generator.rson'))
    return {issue.code for issue in lint_rson_runtime(project)}


def test_graph_array_initialization_is_supported_without_newarray():
    result = codes(['ArrayAdd(queue, 42);', 'ArrayClear(queue);'])
    assert 'runtime-persistent-array-use-without-newarray' not in result


def test_unknown_graph_variable_still_requires_array_initialization():
    result = codes(['ArrayClear(queue);'], variables=[{'Type': 'TVar', 'Name': 'queue', '#': 0}])
    assert 'runtime-persistent-array-use-without-newarray' in result


def test_graph_array_does_not_borrow_a_missing_sibling_initializer():
    result = codes([
        'function InitStorage() {', 'queue = newarray(1);', 'ArrayClear(other);', '}',
    ], variables=[
        {'Type': 'TVar', 'Name': 'queue', '#': 0, 'Var.Type': 'Array', 'Init': '1'},
        {'Type': 'TVar', 'Name': 'other', '#': 2},
    ])
    assert 'runtime-persistent-array-use-before-newarray' in result


def test_zero_based_iteration_after_explicit_service_slot_removal():
    result = codes([
        'ArrayAdd(queue, 42);', 'ArrayDelete(queue, 0);',
        'ArrayRandomize(10, queue);',
        'for(int i=0; i<ArrayDim(queue); i=i+1) {', 'result=queue[i];', '}',
        'result=queue[0];', 'if(ArrayDim(queue)>0) result=1;',
    ])
    assert 'runtime-rscript-array-service-index' not in result
    assert 'runtime-rscript-array-empty-dimension' not in result


def test_conditional_or_stale_slot_removal_does_not_hide_service_index():
    prefixes = [
        ['if(flag) ArrayDelete(queue, 0);'],
        ['if(flag)', 'ArrayDelete(queue, 0);'],
        ['if(flag) {', 'ArrayDelete(queue, 0);', '}'],
        ['function Uncalled() {', 'ArrayDelete(queue, 0);', '}'],
        ['ArrayDelete(queue, 0);', 'ArrayClear(queue);'],
        ['ArrayDelete(queue, 0);', 'queue=newarray(1);'],
        ['ArrayDelete(queue, 0);', 'MaybeReset(queue);'],
    ]
    for prefix in prefixes:
        assert 'runtime-rscript-array-service-index' in codes([*prefix, 'result=queue[0];']), prefix


def test_reset_after_loop_read_invalidates_later_iterations():
    result = codes([
        'ArrayDelete(queue, 0);',
        'for(int i=0; i<ArrayDim(queue); i=i+1) {',
        'result=queue[i];', 'ArrayClear(queue);', '}',
    ])
    assert 'runtime-rscript-array-service-index' in result


def test_live_bounded_star_enumeration_is_not_nullable_lookup():
    result = codes([
        'for(int i=0; i<GalaxyStars(); i=i+1) {',
        'dword star=GalaxyStar(i);', 'result=StarPlanets(star);',
        'for(int j=0; j<StarPlanets(star); j=j+1) {', 'result=j;', '}',
        'if(star && StarOwner(star)==People) result=1;',
        'result=StarName(GalaxyStar(i));', '}',
    ])
    assert 'runtime-object-api-without-explicit-guard' not in result
    assert 'runtime-object-api-behind-boolean-guard' not in result


def test_star_lookup_bounds_are_not_inferred_from_invalid_or_stale_loops():
    cases = [
        ['for(int i=0; i<=GalaxyStars(); i=i+1) {', 'dword star=GalaxyStar(i);', 'result=StarName(star);', '}'],
        ['for(int i=-1; i<GalaxyStars(); i=i+1) {', 'dword star=GalaxyStar(i);', 'result=StarName(star);', '}'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'i=i+2;', 'dword star=GalaxyStar(i);', 'result=StarName(star);', '}'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'dword star=GalaxyStar(i);', '}', 'result=StarName(star);'],
        ['dword star=GalaxyStar(saved_index);', 'result=StarName(star);'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'if(flag) star=GalaxyStar(i);', 'result=StarName(star);', '}'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'if(flag)', 'star=GalaxyStar(i);', 'result=StarName(star);', '}'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'if(flag) {', 'star=GalaxyStar(i);', '}', 'result=StarName(star);', '}'],
        ['for(int i=0; i<GalaxyStars(); i=i+1) {', 'star=GalaxyStar(i);', 'MaybeReset(star);', 'result=StarName(star);', '}'],
    ]
    for lines in cases:
        assert 'runtime-object-api-without-explicit-guard' in codes(lines), lines
