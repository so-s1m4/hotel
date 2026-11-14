"""Compose explicitly pinned Dev/Synthetic exports without changing their splits.

Only parent TRAIN/VALID chat contents are read. Parent TEST manifest metadata is
used for exclusion; test.jsonl is never opened. Optional reviewed fragments stay
TRAIN-only and link every component back to an approved parent TRAIN row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from common.contracts.schemas import GuestIntentV1
from ingestion.guest_generation import generation_schema, generation_schema_sha256
from ingestion.guest_parser import _canonical as parser_canonical, _known_context, _system_prompt, _utc
from scripts import prepare_guest_sft as prepare

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'guest-sft-mixed-v1'
SOURCE = 'mixed_dev_synthetic'
LEGACY_CODE = {'scripts/prepare_guest_sft.py', 'ingestion/guest_parser.py', 'ingestion/guest_generation.py',
               'ingestion/request_polarity.py', 'ingestion/llm_transport.py', 'common/contracts/schemas.py'}
CODE = LEGACY_CODE | {'scripts/compose_guest_sft.py', 'scripts/train_guest_lora.py'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def object_bytes(raw):
    value = json.loads(raw, object_pairs_hook=prepare._pairs,
                      parse_constant=prepare._invalid_constant, parse_float=prepare._finite_float)
    if not isinstance(value, dict):
        raise ValueError('Manifest/provenance must be an object')
    return value


def obj(path):
    return object_bytes(Path(path).read_bytes())


class ReadSet:
    """Pin one immutable read per source; never open a parent's TEST alias."""
    def __init__(self, forbidden=()):
        self.forbidden = [Path(path).resolve() for path in forbidden]
        self.snapshots = {}

    def read(self, path):
        path = Path(path)
        if any(path.resolve() == protected or (path.exists() and protected.exists() and path.samefile(protected)) for protected in self.forbidden):
            raise ValueError('Parent TEST contents/aliases must not be opened')
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if stamp(before) != stamp(after):
            raise ValueError('Source changed during read')
        current = (stamp(after), sha(raw))
        if path.resolve() in self.snapshots and self.snapshots[path.resolve()] != current:
            raise ValueError('Source changed during composition')
        self.snapshots[path.resolve()] = current
        return raw

    def verify(self):
        for path in list(self.snapshots):
            self.read(path)


def mandatory_exclusions():
    paths = list(prepare.EVALUATION_FIXTURES)
    diagnostic = ROOT/'outputs/release-candidate/blind-v2'
    if diagnostic.exists():
        paths.extend(diagnostic/name for name in ('single_intent_cases.jsonl','multi_intent_cases.jsonl'))
    return {str(Path(path).resolve()) for path in paths}


def system_prompt():
    return _system_prompt('extraction') + '\nSchema: ' + parser_canonical(generation_schema('extraction'))


def chat_identity(row, system):
    if not isinstance(row, dict) or set(row) != {'messages'}:
        raise ValueError('Chat row must contain exactly messages')
    messages = row['messages']
    if (not isinstance(messages, list) or len(messages) != 3
            or any(not isinstance(m, dict) or set(m) != {'role', 'content'} or not isinstance(m['content'], str) or not m['content'] for m in messages)
            or [m['role'] for m in messages] != ['system', 'user', 'assistant'] or messages[0]['content'] != system):
        raise ValueError('Chat roles/system prompt do not match current extraction prompt')
    user = json.loads(messages[1]['content'], object_pairs_hook=prepare._pairs, parse_constant=prepare._invalid_constant, parse_float=prepare._finite_float)
    target = json.loads(messages[2]['content'], object_pairs_hook=prepare._pairs, parse_constant=prepare._invalid_constant, parse_float=prepare._finite_float)
    if not isinstance(user, dict) or set(user) != {'guest_record', 'known_context'} or not isinstance(user['known_context'], dict):
        raise ValueError('Expected frozen production guest_record/known_context envelope')
    record = user['guest_record']
    if (not isinstance(record, dict) or not set(record) <= prepare.RECORD_KEYS
            or any(v is not None and not isinstance(v, str) for v in record.values())
            or any(not isinstance(record.get(k), str) or not record[k].strip() for k in ('request_id', 'timestamp_utc', 'language', 'request_type', 'text'))
            or not record['timestamp_utc'].endswith('Z') or _utc(record['timestamp_utc']) is None):
        raise ValueError('Invalid production source record')
    safe, _ = _known_context(record, user['known_context'])
    if canonical(safe) != canonical(user['known_context']):
        raise ValueError('Known context contains unsupported, future or outcome fields')
    if not isinstance(target, dict) or set(target) != {'intents'} or not isinstance(target['intents'], list) or not 1 <= len(target['intents']) <= 32:
        raise ValueError('Expected nonempty extraction target')
    for item in target['intents']:
        if (not isinstance(item, dict) or set(item) != prepare.TARGET_KEYS
                or item['requested_time_utc'] is not None or item['details'] != {} or not isinstance(item['details'], dict)
                or type(item['needs_review']) is not bool):
            raise ValueError('Extraction target shape/policy mismatch')
        for field in ('evidence_quote', 'requested_time_text'):
            quote = item[field]
            if quote is None and field == 'requested_time_text':
                continue
            if not isinstance(quote, str) or not quote or quote not in record['text']:
                raise ValueError('Target quotation must be an exact nonempty source substring')
        GuestIntentV1.model_validate(item, strict=True)
    return {'input_sha256': sha(messages[1]['content']), 'target_sha256': sha(messages[2]['content']),
            'production_input_sha256': sha(parser_canonical(user)), 'production_target_sha256': sha(canonical(target)),
            'record': record, 'chat': row}


def _metadata_keys(example):
    if any(not isinstance(example.get(k), list) or not example[k] or any(not isinstance(v, str) or not v for v in example[k]) for k in ('ids', 'group_ids')):
        raise ValueError('Missing parent IDs/groups')
    if any(not digest(example.get(k)) for k in ('input_sha256', 'target_sha256', 'group_sha256')):
        raise ValueError('Missing parent input/target/group hashes')
    return ({('id', value) for value in example['ids']} | {('group', value) for value in example['group_ids']}
            | {('input', example['input_sha256']), ('group_sha256', example['group_sha256'])})


def _parent(path, kind, pin, system, reads):
    path = Path(path).resolve()
    content = reads.read(path/'manifest.json')
    if not digest(pin) or sha(content) != pin:
        raise ValueError('Unknown or changed pinned parent manifest')
    manifest = object_bytes(content)
    if (manifest.get('schema_version') != 'guest-sft-v1' or manifest.get('source_dataset') != kind
            or manifest.get('generation_profile') != 'extraction'):
        raise ValueError('Only separate approved Dev and Synthetic extraction parents are allowed')
    if (manifest.get('prompt', {}).get('system_sha256') != sha(system)
            or manifest.get('prompt', {}).get('generation_schema_sha256') != generation_schema_sha256('extraction')):
        raise ValueError('Parent prompt/schema is not current extraction prompt')
    code = manifest.get('code', {}).get('files', {})
    if set(code) != LEGACY_CODE or any(not digest(value) for value in code.values()):
        raise ValueError('Missing parent exporter/parser code provenance')
    outputs = manifest.get('outputs', {})
    if set(outputs) != {'train.jsonl', 'valid.jsonl', 'test.jsonl'}:
        raise ValueError('Parent must identify all original splits')
    for output in outputs.values():
        if not isinstance(output, dict) or not digest(output.get('sha256')) or type(output.get('rows')) is not int or output['rows'] < 0:
            raise ValueError('Invalid parent output identity')
    examples = manifest.get('examples')
    if not isinstance(examples, list):
        raise ValueError('Missing parent line provenance')
    index = {}
    for example in examples:
        if not isinstance(example, dict) or example.get('source_dataset') != kind or example.get('split') not in prepare.SPLITS:
            raise ValueError('Invalid parent example provenance')
        line = example.get('output_line')
        pointer = (example['split'], line)
        if type(line) is not int or line < 1 or pointer in index:
            raise ValueError('Duplicate/invalid parent output line')
        _metadata_keys(example)
        index[pointer] = example
    for split in prepare.SPLITS:
        if {line for phase, line in index if phase == split} != set(range(1, outputs[split+'.jsonl']['rows']+1)):
            raise ValueError('Parent provenance does not cover each split line exactly')
    records = []
    for split in ('train', 'valid'):
        raw = reads.read(path/(split+'.jsonl'))
        if sha(raw) != outputs[split+'.jsonl']['sha256']:
            raise ValueError('Parent train/valid bytes changed')
        rows = prepare._json_rows(raw)
        if len(rows) != outputs[split+'.jsonl']['rows']:
            raise ValueError('Parent row count mismatch')
        for line, row in enumerate(rows, 1):
            identity = chat_identity(row, system)
            example = index[split, line]
            if any(identity[key] != example[key] for key in ('input_sha256', 'target_sha256')):
                raise ValueError('Parent target/input differs from original line provenance')
            records.append({**identity, 'split': split, 'parent_manifest_sha256': pin,
                            'parent_output_line': line, 'parent_example': example})
    # Deliberately do not open/hash/parse the parent's test.jsonl. These are
    # exclusion identifiers from the operator-pinned manifest, not test answers.
    test_metadata = [value for (split, _), value in index.items() if split == 'test']
    return {'path': str(path), 'manifest_sha256': pin, 'source_dataset': kind,
            'original_code': manifest['code'], 'outputs': outputs}, records, test_metadata


def _build(spec):
    system = system_prompt()
    reads = ReadSet([Path(spec[kind+'_export'])/'test.jsonl' for kind in ('dev','synthetic')])
    required = mandatory_exclusions()
    if not isinstance(spec.get('excluded_evaluation_paths'), list) or not required <= set(spec['excluded_evaluation_paths']):
        raise ValueError('Required evaluation exclusions cannot be removed')
    if not isinstance(spec.get('evaluation_sha256'), dict) or set(spec['evaluation_sha256']) != set(spec['excluded_evaluation_paths']):
        raise ValueError('Evaluation exclusion bytes must be pinned')
    parents, records, test_metadata = [], [], []
    for kind in ('dev', 'synthetic'):
        parent, rows, tests = _parent(spec[kind+'_export'], kind, spec[kind+'_manifest_sha256'], system, reads)
        parents.append(parent); records.extend(rows); test_metadata.extend(tests)
    weight = spec['augmentation_weight']
    if type(weight) is not int or not 1 <= weight <= 4:
        raise ValueError('Augmentation weight must be an integer 1..4')
    if bool(spec['augmentation_chat']) != bool(spec['augmentation_provenance']):
        raise ValueError('Augmentation chat and reviewed provenance must be supplied together')
    if not spec['augmentation_chat'] and weight != 1:
        raise ValueError('Weight requires augmentation')
    protected, protected_ids, exclusion_receipts = set(), set(), []
    for name in spec['excluded_evaluation_paths']:
        path = Path(name)
        if path.name == 'test.jsonl':
            raise ValueError('Do not open parent heldout contents as an exclusion fixture')
        raw = reads.read(path)
        if sha(raw) != spec['evaluation_sha256'].get(str(path)):
            raise ValueError('Evaluation exclusion bytes changed')
        for case in prepare._json_rows(raw):
            record = case.get('record')
            if not isinstance(record, dict) or not isinstance(record.get('text'), str):
                raise ValueError('Evaluation exclusion needs source records only')
            protected.add(prepare.normalize_text(record['text']))
            protected_ids.update((kind, record[key]) for kind, key in [('request','request_id'),('booking','booking_id')] if isinstance(record.get(key),str) and record[key])
        exclusion_receipts.append({'path': str(path.resolve()), 'sha256': sha(raw)})
    seen, targets, lineage = {}, {}, {}

    def register(split, keys, identity=None):
        if identity is not None:
            record = identity['record']
            text = prepare.normalize_text(record['text'])
            record_ids = {(kind, record[key]) for kind, key in [('request','request_id'),('booking','booking_id')] if isinstance(record.get(key),str) and record[key]}
            if text in protected or record_ids & protected_ids:
                raise ValueError('Evaluation text/identity cannot enter training or validation')
            keys = keys | record_ids | {('text',text), ('input',identity['input_sha256']), ('production_input',identity['production_input_sha256'])}
            previous = targets.setdefault(identity['production_input_sha256'], identity['production_target_sha256'])
            if previous != identity['production_target_sha256']:
                raise ValueError('Conflicting targets for the same production input')
        for key in keys:
            if key in seen and seen[key] != split:
                raise ValueError('Cross-split group/ID/text/input leakage')
            seen[key] = split
    for example in test_metadata:
        register('test', _metadata_keys(example))
    for record in records:
        register(record['split'], _metadata_keys(record['parent_example']), record)
        pointer = (record['parent_manifest_sha256'], record['split'], record['parent_output_line'])
        lineage[pointer] = record
    fragments, fragment_receipt = [], None
    if spec['augmentation_chat']:
        chat_path, provenance_path = Path(spec['augmentation_chat']), Path(spec['augmentation_provenance'])
        chat_bytes = reads.read(chat_path); provenance_bytes = reads.read(provenance_path); provenance = object_bytes(provenance_bytes)
        if (set(provenance) != {'schema_version','source_split','chat_file_sha256','rows'} or provenance.get('schema_version') != 'guest-sft-train-fragments-v1' or provenance.get('source_split') != 'train'
                or provenance.get('chat_file_sha256') != sha(chat_bytes) or not isinstance(provenance.get('rows'), list)):
            raise ValueError('Unknown, changed or non-TRAIN fragment provenance')
        rows = prepare._json_rows(chat_bytes); provenance_rows = {}
        for value in provenance['rows']:
            if not isinstance(value, dict) or set(value) != {'output_line','chat_sha256','group_id','annotation','parents'} or type(value.get('output_line')) is not int or value['output_line'] < 1 or value['output_line'] in provenance_rows:
                raise ValueError('Duplicate/invalid fragment output line')
            provenance_rows[value['output_line']] = value
        if set(provenance_rows) != set(range(1,len(rows)+1)) or not rows:
            raise ValueError('Fragment review must cover every nonempty chat row exactly')
        fragment_inputs = set()
        for line, row in enumerate(rows,1):
            review = provenance_rows[line]
            if review.get('chat_sha256') != sha(canonical(row)) or not isinstance(review.get('group_id'), str) or not review['group_id']:
                raise ValueError('Changed fragment row or missing group')
            annotation = review.get('annotation', {})
            if (not isinstance(annotation, dict) or not {'reviewed','reviewer','reviewer_kind','method'} <= set(annotation) or not set(annotation) <= {'reviewed','reviewer','reviewer_kind','method','notes'} or ('notes' in annotation and not isinstance(annotation['notes'],str)) or annotation.get('reviewed') is not True or not isinstance(annotation.get('reviewer'),str) or not annotation['reviewer'].strip()
                    or annotation.get('reviewer_kind') not in {'human','assistant','model'} or annotation.get('method') not in {'manual','teacher_reviewed'}):
                raise ValueError('Fragment requires explicit reviewed annotation')
            pointers, linked, keys = set(), [], {('group',review['group_id'])}
            if not isinstance(review['parents'], list):
                raise ValueError('Fragment parents must be a list')
            for parent in review['parents']:
                if not isinstance(parent,dict) or set(parent) != {'manifest_sha256','split','output_line','input_sha256','target_sha256'}:
                    raise ValueError('Invalid fragment parent pointer')
                if type(parent['output_line']) is not int or parent['output_line'] < 1 or not digest(parent['manifest_sha256']):
                    raise ValueError('Invalid typed fragment parent identity')
                pointer = (parent['manifest_sha256'], parent['split'], parent['output_line'])
                if parent['split'] != 'train' or pointer in pointers or pointer not in lineage:
                    raise ValueError('Unknown, repeated or non-TRAIN fragment parent')
                source = lineage[pointer]
                if any(parent[key] != source[key] for key in ('input_sha256','target_sha256')):
                    raise ValueError('Changed fragment parent target/input provenance')
                pointers.add(pointer); linked.append(source['parent_example']); keys |= _metadata_keys(source['parent_example'])
            if not pointers:
                raise ValueError('Every fragment must link approved TRAIN parent groups')
            identity = chat_identity(row,system)
            register('train',keys,identity)
            if identity['production_input_sha256'] in fragment_inputs:
                raise ValueError('Repeated fragment production input; declare weight instead')
            fragment_inputs.add(identity['production_input_sha256'])
            fragments.append({**identity,'split':'train','fragment_line':line,'fragment_provenance':review,
                              'linked_parent_groups':sorted({x['group_sha256'] for x in linked})})
        fragment_receipt = {'chat_path': str(chat_path.resolve()), 'chat_sha256':sha(chat_bytes),
            'provenance_path':str(provenance_path.resolve()), 'provenance_sha256':sha(provenance_bytes), 'unique_rows':len(rows)}
    output_rows = {'train': [], 'valid': [], 'test': []}; examples=[]
    for record in records:
        split=record['split'];output_rows[split].append(record['chat'])
        examples.append({'split':split,'output_line':len(output_rows[split]),'input_sha256':record['input_sha256'],
            'target_sha256':record['target_sha256'],'source':{'kind':'parent','manifest_sha256':record['parent_manifest_sha256'],
            'output_line':record['parent_output_line'],'original_example':record['parent_example']}})
    for record in fragments:
        for repeat in range(weight):
            output_rows['train'].append(record['chat'])
            examples.append({'split':'train','output_line':len(output_rows['train']),'input_sha256':record['input_sha256'],
                'target_sha256':record['target_sha256'],'source':{'kind':'reviewed_train_fragment','fragment_line':record['fragment_line'],
                'repeat_index':repeat,'review_and_parents':record['fragment_provenance'],'linked_parent_groups':record['linked_parent_groups']}})
    blobs={split+'.jsonl':''.join(canonical(row)+'\n' for row in rows).encode() for split,rows in output_rows.items()}
    manifest={'schema_version':SCHEMA,'source_dataset':SOURCE,'generation_profile':'extraction','composition':spec,
        'parents':parents,'augmentation':fragment_receipt,'prompt':{'system_sha256':sha(system),'generation_schema_sha256':generation_schema_sha256('extraction')},
        'code':{'files':{name:sha(reads.read(ROOT/name)) for name in sorted(CODE)}},
        'outputs':{name:{'sha256':sha(raw),'rows':len(output_rows[Path(name).stem])} for name,raw in blobs.items()},
        'counts':{'parent_train_rows':sum(r['split']=='train' for r in records),
                  'unique_parent_train':len({r['production_input_sha256'] for r in records if r['split']=='train'}),
                  'unique_fragment_train':len(fragments),
                  'unique_train_inputs':len({r['production_input_sha256'] for r in records if r['split']=='train'} | {r['production_input_sha256'] for r in fragments}),
                  'train_exposures':len(output_rows['train']),'valid_rows':len(output_rows['valid']),'test_rows':0,'augmentation_weight':weight},
        'examples':examples,'exclusion':{'evaluation_files':exclusion_receipts,'parent_test_metadata_rows':len(test_metadata),
            'parent_test_contents_read':False,'normalization':'NFKC + casefold + whitespace collapse'},
        'limits':['Parent pins/review annotations assert approval, not independent semantic truth.',
                  'Original parent code snapshots are retained honestly; current code snapshot identifies composition, not regeneration of parent labels.',
                  'TEST contents are not read. Metadata guards cannot establish cross-source semantic/paraphrase disjointness.',
                  'Validation splits are preserved; they may lack multi-intent coverage. Repetitions are exposures, not independent examples.',
                  'Fragment pointers prove approved TRAIN association; annotation review, not code, asserts semantic derivation/completeness.',
                  'Token lengths must pass the existing exact native tokenizer preflight; never truncate targets.']}
    reads.verify()
    return manifest,blobs


def compose_dataset(dev_export, synthetic_export, output_dir, *, dev_manifest_sha256, synthetic_manifest_sha256,
                    augmentation_chat=None, augmentation_provenance=None, augmentation_weight=1, excluded_evaluation_paths=()):
    out=Path(output_dir)
    if out.exists() or out.is_symlink():
        raise FileExistsError('Mixed output directory must be new')
    for parent in (dev_export,synthetic_export):
        root=Path(parent).resolve()
        if out.resolve()==root or root in out.resolve().parents:
            raise ValueError('Do not write inside approved parent exports')
    exclusions = mandatory_exclusions() | {str(Path(p).resolve()) for p in excluded_evaluation_paths}
    reads = ReadSet([Path(p)/'test.jsonl' for p in (dev_export,synthetic_export)])
    pins = {path:sha(reads.read(path)) for path in sorted(exclusions)}
    spec={'dev_export':str(Path(dev_export).resolve()),'synthetic_export':str(Path(synthetic_export).resolve()),
        'dev_manifest_sha256':dev_manifest_sha256,'synthetic_manifest_sha256':synthetic_manifest_sha256,
        'augmentation_chat':str(Path(augmentation_chat).resolve()) if augmentation_chat else None,
        'augmentation_provenance':str(Path(augmentation_provenance).resolve()) if augmentation_provenance else None,
        'augmentation_weight':augmentation_weight,'excluded_evaluation_paths':sorted(exclusions),'evaluation_sha256':pins}
    manifest,blobs=_build(spec)
    out.parent.mkdir(parents=True,exist_ok=True);out.mkdir()
    try:
        for name,raw in {**blobs,'manifest.json':(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()}.items():
            with (out/name).open('xb') as stream:stream.write(raw)
    except BaseException:
        shutil.rmtree(out);raise
    return manifest


def validate_composed_dataset(directory):
    directory=Path(directory)
    manifest=obj(directory/'manifest.json')
    if manifest.get('schema_version')!=SCHEMA or manifest.get('source_dataset')!=SOURCE:
        raise ValueError('Expected explicit mixed Dev/Synthetic manifest')
    spec=manifest.get('composition')
    required={'dev_export','synthetic_export','dev_manifest_sha256','synthetic_manifest_sha256','augmentation_chat',
              'augmentation_provenance','augmentation_weight','excluded_evaluation_paths','evaluation_sha256'}
    if not isinstance(spec,dict) or set(spec)!=required:
        raise ValueError('Missing mixed parent/fragment provenance')
    expected,blobs=_build(spec)
    if canonical(manifest)!=canonical(expected):
        raise ValueError('Mixed manifest/provenance differs from verified reconstruction')
    for name,raw in blobs.items():
        if (directory/name).read_bytes()!=raw:
            raise ValueError('Mixed target/input/split bytes differ from original provenance')
    return {'path':str(directory.resolve()),'manifest_sha256':sha((directory/'manifest.json').read_bytes()),'manifest':manifest}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dev-export','synthetic-export','out'):
        parser.add_argument('--'+name,required=True,type=Path)
    for name in ('dev-manifest-sha256','synthetic-manifest-sha256'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--augmentation-chat',type=Path);parser.add_argument('--augmentation-provenance',type=Path)
    parser.add_argument('--augmentation-weight',type=int,default=1)
    parser.add_argument('--exclude-evaluation',action='append',type=Path,default=[])
    args=vars(parser.parse_args(argv));args['output_dir']=args.pop('out');args['excluded_evaluation_paths']=args.pop('exclude_evaluation')
    manifest=compose_dataset(**args)
    print(json.dumps({'schema_version':SCHEMA,'counts':manifest['counts']}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
