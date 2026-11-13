"""CLI failure paths preserve complete generations and report partial rollback."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from common.contracts.schemas import DecisionRecordV2
import engine.output_artifacts as output
import run


@pytest.fixture
def cli(tmp_path, monkeypatch):
    data=tmp_path/'data';data.mkdir()
    (data/'building.json').write_text('{}')
    out=tmp_path/'decisions.jsonl';audit=tmp_path/'audit.jsonl'
    rejected=tmp_path/'decisions_rejections.jsonl'
    old={out:b'OLD_DECISIONS\n',audit:b'OLD_AUDIT\n',rejected:b'OLD_REJECTIONS\n'}
    for path,contents in old.items():path.write_bytes(contents)
    calls=[]
    row=DecisionRecordV2(event_id='NEW',timestamp_utc='2026-01-06T09:00:00Z',
        affected_assets=['BUILDING'],issue_type='data_quality',action='flag_anomaly',
        priority='low',reason='Controlled output generation',requires_human_approval=False)
    class Engine:
        def __init__(self,*args):
            calls.append('init')
            self.executor=SimpleNamespace(audit=[{'decision_id':'NEW','status':'simulated','simulated':True,'effects_applied':False}])
            self.event_count=1;self.rejections=[]
            self.notifications=SimpleNamespace(suppressed=0);self.water=SimpleNamespace(diagnostics={})
        def run(self):
            calls.append('replay')
            return [row]
    monkeypatch.setattr(run,'inspect_dataset',lambda directory:{'profile':'fixture'})
    monkeypatch.setattr('ingestion.replay.ReplayData',lambda *a,**kw:object())
    monkeypatch.setattr('engine.planner.DecisionEngine',Engine)
    def invoke(*extra,out_path=out,audit_path=audit):
        monkeypatch.setattr(sys,'argv',['run.py','--data-dir',str(data),'--out',str(out_path),'--audit',str(audit_path),*map(str,extra)])
        return run.main()
    return SimpleNamespace(root=tmp_path,data=data,out=out,audit=audit,rejected=rejected,old=old,calls=calls,invoke=invoke)


def assert_old(cli):
    assert all(path.read_bytes()==contents for path,contents in cli.old.items())


@pytest.mark.parametrize('kind',['audit_directory','rejections_directory','out_directory',
    'insights_directory','annual_directory','annual_json_directory','parent_file','path_alias','input_dataset','input_symlink','ancestor_output'])
def test_invalid_destinations_fail_before_replay_and_preserve_prior_outputs(cli,kind,capsys):
    extra=[];out_path=cli.out;audit_path=cli.audit
    if kind=='audit_directory':
        audit_path=cli.root/'bad-audit';audit_path.mkdir()
    elif kind=='rejections_directory':
        cli.rejected.unlink();cli.rejected.mkdir();cli.old.pop(cli.rejected)
    elif kind=='out_directory':
        out_path=cli.root/'bad-out';out_path.mkdir()
    elif kind in {'insights_directory','annual_directory','annual_json_directory'}:
        report=cli.root/'report.md'
        if kind=='annual_json_directory':report.with_suffix('.json').mkdir()
        else:report.mkdir()
        extra=['--insights' if kind=='insights_directory' else '--annual-insights',report]
    elif kind=='parent_file':
        parent=cli.root/'not-a-directory';parent.write_text('KEEP')
        audit_path=parent/'audit.jsonl'
    elif kind=='path_alias':audit_path=cli.root/'.'/'decisions.jsonl'
    elif kind=='input_dataset':out_path=cli.data/'building.json'
    elif kind=='input_symlink':
        out_path=cli.root/'input-alias';out_path.symlink_to(cli.data/'building.json')
    elif kind=='ancestor_output':
        out_path=cli.root/'not-created';audit_path=out_path/'audit.jsonl'
    with pytest.raises(SystemExit) as error:
        cli.invoke(*extra,out_path=out_path,audit_path=audit_path)
    assert error.value.code==2
    assert cli.calls==[]
    assert_old(cli)
    captured=capsys.readouterr()
    assert 'replay_complete' not in captured.out
    assert 'Output preflight failed' in captured.err
    assert (cli.data/'building.json').read_text()=='{}'


@pytest.mark.parametrize('alias_kind',['hardlink_output','symlink_output','hardlink_input'])
def test_existing_aliases_are_rejected_before_replay(cli,alias_kind):
    alias=cli.root/'alias.jsonl'
    if alias_kind=='hardlink_output':os.link(cli.out,alias)
    elif alias_kind=='symlink_output':alias.symlink_to(cli.out)
    else:os.link(cli.data/'building.json',alias)
    with pytest.raises(SystemExit):cli.invoke(audit_path=alias)
    assert not cli.calls
    assert_old(cli)


@pytest.mark.parametrize('failed_name',['audit.jsonl','decisions_rejections.jsonl','insights.md'])
def test_late_stage_write_failure_leaves_entire_previous_generation(cli,monkeypatch,capsys,failed_name):
    insights=cli.root/'insights.md'
    insights.write_bytes(b'OLD_INSIGHTS\n');cli.old[insights]=b'OLD_INSIGHTS\n'
    monkeypatch.setattr('engine.insights.generate_insights',lambda *a:'NEW_INSIGHTS\n')
    original=Path.write_text
    def write(path,text,*args,**kwargs):
        if path.name.startswith('.hotel-stage-'+failed_name+'-'):
            raise OSError('injected late artifact write failure')
        return original(path,text,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',write)
    with pytest.raises(SystemExit) as error:cli.invoke('--insights',insights)
    assert error.value.code==2 and cli.calls==['init','replay']
    assert_old(cli)
    captured=capsys.readouterr()
    assert 'replay_complete' not in captured.out
    assert 'not_published' in captured.err
    assert not list(cli.root.glob('.hotel-*'))


def test_late_optional_report_serialization_failure_preserves_outputs(cli,monkeypatch,capsys):
    def fail(*args):raise ValueError('injected report render failure')
    monkeypatch.setattr('engine.insights.generate_insights',fail)
    with pytest.raises(SystemExit):cli.invoke('--insights',cli.root/'report.md')
    assert_old(cli)
    assert 'replay_complete' not in capsys.readouterr().out
    assert not (cli.root/'report.md').exists()


def test_failed_replacement_rolls_back_earlier_outputs(cli,monkeypatch,capsys):
    replace=os.replace
    def fail_audit_stage(source,destination):
        if Path(destination)==cli.audit and Path(source).name.startswith('.hotel-stage-'):
            raise OSError('injected audit replace failure')
        return replace(source,destination)
    monkeypatch.setattr(output.os,'replace',fail_audit_stage)
    with pytest.raises(SystemExit):cli.invoke()
    assert_old(cli)
    captured=capsys.readouterr()
    assert 'rolled_back' in captured.err
    assert 'replay_complete' not in captured.out
    assert not list(cli.root.glob('.hotel-*'))


def test_failed_rollback_reports_partial_publication_and_keeps_recovery_backup(cli,monkeypatch,capsys):
    replace=os.replace
    def fail(source,destination):
        name=Path(source).name
        if Path(destination)==cli.audit and name.startswith('.hotel-stage-'):
            raise OSError('injected publication failure')
        if Path(destination)==cli.out and name.startswith('.hotel-backup-'):
            raise OSError('injected rollback failure')
        return replace(source,destination)
    monkeypatch.setattr(output.os,'replace',fail)
    with pytest.raises(SystemExit):cli.invoke()
    captured=capsys.readouterr()
    assert 'partial_publication' in captured.err and str(cli.out) in captured.err
    assert 'replay_complete' not in captured.out
    assert cli.out.read_bytes()!=cli.old[cli.out]
    assert cli.audit.read_bytes()==cli.old[cli.audit]
    backups=list(cli.root.glob('.hotel-backup-decisions.jsonl-*'))
    assert len(backups)==1 and backups[0].read_bytes()==cli.old[cli.out]


def test_normal_cli_publishes_all_optional_artifacts_before_completion(cli,monkeypatch,capsys):
    report=cli.root/'insights.md';annual=cli.root/'annual.md'
    monkeypatch.setattr('engine.insights.generate_insights',lambda *a:'Evidence summary\n')
    monkeypatch.setattr('engine.annual.analyze_dataset',lambda *a:{'fixture':True})
    monkeypatch.setattr('engine.annual.render_report',lambda *a:'Annual fixture\n')
    replace=os.replace;published=[]
    def track(source,destination):
        result=replace(source,destination)
        if Path(source).name.startswith('.hotel-stage-'):
            published.append(Path(destination))
        return result
    monkeypatch.setattr(output.os,'replace',track)
    assert cli.invoke('--insights',report,'--annual-insights',annual)==0
    summary=json.loads(capsys.readouterr().out)
    assert summary['status']=='replay_complete'
    assert set(published)=={cli.out,cli.audit,cli.rejected,report,annual,annual.with_suffix('.json')}
    assert json.loads(cli.out.read_text())['event_id']=='NEW'
    assert json.loads(cli.audit.read_text())['decision_id']=='NEW'
    assert cli.rejected.read_text()==''
    assert report.read_text()=='Evidence summary\n'
    assert annual.read_text()=='Annual fixture\n'
    assert json.loads(annual.with_suffix('.json').read_text())=={'fixture':True}
    assert not list(cli.root.glob('.hotel-*'))


def test_config_input_cannot_be_used_as_output(cli):
    config=cli.root/'config.json';config.write_text('{}')
    with pytest.raises(SystemExit):cli.invoke('--config',config,audit_path=config)
    assert not cli.calls and config.read_text()=='{}'
    assert_old(cli)


@pytest.mark.parametrize('kind',['keyboard','system_exit'])
@pytest.mark.parametrize('point',['before_first','after_first_return','before_second'])
def test_python_control_flow_interrupt_rolls_back_and_preserves_original_exception(cli,monkeypatch,capsys,kind,point):
    original_inodes={path:path.stat().st_ino for path in cli.old}
    interruption=KeyboardInterrupt('publication cancelled') if kind=='keyboard' else SystemExit(17)
    replace=os.replace
    def interrupt(source,destination):
        stage=Path(source).name.startswith('.hotel-stage-')
        if stage and Path(destination)==cli.out:
            if point=='before_first':raise interruption
            result=replace(source,destination)
            if point=='after_first_return':raise interruption
            return result
        if stage and Path(destination)==cli.audit and point=='before_second':raise interruption
        return replace(source,destination)
    monkeypatch.setattr(output.os,'replace',interrupt)
    with pytest.raises(type(interruption)) as error:cli.invoke()
    assert error.value is interruption
    assert error.value.publication_state==('not_published' if point=='before_first' else 'rolled_back')
    if point=='before_first':
        assert {path:path.stat().st_ino for path in cli.old}==original_inodes
    if kind=='system_exit':assert error.value.code==17
    assert_old(cli)
    assert not list(cli.root.glob('.hotel-*'))
    captured=capsys.readouterr()
    assert 'replay_complete' not in captured.out
    assert 'Output generation failed' not in captured.err


@pytest.mark.parametrize('rollback_kind',['io_error','keyboard','system_exit'])
def test_interrupted_publication_keeps_original_exception_and_backup_when_rollback_fails(cli,monkeypatch,capsys,rollback_kind):
    interruption=KeyboardInterrupt('original publication cancellation')
    rollback_error={'io_error':OSError('restore unavailable'),
                    'keyboard':KeyboardInterrupt('second cancellation'),
                    'system_exit':SystemExit(19)}[rollback_kind]
    replace=os.replace
    def fail(source,destination):
        name=Path(source).name
        if Path(destination)==cli.audit and name.startswith('.hotel-stage-'):raise interruption
        if Path(destination)==cli.out and name.startswith('.hotel-backup-'):raise rollback_error
        return replace(source,destination)
    monkeypatch.setattr(output.os,'replace',fail)
    with pytest.raises(KeyboardInterrupt) as error:cli.invoke()
    assert error.value is interruption
    assert error.value.publication_state=='partial_publication'
    assert error.value.changed_paths==[str(cli.out)]
    assert error.value.rollback_errors
    assert any('partial_publication' in note for note in error.value.__notes__)
    backup=Path(error.value.recovery_backups[0])
    assert backup.read_bytes()==cli.old[cli.out]
    assert cli.audit.read_bytes()==cli.old[cli.audit]
    assert cli.rejected.read_bytes()==cli.old[cli.rejected]
    assert cli.out.read_bytes()!=cli.old[cli.out]
    assert 'replay_complete' not in capsys.readouterr().out


def test_new_file_is_removed_when_interrupted_after_actual_replace(cli,monkeypatch):
    cli.out.unlink();cli.old.pop(cli.out)
    interruption=KeyboardInterrupt('new output interrupted')
    replace=os.replace
    def interrupt(source,destination):
        result=replace(source,destination)
        if Path(destination)==cli.out and Path(source).name.startswith('.hotel-stage-'):raise interruption
        return result
    monkeypatch.setattr(output.os,'replace',interrupt)
    with pytest.raises(KeyboardInterrupt) as error:cli.invoke()
    assert error.value is interruption
    assert not cli.out.exists()
    assert_old(cli)
    assert not list(cli.root.glob('.hotel-*'))


def test_interrupt_during_preparation_never_publishes_and_remains_keyboard_interrupt(cli,monkeypatch):
    interruption=KeyboardInterrupt('serialization interrupted')
    original=Path.write_text
    def interrupt(path,*args,**kwargs):
        if path.name.startswith('.hotel-stage-audit.jsonl-'):raise interruption
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'write_text',interrupt)
    with pytest.raises(KeyboardInterrupt) as error:cli.invoke()
    assert error.value is interruption and error.value.publication_state=='not_published'
    assert_old(cli)
    assert not list(cli.root.glob('.hotel-*'))


def test_keyboard_interrupt_during_io_error_rollback_is_not_converted_to_exit_two(cli,monkeypatch):
    interruption=KeyboardInterrupt('rollback cancelled')
    original_failure=OSError('original audit replace failure')
    replace=os.replace
    def fail(source,destination):
        name=Path(source).name
        if Path(destination)==cli.audit and name.startswith('.hotel-stage-'):raise original_failure
        if Path(destination)==cli.out and name.startswith('.hotel-backup-'):raise interruption
        return replace(source,destination)
    monkeypatch.setattr(output.os,'replace',fail)
    with pytest.raises(KeyboardInterrupt) as error:cli.invoke()
    assert error.value is interruption
    assert error.value.__cause__ is original_failure
    assert error.value.publication_state=='partial_publication'
    assert Path(error.value.recovery_backups[0]).read_bytes()==cli.old[cli.out]
