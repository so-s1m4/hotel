"""Pinned split composition without test-content reads or target relabelling."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from ingestion.guest_parser import _known_context
from scripts import compose_guest_sft as c
from scripts import train_guest_lora as launcher


def chat(identifier, intent='extra_towels', text=None, target=None):
    word = 'wifi' if intent == 'wifi_help' else 'towels'
    record = {'request_id': identifier, 'timestamp_utc': '2026-01-06T09:00:00Z',
              'room_id': 'R101', 'language': 'en', 'request_type': intent,
              'text': text or identifier + ': please help with ' + word + '.'}
    safe, _ = _known_context(record, {})
    target = target or {'intents': [{'intent': intent, 'requested_time_utc': None,
        'requested_time_text': None, 'evidence_quote': word, 'details': {}, 'needs_review': False}]}
    return {'messages': [{'role': 'system', 'content': c.system_prompt()},
        {'role': 'user', 'content': c.canonical({'guest_record': record, 'known_context': safe})},
        {'role': 'assistant', 'content': c.canonical(target)}]}


def parent(root, source):
    folder = root/source; folder.mkdir()
    examples=[]; outputs={}
    for split in ('train','valid','test'):
        identifier = source + '-' + split
        row=chat(identifier, 'wifi_help' if source=='synthetic' else 'extra_towels')
        raw=(c.canonical(row)+'\n').encode();(folder/(split+'.jsonl')).write_bytes(raw)
        outputs[split+'.jsonl']={'rows':1,'sha256':c.sha(raw)}
        examples.append({'source_dataset':source,'split':split,'output_line':1,'ids':[identifier],
            'group_ids':[identifier],'group_sha256':c.sha(identifier),
            'input_sha256':c.sha(row['messages'][1]['content']), 'target_sha256':c.sha(row['messages'][2]['content'])})
    manifest={'schema_version':'guest-sft-v1','source_dataset':source,'generation_profile':'extraction',
        'prompt':{'system_sha256':c.sha(c.system_prompt()),'generation_schema_sha256':c.generation_schema_sha256('extraction')},
        'code':{'files':{name:c.sha((c.ROOT/name).read_bytes()) for name in c.LEGACY_CODE}},
        'outputs':outputs,'examples':examples}
    (folder/'manifest.json').write_text(json.dumps(manifest))
    return folder


@pytest.fixture
def parents(tmp_path):
    return parent(tmp_path,'dev'),parent(tmp_path,'synthetic')


def pins(parents):
    return {kind+'_manifest_sha256':c.sha((path/'manifest.json').read_bytes()) for kind,path in zip(('dev','synthetic'),parents)}


def augmentation(root,parents):
    dev,syn=parents
    sources=[json.loads((path/'train.jsonl').read_text()) for path in parents]
    text=' '.join(json.loads(row['messages'][1]['content'])['guest_record']['text'] for row in sources)
    target={'intents':[json.loads(row['messages'][2]['content'])['intents'][0] for row in sources]}
    row=chat('FRAGMENT-INDEPENDENT', text=text,target=target)
    path=root/'fragments.jsonl';path.write_text(c.canonical(row)+'\n')
    refs=[]
    for kind,path_parent in zip(('dev','synthetic'),parents):
        m=c.obj(path_parent/'manifest.json');e=next(x for x in m['examples'] if x['split']=='train')
        refs.append({'manifest_sha256':c.sha((path_parent/'manifest.json').read_bytes()),'split':'train',
                     'output_line':1,'input_sha256':e['input_sha256'],'target_sha256':e['target_sha256']})
    sidecar={'schema_version':'guest-sft-train-fragments-v1','source_split':'train','chat_file_sha256':c.sha(path.read_bytes()),
        'rows':[{'output_line':1,'chat_sha256':c.sha(c.canonical(row)),'group_id':'FRAGMENT-GROUP',
                 'annotation':{'reviewed':True,'reviewer':'Synthetic fixture review','reviewer_kind':'assistant','method':'manual'},'parents':refs}]}
    provenance=root/'fragment-provenance.json';provenance.write_text(json.dumps(sidecar))
    return path,provenance


def compose(parents,out,**kwargs):
    return c.compose_dataset(*parents,out,**pins(parents),**kwargs)


def test_weighted_composition_preserves_parent_splits_and_empty_test_without_reading_it(parents,tmp_path,monkeypatch):
    aug,prov=augmentation(tmp_path,parents)
    original=Path.read_bytes;forbidden={str((p/'test.jsonl').resolve()) for p in parents}
    def no_test(path):
        assert str(path.resolve()) not in forbidden,'Parent TEST content must not be read'
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',no_test)
    out=tmp_path/'mixed'
    manifest=compose(parents,out,augmentation_chat=aug,augmentation_provenance=prov,augmentation_weight=2)
    assert manifest['source_dataset']=='mixed_dev_synthetic'
    assert manifest['counts']=={'parent_train_rows':2,'unique_parent_train':2,'unique_fragment_train':1,
        'unique_train_inputs':3,'train_exposures':4,'valid_rows':2,'test_rows':0,'augmentation_weight':2}
    assert (out/'test.jsonl').read_bytes()==b''
    assert len((out/'valid.jsonl').read_text().splitlines())==2
    fragment_examples=[row for row in manifest['examples'] if row['source']['kind']=='reviewed_train_fragment']
    assert [row['source']['repeat_index'] for row in fragment_examples]==[0,1]
    assert all(len(row['source']['linked_parent_groups'])==2 for row in fragment_examples)
    checked=launcher._data_identity(out)
    assert checked['manifest']==manifest


@pytest.mark.parametrize('change', ['source_kind','unknown_pin','parent_target','duplicate_line'])
def test_changed_parent_or_forged_line_provenance_fails_before_output(parents,tmp_path,change):
    dev=parents[0];m=c.obj(dev/'manifest.json');expected=pins(parents)
    if change=='source_kind':m['source_dataset']='quickstart'
    elif change=='duplicate_line':m['examples'].append(deepcopy(m['examples'][0]))
    elif change=='parent_target':m['examples'][0]['target_sha256']='0'*64
    else:expected['dev_manifest_sha256']='0'*64
    if change!='unknown_pin':
        (dev/'manifest.json').write_text(json.dumps(m));expected['dev_manifest_sha256']=c.sha((dev/'manifest.json').read_bytes())
    with pytest.raises(ValueError):c.compose_dataset(*parents,tmp_path/'mixed',**expected)
    assert not (tmp_path/'mixed').exists()


@pytest.mark.parametrize('change', ['unreviewed','valid_parent','unknown_parent','changed_target','repeated_parent','boolean_parent_line','missing_line'])
def test_bad_fragment_review_and_parent_claims_are_rejected(parents,tmp_path,change):
    aug,prov=augmentation(tmp_path,parents);data=c.obj(prov);row=data['rows'][0]
    if change=='unreviewed':row['annotation']['reviewed']=False
    elif change=='valid_parent':row['parents'][0]['split']='valid'
    elif change=='unknown_parent':row['parents'][0]['manifest_sha256']='0'*64
    elif change=='changed_target':row['parents'][0]['target_sha256']='0'*64
    elif change=='repeated_parent':row['parents'].append(deepcopy(row['parents'][0]))
    elif change=='boolean_parent_line':row['parents'][0]['output_line']=True
    else:data['rows']=[]
    prov.write_text(json.dumps(data))
    with pytest.raises(ValueError):compose(parents,tmp_path/'mixed',augmentation_chat=aug,augmentation_provenance=prov)
    assert not (tmp_path/'mixed').exists()


def test_parent_groups_cannot_be_hidden_by_new_fragment_group_id(parents,tmp_path):
    dev=parents[0];m=c.obj(dev/'manifest.json')
    # A malformed approved export with the same connected group across phases
    # cannot become safe by assigning a fresh operator group string later.
    train=next(x for x in m['examples'] if x['split']=='train')
    valid=next(x for x in m['examples'] if x['split']=='valid')
    valid['group_ids']=train['group_ids']
    (dev/'manifest.json').write_text(json.dumps(m))
    with pytest.raises(ValueError,match='Cross-split'):compose(parents,tmp_path/'mixed')


def test_augmentation_context_cannot_bypass_production_causality_filter(parents,tmp_path):
    aug,prov=augmentation(tmp_path,parents);row=json.loads(aug.read_text())
    user=json.loads(row['messages'][1]['content']);user['known_context']['future_room_release']=True
    user['known_context']['booking']={'status':'checked_out','completed_utc':'2027-01-01T00:00:00Z'}
    row['messages'][1]['content']=c.canonical(user);aug.write_text(c.canonical(row)+'\n')
    side=c.obj(prov);side['chat_file_sha256']=c.sha(aug.read_bytes());side['rows'][0]['chat_sha256']=c.sha(c.canonical(row));prov.write_text(json.dumps(side))
    with pytest.raises(ValueError,match='Known context'):compose(parents,tmp_path/'mixed',augmentation_chat=aug,augmentation_provenance=prov)


def test_same_parsed_input_with_different_json_whitespace_cannot_change_target(parents,tmp_path):
    aug,prov=augmentation(tmp_path,parents);row=json.loads(aug.read_text());duplicate=deepcopy(row)
    duplicate['messages'][1]['content']=json.dumps(json.loads(row['messages'][1]['content']),indent=2)
    target=json.loads(duplicate['messages'][2]['content']);target['intents'][0]['needs_review']=True
    duplicate['messages'][2]['content']=c.canonical(target)
    aug.write_text(c.canonical(row)+'\n'+c.canonical(duplicate)+'\n')
    side=c.obj(prov);side['chat_file_sha256']=c.sha(aug.read_bytes());second=deepcopy(side['rows'][0]);second.update(output_line=2,chat_sha256=c.sha(c.canonical(duplicate)))
    side['rows'].append(second);prov.write_text(json.dumps(side))
    with pytest.raises(ValueError,match='Conflicting targets'):compose(parents,tmp_path/'mixed',augmentation_chat=aug,augmentation_provenance=prov)


@pytest.mark.parametrize('alias_kind',['symlink','hardlink'])
def test_exclusion_alias_cannot_open_parent_test_contents(parents,tmp_path,monkeypatch,alias_kind):
    alias=tmp_path/'innocent.jsonl';target=parents[0]/'test.jsonl'
    if alias_kind=='symlink':alias.symlink_to(target)
    else:os.link(target,alias)
    original=Path.read_bytes
    def guard(path):
        assert not path.samefile(target),'TEST alias must be refused before content read'
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',guard)
    with pytest.raises(ValueError,match='TEST'):compose(parents,tmp_path/'mixed',excluded_evaluation_paths=[alias])


def test_mixed_validator_cannot_remove_required_evaluation_guards_or_relabel_output(parents,tmp_path):
    out=tmp_path/'mixed';compose(parents,out);manifest=c.obj(out/'manifest.json')
    manifest['composition']['excluded_evaluation_paths']=[];manifest['composition']['evaluation_sha256']={}
    (out/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='Required evaluation'):launcher._data_identity(out)


def test_changed_output_target_is_rejected_even_when_manifest_output_hash_is_rewritten(parents,tmp_path):
    out=tmp_path/'mixed';compose(parents,out)
    rows=[json.loads(line) for line in (out/'train.jsonl').read_text().splitlines()]
    target=json.loads(rows[0]['messages'][2]['content']);target['intents'][0]['needs_review']=True
    rows[0]['messages'][2]['content']=c.canonical(target)
    (out/'train.jsonl').write_text(''.join(c.canonical(row)+'\n' for row in rows))
    manifest=c.obj(out/'manifest.json');manifest['outputs']['train.jsonl']['sha256']=c.sha((out/'train.jsonl').read_bytes())
    (out/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='provenance'):launcher._data_identity(out)


def test_evaluation_message_is_not_promoted_via_reviewed_augmentation(parents,tmp_path):
    aug,prov=augmentation(tmp_path,parents)
    user=json.loads(json.loads(aug.read_text())['messages'][1]['content'])
    protected=tmp_path/'evaluation.jsonl';protected.write_text(json.dumps({'record':user['guest_record']})+'\n')
    with pytest.raises(ValueError,match='Evaluation text/identity'):
        compose(parents,tmp_path/'mixed',augmentation_chat=aug,augmentation_provenance=prov,
                excluded_evaluation_paths=[protected])
    assert not (tmp_path/'mixed').exists()


def test_test_metadata_group_cannot_be_reassigned_to_train_by_fragment_claim(parents,tmp_path):
    aug,prov=augmentation(tmp_path,parents);side=c.obj(prov)
    test=next(row for row in c.obj(parents[0]/'manifest.json')['examples'] if row['split']=='test')
    side['rows'][0]['group_id']=test['group_ids'][0]
    prov.write_text(json.dumps(side))
    with pytest.raises(ValueError,match='Cross-split'):
        compose(parents,tmp_path/'mixed',augmentation_chat=aug,augmentation_provenance=prov)


def test_parent_manifest_change_during_pinned_read_fails_before_publish(parents,tmp_path,monkeypatch):
    expected=pins(parents);target=(parents[0]/'manifest.json').resolve();original=Path.read_bytes;changed=[]
    def mutate_after_read(path):
        raw=original(path)
        if path.resolve()==target and not changed:
            data=json.loads(raw);data['examples'][0]['group_ids']=['different-group']
            target.write_text(json.dumps(data));changed.append(True)
        return raw
    monkeypatch.setattr(Path,'read_bytes',mutate_after_read)
    with pytest.raises(ValueError,match='changed'):
        c.compose_dataset(*parents,tmp_path/'mixed',**expected)
    assert changed and not (tmp_path/'mixed').exists()
