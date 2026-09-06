import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from srhd_modkit.diagnostics import runtime_issue_rows
from srhd_modkit.runtime_lint import RuntimeIssue
from srhd_modkit.scripts import RSON_FILE_ID, RSON_FILE_VERSION
from srhd_modkit.toolchain import ScriptBuildFailure, Toolchain


def test_compile_allow_is_exact_scoped_and_visible(tmp_path):
    source = tmp_path / 'Generator.rson'
    source.write_text(json.dumps({
        'FileID': RSON_FILE_ID, 'FileVersion': RSON_FILE_VERSION,
        'ScriptName': 'Generator', 'Visual.Objects': [{'Operations': [
            {'Type': 'Top', 'Name': 'Init', '#': 1, 'Parent': -1,
             'Code.Type': 'Init', 'Code': ['result=1;']},
        ]}], 'Visual.Links': [],
    }), encoding='utf-8')
    issue = RuntimeIssue('error', 'runtime-object-api-without-explicit-guard', 'Review lookup', str(source))
    chain = Toolchain(tmp_path / 'no-external-tools')
    scr = tmp_path / 'out.scr'
    lang = tmp_path / 'out.lang.txt'

    def compile_stub(_source, staged_scr, staged_lang, **kwargs):
        staged_scr.write_bytes((8).to_bytes(4, 'little') + b'compiled')
        staged_lang.write_bytes(''.encode('utf-16'))
        return SimpleNamespace(exit_code=0, forced_after_outputs=False, elapsed_seconds=0.01), {}, {}

    with patch('srhd_modkit.toolchain.lint_rson_runtime', return_value=[issue]), patch.object(
        chain, '_compile_rson_with_rscript', side_effect=compile_stub,
    ) as compiler:
        for rules in [(), ('runtime-*',), (issue.code + ':Different.rson',)]:
            with pytest.raises(ScriptBuildFailure) as failure:
                chain.compile_rson(source, scr, lang, allow=rules)
            report = failure.value.as_dict()
            assert report['preflight_passed'] is False
            assert report['compiler_started'] is False
            assert report['runtime_issues'][0]['code'] == issue.code
            assert not scr.exists()
        compiler.assert_not_called()
        result = chain.compile_rson(source, scr, lang, allow=[issue.code + ':Generator.rson'])
        assert scr.is_file()
        assert result['runtime_issues'][0]['suppressed'] is True
        assert result['runtime_issues'][0]['severity'] == 'error'
        assert result['allow'] == [issue.code + ':Generator.rson']
        compiler.assert_called_once()


def test_allow_does_not_hide_a_different_error_or_remove_evidence(tmp_path):
    rows = runtime_issue_rows([
        RuntimeIssue('error', 'runtime-one', 'First', str(tmp_path / 'a.rson')),
        RuntimeIssue('error', 'runtime-two', 'Second', str(tmp_path / 'a.rson')),
        RuntimeIssue('error', 'runtime-one', 'Third', str(tmp_path / 'b.rson')),
    ], tmp_path, ['runtime-one:a.rson'])
    assert len(rows) == 3
    assert [row.get('suppressed', False) for row in rows] == [True, False, False]


def test_cli_exposes_specific_allow_without_global_skip_switch():
    from srhd_modkit.cli import build_parser
    args = build_parser().parse_args([
        'script', 'build', 'test.rson', '--scr', 'test.scr', '--lang', 'test.txt',
        '--allow', 'runtime-example:*.rson',
    ])
    assert args.allow == ['runtime-example:*.rson']
