"""Offline indexed Global queue regression, structural validation and atomic CAS."""
import copy
import json
import unittest

from remote_tests import test_global_heartbeat as fixtures
from remote_tests.test_global_control import (
    FakeGoogle, indexed_body, indexed_document, replace_document_text, utf16_length,
)
from remote_transport import global_control as queue
from remote_transport.control import CASConflict
from remote_transport.model import ProtocolError


class IndexedTopologyTests(unittest.TestCase):
    def snapshot(self, document):
        return queue.snapshot(document, 'fixture-document', 't.0')

    def test_raw_and_normalized_resources_have_exact_utf16_multirun_topology(self):
        text = '開始😀\n漢字と🚀𝄞\u0085\u2028\u2029\nend\n'
        raw = indexed_document(text)
        self.assertEqual(self.snapshot(raw).text, text)
        body = raw['tabs'][0]['documentTab']['body']
        self.assertEqual(body['content'][-1]['endIndex'], 1+utf16_length(text))
        self.assertGreater(utf16_length(text), len(text))
        self.assertGreater(len(body['content'][1]['paragraph']['elements']), 1)
        normalized = copy.deepcopy(raw)
        normalized['tabs'] = [{'tabId': 't.0', 'parentTabId': None,
                               'body': copy.deepcopy(body)}]
        self.assertEqual(self.snapshot(normalized).text, text)
        # Either explicit zero or omitted zero is the provider's section break.
        raw['tabs'][0]['documentTab']['body']['content'][0]['startIndex'] = 0
        self.assertEqual(self.snapshot(raw).text, text)
        without_section = copy.deepcopy(raw)
        without_section['tabs'][0]['documentTab']['body']['content'].pop(0)
        self.assertEqual(self.snapshot(without_section).text, text)

    def test_normalized_full_wrapper_accepts_proven_nullable_metadata(self):
        # Schema copied from a previously saved, sanitized connector shape.
        # All content, identifiers, URLs and style values here are synthetic.
        # Its three paragraph spans match the reported 24,660-unit replacement.
        text = 'x'*37 + '\n' + 'y'*24586 + '\n' + 'z'*35 + '\n'
        body = indexed_body(text)
        body['content'][0]['sectionBreak'] = {'sectionStyle': {
            'columnSeparatorStyle': 'NONE', 'contentDirection': 'LEFT_TO_RIGHT',
            'sectionType': 'CONTINUOUS'}}
        for paragraph in body['content'][1:]:
            paragraph['paragraph']['paragraphStyle'] = {
                'direction': 'LEFT_TO_RIGHT', 'namedStyleType': 'NORMAL_TEXT'}
            for run in paragraph['paragraph']['elements']:
                run['textRun']['textStyle'] = {}
        normalized = {
            'body': None, 'documentId': 'fixture-document',
            'document_url': 'https://docs.google.com/document/d/fixture-document/edit',
            'revisionId': 'r-new', 'suggestionsViewMode': 'SUGGESTIONS_INLINE',
            'title': None, 'url': None,
            'tabs': [{'body': body, 'documentId': 'fixture-document',
                'documentStyle': {}, 'document_url': 'https://docs.google.com/document/d/fixture-document/edit',
                'footers': None, 'footnotes': None, 'headers': None, 'iconEmoji': None,
                'index': 0, 'inlineObjects': None, 'lists': None, 'namedRanges': None,
                'namedStyles': {}, 'nestingLevel': None, 'parentTabId': None,
                'positionedObjects': None, 'suggestedDocumentStyleChanges': None,
                'suggestedNamedStylesChanges': None, 'tabId': 't.0', 'title': 'Synthetic tab'}]}
        self.assertEqual(self.snapshot(normalized).text, text)
        self.assertEqual([element['endIndex'] for element in body['content']], [1, 39, 24626, 24662])
        self.assertEqual(utf16_length(text[:-1]), 24660)
        for field in ('headers', 'footers', 'footnotes', 'inlineObjects', 'positionedObjects', 'lists'):
            bad = copy.deepcopy(normalized); bad['tabs'][0][field] = {'hidden': {}}
            with self.subTest(field=field), self.assertRaises(ProtocolError): self.snapshot(bad)
        bad = copy.deepcopy(normalized); bad['body'] = copy.deepcopy(body)
        with self.assertRaises(ProtocolError): self.snapshot(bad)

    def test_24660_character_source_and_larger_body_preserve_final_newline_indices(self):
        for size in (24660, 75000):
            with self.subTest(size=size):
                text = 'A\n' + 'x'*(size-4) + '\nZ'
                text += '\n'
                self.assertEqual(len(text), size+1)
                document = indexed_document(text)
                source = self.snapshot(document)
                self.assertEqual(source.text, text)
                self.assertEqual(1+utf16_length(source.text[:-1]), size+1)
                self.assertEqual(document['tabs'][0]['documentTab']['body']['content'][-1]['endIndex'], size+2)

    def test_indices_are_required_contiguous_exact_integers(self):
        original = indexed_document('first\nsecond😀\n')
        paths = [('section', 'endIndex'), ('paragraph', 'startIndex'), ('paragraph', 'endIndex'),
                 ('run', 'startIndex'), ('run', 'endIndex')]
        def target(document, label):
            body = document['tabs'][0]['documentTab']['body']['content']
            return body[0] if label == 'section' else body[1] if label == 'paragraph' else body[1]['paragraph']['elements'][0]
        for label, key in paths:
            for value in (None, True, False, 1.0, '1', -1, 100000):
                bad = copy.deepcopy(original); target(bad, label)[key] = value
                with self.subTest(label=label, key=key, value=value), self.assertRaises(ProtocolError):
                    self.snapshot(bad)
            bad = copy.deepcopy(original); del target(bad, label)[key]
            with self.subTest(label=label, missing=key), self.assertRaises(ProtocolError):
                self.snapshot(bad)
        for location in ('section', 'paragraph', 'run'):
            bad = copy.deepcopy(original)
            target(bad, location)['endIndex'] += 1
            with self.subTest(overlap=location), self.assertRaises(ProtocolError):
                self.snapshot(bad)
        bad = copy.deepcopy(original)
        bad['tabs'][0]['documentTab']['body']['content'][2]['startIndex'] += 1
        with self.assertRaises(ProtocolError): self.snapshot(bad)
        bad = indexed_document('A😀\n')
        for element in bad['tabs'][0]['documentTab']['body']['content'][1:]:
            element['endIndex'] -= 1
            element['paragraph']['elements'][-1]['endIndex'] -= 1
        with self.assertRaises(ProtocolError): self.snapshot(bad)

    def test_indexless_multiline_single_run_fake_is_not_a_valid_provider_resource(self):
        bad = indexed_document('first\nsecond\n')
        bad['tabs'][0]['documentTab']['body']['content'] = [
            {'sectionBreak': {}}, {'paragraph': {'elements': [{'textRun': {'content': 'first\nsecond\n'}}]}}]
        with self.assertRaises(ProtocolError): self.snapshot(bad)
        bad = indexed_document('first\nsecond\n')
        body = bad['tabs'][0]['documentTab']['body']['content']
        body[1]['paragraph']['elements'].extend(body[2]['paragraph']['elements'])
        body[1]['endIndex'] = body[2]['endIndex']; del body[2]
        with self.assertRaises(ProtocolError): self.snapshot(bad)

    def test_ambiguous_tabs_suggestions_and_nontext_structure_fail_closed(self):
        def body(document): return document['tabs'][0]['documentTab']['body']['content']
        def dt(document): return document['tabs'][0]['documentTab']
        def run(document): return body(document)[1]['paragraph']['elements'][0]
        mutations = {
            'wrong document': lambda d: d.update(documentId='another-document'),
            'missing revision': lambda d: d.pop('revisionId'),
            'empty revision': lambda d: d.update(revisionId=''),
            'bool revision': lambda d: d.update(revisionId=True),
            'missing suggestions mode': lambda d: d.pop('suggestionsViewMode'),
            'preview suggestions': lambda d: d.update(suggestionsViewMode='PREVIEW_WITHOUT_SUGGESTIONS'),
            'second tab': lambda d: d['tabs'].append(copy.deepcopy(d['tabs'][0])),
            'child tab': lambda d: d['tabs'][0].update(childTabs=[{'tabId': 'child'}]),
            'wrong tab': lambda d: d['tabs'][0]['tabProperties'].update(tabId='other'),
            'mixed normalized raw': lambda d: d['tabs'][0].update(tabId='t.0'),
            'header': lambda d: dt(d).update(headers={'h': {'content': []}}),
            'footer': lambda d: dt(d).update(footers={'f': {'content': []}}),
            'footnote': lambda d: dt(d).update(footnotes={'f': {'content': []}}),
            'missing body': lambda d: dt(d).pop('body'),
            'empty body': lambda d: dt(d)['body'].update(content=[]),
            'duplicate section': lambda d: body(d).insert(0, copy.deepcopy(body(d)[0])),
            'late section': lambda d: body(d).append(copy.deepcopy(body(d)[0])),
            'table': lambda d: body(d)[1].update(table={}),
            'toc': lambda d: body(d)[1].update(tableOfContents={}),
            'inline object': lambda d: run(d).update(inlineObjectElement={'inlineObjectId': 'object'}),
            'footnote reference': lambda d: run(d).update(footnoteReference={'footnoteId': 'f'}),
            'insertion suggestion': lambda d: run(d).update(suggestedInsertionIds=['s']),
            'deletion suggestion': lambda d: run(d).update(suggestedDeletionIds=['s']),
            'nested suggestion': lambda d: run(d)['textRun'].update(suggestedTextStyleChanges={'s': {}}),
            'empty run': lambda d: run(d)['textRun'].update(content=''),
            'nonstring run': lambda d: run(d)['textRun'].update(content=42),
            'missing elements': lambda d: body(d)[1]['paragraph'].pop('elements'),
        }
        for label, mutate in mutations.items():
            bad = indexed_document('first\nsecond\n'); mutate(bad)
            with self.subTest(case=label), self.assertRaises(ProtocolError): self.snapshot(bad)


class AtomicDocsFakeTests(unittest.TestCase):
    def setUp(self):
        self.google = FakeGoogle()
        self.document = self.google.create_document_once('folder', 'fixture')
        self.google.docs[self.document] = ['開始😀\nlast\n', 7]

    def arguments(self, text='new😀\nvalue'):
        source = self.google.docs[self.document][0]
        return {'document_id': self.document, 'write_control': {'requiredRevisionId': 'r7'},
                'requests': [{'deleteContentRange': {'range': {'startIndex': 1,
                    'endIndex': 1+utf16_length(source[:-1]), 'tabId': 't.0'}}},
                    {'insertText': {'location': {'index': 1, 'tabId': 't.0'}, 'text': text}}]}

    def test_two_subrequests_commit_once_with_two_empty_replies(self):
        response = self.google.batch_update_document(**self.arguments())
        self.assertEqual(response['replies'], [{}, {}])
        self.assertEqual(response['writeControl'], {'requiredRevisionId': 'r8'})
        self.assertEqual(self.google.docs[self.document], ['new😀\nvalue\n', 8])
        self.assertEqual(len(self.google.calls), 2)

    def test_stale_revision_rejects_whole_batch_without_mutation(self):
        arguments = self.arguments(); arguments['write_control']['requiredRevisionId'] = 'r6'
        before = copy.deepcopy(self.google.docs)
        with self.assertRaises(CASConflict): self.google.batch_update_document(**arguments)
        self.assertEqual(self.google.docs, before)

    def test_bad_second_request_never_leaves_deletion_committed(self):
        for edit in (lambda a: a['requests'][1]['insertText']['location'].update(tabId='other'),
                     lambda a: a['requests'][1]['insertText']['location'].update(index=2),
                     lambda a: a['requests'][1]['insertText'].update(text=None)):
            arguments = self.arguments(); edit(arguments); before = copy.deepcopy(self.google.docs)
            with self.assertRaises(AssertionError): self.google.batch_update_document(**arguments)
            self.assertEqual(self.google.docs, before)

    def test_range_cannot_split_astral_or_delete_implicit_newline(self):
        for end in (4, 1+utf16_length(self.google.docs[self.document][0]), True, 2.0, '2'):
            arguments = self.arguments()
            arguments['requests'][0]['deleteContentRange']['range']['endIndex'] = end
            before = copy.deepcopy(self.google.docs)
            with self.subTest(end=end), self.assertRaises(AssertionError): self.google.batch_update_document(**arguments)
            self.assertEqual(self.google.docs, before)

    def test_lost_response_is_after_atomic_commit_and_not_an_extra_revision(self):
        self.google.lose = True
        with self.assertRaises(TimeoutError): self.google.batch_update_document(**self.arguments())
        self.assertEqual(self.google.docs[self.document], ['new😀\nvalue\n', 8])
        self.assertEqual(self.google.get_document(self.document)['revisionId'], 'r8')

    def test_child_replace_protocol_retains_occurrence_response(self):
        response = self.google.batch_update_document(self.document, [
            {'replaceAllText': {'containsText': {'text': 'last', 'matchCase': True, 'searchByRegex': False},
                               'replaceText': 'child', 'tabsCriteria': {'tabIds': ['t.0']}}}],
            {'requiredRevisionId': 'r7'})
        self.assertEqual(response['replies'], [{'replaceAllText': {'occurrencesChanged': 1}}])
        self.assertEqual(self.google.docs[self.document], ['開始😀\nchild\n', 8])


class IndexedQueuePlanTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def assert_indexed(self, source, packet):
        self.assertEqual(packet['tool_arguments'], {
            'document_id': source.document_id,
            'write_control': {'requiredRevisionId': source.revision_id},
            'requests': [
                {'deleteContentRange': {'range': {'startIndex': 1,
                    'endIndex': 1+utf16_length(source.text[:-1]), 'tabId': source.tab_id}}},
                {'insertText': {'location': {'index': 1, 'tabId': source.tab_id},
                                'text': queue.block(packet['expected_state'])[:-1]}}]})
        self.assertEqual(queue.validate_plan(packet, self.f.code).text, source.text)
        self.assertNotIn('replaceAllText', json.dumps(packet['tool_arguments']))

    def planned(self, kind, **arguments):
        source = self.f.source(); path, out = self.f.plan(kind, **arguments)
        packet = json.loads(path.read_text()); self.assert_indexed(source, packet)
        self.assertTrue(self.f.commit(path, out)['verified'])
        return source, path, out, packet

    def test_single_event_uses_indexed_batch(self):
        self.planned('join', capacity=2, seconds=1200)

    def test_normalized_response_nullable_target_is_accepted_without_mutation(self):
        f = self.f; source = f.source(); path, out = f.plan('join', capacity=2, seconds=1200)
        packet = json.loads(path.read_text())
        raw = f.google.batch_update_document(**out['tool_arguments'])
        # Connector response schema; identifiers and revisions are synthetic.
        response = {'documentId': source.document_id,
                    'document_url': 'https://docs.google.com/document/d/fixture-document/edit',
                    'revisionId': raw['writeControl']['requiredRevisionId'], 'replies': [{}, {}],
                    'writeControl': {'requiredRevisionId': raw['writeControl']['requiredRevisionId'],
                                     'targetRevisionId': None}}
        original = copy.deepcopy(response); readback = f.google.get_document(source.document_id)
        self.assertEqual(queue.verify_update(packet, raw, readback, f.code).state, packet['expected_state'])
        self.assertEqual(queue.verify_update(packet, response, readback, f.code).state, packet['expected_state'])
        self.assertTrue(f.ledger.verify_plan(path, response, readback)['verified'])
        self.assertEqual(response, original)
        self.assertEqual(packet['tool_arguments']['write_control'], {'requiredRevisionId': source.revision_id})

    def test_nullable_response_support_rejects_nonnull_target_unknown_keys_and_bad_required(self):
        f = self.f; source = f.source(); path, out = f.plan('join', capacity=2, seconds=1200)
        packet = json.loads(path.read_text()); response = f.google.batch_update_document(**out['tool_arguments'])
        readback = f.google.get_document(source.document_id)
        revision = response['writeControl']['requiredRevisionId']
        controls = [None, [], {}, {'targetRevisionId': None},
                    {'requiredRevisionId': revision, 'unexpected': None},
                    {'requiredRevisionId': revision, 'targetRevisionId': None, 'unexpected': None}]
        controls += [{'requiredRevisionId': revision, 'targetRevisionId': target}
                     for target in (revision, '', True, False, 0, 1, [], {})]
        controls += [{'requiredRevisionId': required, 'targetRevisionId': None}
                     for required in (None, '', True, False, 0, 1, [], {}, source.revision_id, 'x'*1025)]
        for control in controls:
            bad = copy.deepcopy(response); bad['writeControl'] = control
            with self.subTest(control=control), self.assertRaisesRegex(ProtocolError, 'global_response_revision_unverified'):
                queue.verify_update(packet, bad, readback, f.code)

    def test_nullable_response_still_requires_authenticated_changed_readback_and_exact_prefix(self):
        f = self.f; source = f.source(); path, out = f.plan('join', capacity=2, seconds=1200)
        packet = json.loads(path.read_text()); response = f.google.batch_update_document(**out['tool_arguments'])
        response['writeControl']['targetRevisionId'] = None
        readback = f.google.get_document(source.document_id)
        same_revision = copy.deepcopy(readback); same_revision['revisionId'] = source.revision_id
        missing_event = replace_document_text(copy.deepcopy(readback), queue.block(source.state))
        unauthenticated_state = copy.deepcopy(packet['expected_state'])
        unauthenticated_state['events'][-1]['mac'] = '0'*64
        unauthenticated = replace_document_text(copy.deepcopy(readback), queue.block(unauthenticated_state))
        for bad in (same_revision, missing_event, unauthenticated):
            with self.assertRaises(ProtocolError): queue.verify_update(packet, response, bad, f.code)

    def test_join_heartbeat_group_uses_indexed_batch(self):
        f = self.f; source = f.source()
        joined = queue.transition(source.state, f.code, 'join', 'native',
            {'native_task_id': f.ledger.identity, 'controller_epoch': 'a'*32,
             'lease_expires': int(f.clock)+1200, 'capacity': 2}, now=int(f.clock))
        f.clock += 1
        new = queue.transition(joined, f.code, 'heartbeat', 'native',
            {'native_task_id': f.ledger.identity, 'controller_epoch': 'a'*32}, now=int(f.clock))
        packet = queue.plan_join_heartbeat(source, new, f.code); self.assert_indexed(source, packet)
        response = f.google.batch_update_document(**packet['tool_arguments'])
        self.assertEqual(response['replies'], [{}, {}])
        self.assertEqual(queue.verify_update(packet, response, f.google.get_document(source.document_id), f.code).state, new)

    def test_claim_begin_group_uses_indexed_batch(self):
        f = self.f; f.join(latency=0); route = f.demand()
        self.planned('claim-begin', route_id=route)

    def test_heartbeat_claim_begin_group_uses_indexed_batch(self):
        f = self.f; f.join(latency=0); route = f.demand(); f.clock += 25
        source = f.source(); path = f.path('due-claim-begin')
        out = f.ledger.plan_claim_begin(source, route, path, heartbeat_if_due=True)
        packet = json.loads(path.read_text()); self.assert_indexed(source, packet)
        self.assertEqual(packet['group_kind'], 'heartbeat-claim-begin')
        self.assertTrue(f.commit(path, out)['verified'])

    def test_heartbeat_admitted_group_uses_indexed_batch(self):
        f = self.f; f.join(latency=0); route = f.demand(); f.event('claim-begin', route_id=route)
        path = f.path('spawn'); out = f.ledger.plan_spawn(f.source(), route, path, f.package)
        f.ledger.record_spawn(path, out['arguments'], {'task_name': '/root/offline/'+out['arguments']['task_name']}, f.path('receipt'))
        f.clock += 1; self.planned('heartbeat-admitted', route_id=route)

    def test_authenticated_large_histories_use_end_exclusive_range_without_search_text(self):
        f = self.f; state = f.initial
        state = queue.transition(state, f.code, 'join', 'native',
            {'native_task_id': f.ledger.identity, 'controller_epoch': 'b'*32,
             'lease_expires': int(f.clock)+1700, 'capacity': 2}, now=int(f.clock))
        arguments = {'native_task_id': f.ledger.identity, 'controller_epoch': 'b'*32}
        for minimum in (24660, 75000):
            while len(queue.block(state)) < minimum:
                f.clock += 1
                state = queue.transition(state, f.code, 'heartbeat', 'native', arguments, now=int(f.clock))
            text = queue.block(state); f.google.docs[f.initial['document_id']] = [text, 400+state['epoch']]
            source = f.source(); f.clock += 1
            new = queue.transition(state, f.code, 'heartbeat', 'native', arguments, now=int(f.clock))
            packet = queue.plan(source, new, f.code); self.assert_indexed(source, packet)
            before = f.google.docs[source.document_id][1]
            response = f.google.batch_update_document(**packet['tool_arguments'])
            self.assertEqual(f.google.docs[source.document_id][1], before+1)
            self.assertEqual(response['replies'], [{}, {}])
            self.assertEqual(queue.verify_update(packet, response, f.google.get_document(source.document_id), f.code).state, new)
            state = new

    def test_exact_saved_plan_rejects_range_location_request_order_and_legacy_shape(self):
        f = self.f; source = f.source(); path, _ = f.plan('join', capacity=2, seconds=1200)
        packet = json.loads(path.read_text())
        changes = [lambda p: p['tool_arguments']['requests'].reverse(),
                   lambda p: p['tool_arguments']['requests'][0]['deleteContentRange']['range'].update(startIndex=2),
                   lambda p: p['tool_arguments']['requests'][0]['deleteContentRange']['range'].update(endIndex=1+len(source.text)),
                   lambda p: p['tool_arguments']['requests'][0]['deleteContentRange']['range'].update(tabId='other'),
                   lambda p: p['tool_arguments']['requests'][1]['insertText']['location'].update(index=2),
                   lambda p: p['tool_arguments']['requests'][1]['insertText']['location'].update(tabId='other'),
                   lambda p: p['tool_arguments']['requests'][1]['insertText'].update(text=queue.block(p['expected_state'])),
                   lambda p: p['tool_arguments'].update(write_control={'targetRevisionId': source.revision_id}),
                   lambda p: p['tool_arguments']['write_control'].update(targetRevisionId=None),
                   lambda p: p['tool_arguments'].update(requests=[{'replaceAllText': {
                       'containsText': {'text': source.text[:-1], 'matchCase': True, 'searchByRegex': False},
                       'replaceText': queue.block(p['expected_state'])[:-1], 'tabsCriteria': {'tabIds': ['t.0']}}}])]
        for change in changes:
            bad = copy.deepcopy(packet); change(bad)
            with self.assertRaises(ProtocolError): queue.validate_plan(bad, f.code)

    def test_lost_reply_requires_authenticated_new_revision_and_burns_replay(self):
        f = self.f; source = f.source(); path, out = f.plan('join', capacity=2, seconds=1200)
        packet = json.loads(path.read_text()); f.google.lose = True
        with self.assertRaises(TimeoutError): f.google.batch_update_document(**out['tool_arguments'])
        calls = len(f.google.calls); readback = f.google.get_document(source.document_id)
        same_revision = copy.deepcopy(readback); same_revision['revisionId'] = source.revision_id
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            queue.verify_update(packet, None, same_revision, f.code)
        wrong = replace_document_text(copy.deepcopy(readback), queue.block(source.state))
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            queue.verify_update(packet, None, wrong, f.code)
        self.assertTrue(f.ledger.verify_plan(path, None, readback)['reconciled_from_event'])
        self.assertEqual(len(f.google.calls), calls)
        with self.assertRaisesRegex(ProtocolError, 'already_issued'):
            f.ledger.plan_event(f.source(), 'join', f.path('retry'), capacity=2, seconds=1200)


if __name__ == '__main__': unittest.main()
