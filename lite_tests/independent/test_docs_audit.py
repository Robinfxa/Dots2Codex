"""Independent immutable connector goldens and strict indexed Docs regressions."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ["LITE_REPO"]) if "LITE_REPO" in os.environ else next(parent for parent in ROOT.parents if (parent / "dots_lite").is_dir())
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
from dots_lite import docs, protocol as p


def golden(name):
    return json.loads((ROOT / 'fixtures' / name).read_text())


def raw_document(text, document_id='fixture-lite-doc', revision='revision-after', tab_id='t.fixture'):
    """Independent SDK emulator; no candidate parser or normalizer used."""
    content = [{'endIndex': 1, 'sectionBreak': {}}]
    index = 1
    for line in text.splitlines(keepends=True):
        assert line.endswith('\n')
        end = index + len(line.encode('utf-16-le')) // 2
        content.append({'startIndex': index, 'endIndex': end, 'paragraph': {'elements': [
            {'startIndex': index, 'endIndex': end, 'textRun': {'content': line}}]}})
        index = end
    return {'documentId': document_id, 'revisionId': revision, 'suggestionsViewMode': 'SUGGESTIONS_INLINE',
            'tabs': [{'tabProperties': {'tabId': tab_id}, 'documentTab': {'body': {'content': content}}}]}


class DocsAudit(unittest.TestCase):
    def source(self): return docs.snapshot(golden('normalized-document.json'), 'fixture-lite-doc', 't.fixture')
    def plan(self): return docs.plan_write(self.source(), {'operation_id': 'op-golden', 'value': '新😀'}, 'op-golden')

    def test_golden_normalized_nullable_fields_and_utf16_are_exact(self):
        source = self.source()
        self.assertEqual(source['text'], '開始😀\n漢字と🚀𝄞\nend\n')
        plan = self.plan()
        self.assertEqual(plan['body']['requests'][0], {'deleteContentRange': {'range': {
            'startIndex': 1, 'endIndex': 17, 'tabId': 't.fixture'}}})
        self.assertEqual(plan['body']['writeControl'], {'requiredRevisionId': 'revision-before'})
        self.assertEqual(plan['body']['requests'][1]['insertText']['location'], {'index': 1, 'tabId': 't.fixture'})
        self.assertFalse(plan['body']['requests'][1]['insertText']['text'].endswith('\n'))

    def test_raw_sdk_and_supported_result_wrappers(self):
        raw = raw_document('開始😀\n漢字と🚀𝄞\nend\n')
        for envelope in [raw, {'result': raw}, {'structuredContent': {'result': raw}}]:
            self.assertEqual(docs.snapshot(envelope, 'fixture-lite-doc')['text'], self.source()['text'])

    def test_blank_document_uses_one_insert_and_one_reply(self):
        source = docs.snapshot(raw_document('\n', revision='empty-before'), 'fixture-lite-doc')
        plan = docs.plan_write(source, {'operation_id': 'op-init'}, 'op-init')
        self.assertEqual(len(plan['body']['requests']), 1)
        ack = {'documentId': 'fixture-lite-doc', 'replies': [{}], 'writeControl': {'requiredRevisionId': 'init-after'}}
        self.assertEqual(docs.accept_write(plan, ack)['status'], 'accepted')

    def test_actual_nullable_update_ack_is_accepted_unchanged(self):
        ack = golden('normalized-update-ack.json'); original = copy.deepcopy(ack)
        result = docs.accept_write(self.plan(), ack)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['revision_id'], 'revision-after')
        self.assertEqual(ack, original)

    def test_ack_wrong_doc_reply_revision_and_error_shapes_stay_unknown(self):
        mutations = [lambda a: a.update(documentId='other-doc'), lambda a: a.update(replies=[{}]),
            lambda a: a.update(replies=[{}, {}, {}]), lambda a: a.update(replies=[{'insertText': {}}, {}]),
            lambda a: a.update(writeControl={'requiredRevisionId': 'revision-before'}),
            lambda a: a.update(writeControl={'requiredRevisionId': None, 'targetRevisionId': None}),
            lambda a: a.update(writeControl={'requiredRevisionId': 'revision-after', 'targetRevisionId': 'r'}),
            lambda a: a.update(writeControl={'requiredRevisionId': 'revision-after', 'targetRevisionId': False}),
            lambda a: a.update(writeControl={'requiredRevisionId': 'revision-after', 'unknown': None})]
        for mutate in mutations:
            ack = golden('normalized-update-ack.json'); mutate(ack['structuredContent'])
            self.assertEqual(docs.accept_write(self.plan(), ack)['status'], 'unknown')
        for ack in [{'success': True}, {'content': [{'type': 'text', 'text': json.dumps(golden('normalized-update-ack.json')['structuredContent'])}]},
                    {**golden('normalized-update-ack.json'), 'isError': True}]:
            self.assertEqual(docs.accept_write(self.plan(), ack)['status'], 'unknown')

    def test_conflicting_optional_response_revision_is_not_a_valid_ack(self):
        ack = golden('normalized-update-ack.json'); ack['structuredContent']['revisionId'] = 'conflicting-revision'
        self.assertEqual(docs.accept_write(self.plan(), ack)['status'], 'unknown')

    def test_wrong_document_tab_missing_revision_and_indices_reject(self):
        for change in [lambda d: d.update(documentId='other-doc'), lambda d: d.pop('revisionId'),
                       lambda d: d['tabs'][0].update(tabId='t.other'),
                       lambda d: d['tabs'][0]['body']['content'][1].update(endIndex=5),
                       lambda d: d['tabs'][0]['body']['content'][1]['paragraph']['elements'][1].update(endIndex=4),
                       lambda d: d['tabs'][0]['body']['content'][1].update(startIndex=True)]:
            resource = golden('normalized-document.json'); change(resource['structuredContent'])
            with self.assertRaises(p.ProtocolError): docs.snapshot(resource, 'fixture-lite-doc', 't.fixture')

    def test_suggestions_objects_additional_tabs_and_dual_body_reject(self):
        mutations = [lambda d: d['tabs'].append(copy.deepcopy(d['tabs'][0])),
            lambda d: d.update(body=copy.deepcopy(d['tabs'][0]['body'])),
            lambda d: d['tabs'][0].update(inlineObjects={'object1': {}}),
            lambda d: d['tabs'][0].update(headers={'header1': {}}),
            lambda d: d['tabs'][0].update(suggestedDocumentStyleChanges={'s1': {}})]
        for mutate in mutations:
            resource = golden('normalized-document.json'); mutate(resource['structuredContent'])
            with self.assertRaises(p.ProtocolError): docs.snapshot(resource, 'fixture-lite-doc', 't.fixture')

    def test_absence_and_same_revision_are_not_proof_of_noncommit(self):
        plan = self.plan()
        self.assertEqual(docs.reconcile_write(plan, golden('normalized-document.json'))['status'], 'unknown')
        unchanged_revision = raw_document(plan['text'], revision='revision-before')
        self.assertNotEqual(docs.reconcile_write(plan, unchanged_revision)['status'], 'applied')
        committed = raw_document(plan['text'])
        self.assertEqual(docs.reconcile_write(plan, committed)['status'], 'applied')
        conflicting = raw_document('{"different":"state"}\n')
        self.assertEqual(docs.reconcile_write(plan, conflicting)['status'], 'conflicting')

    def test_mutated_write_packet_cannot_be_accepted(self):
        plan = self.plan(); plan['body']['writeControl']['requiredRevisionId'] = 'newer-revision'
        with self.assertRaises(p.ProtocolError): docs.accept_write(plan, golden('normalized-update-ack.json'))


if __name__ == '__main__': unittest.main(verbosity=2)
