from datetime import UTC, datetime

import pytest

from weaver.context import ContextDictionary, Page
from weaver.fabric.v2.store import FabricStore
from weaver.schema.v2 import ThreadDraft
from weaver.wiki.compiler import Compiler, EditResult, Summary
from weaver.wiki.store import WikiStore

NOW = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE = 'src_01ARZ3NDEKTSV4RRFFQ69G5FAV'


class FakeLLM:
    def generate_text(self, task, system, user):
        return '본문 ' + user

    def generate_json(self, task, system, user, schema):
        return EditResult(changes=[])


def draft():
    return ThreadDraft(thread_id='one', kind='기록', content='내용', occurred_at=NOW,
                       recorded_at=NOW, tags=['홍길동'], field='status', value='진행',
                       source_id=SOURCE, actor='ai_approved', direction=None, resolves=None,
                       retracts=None, correction=None)


def test_compile_and_bookmark_failure(tmp_path):
    vault = tmp_path / 'vault'
    vault.mkdir()
    store = WikiStore(vault, tmp_path / 'wiki', settle_seconds=0)
    fabric = FabricStore(tmp_path / 'fabric.db')
    context = ContextDictionary(tmp_path / 'context')
    context.sync_from_pages([Page('홍길동', 'person')])
    fabric.append([draft()])
    compiler = Compiler(store, fabric, context, FakeLLM(), tmp_path / 'compiler.db',
                        lambda _: Summary('2025-01-01 홍길동 call', '요약'))
    compiler.run()
    props, body, _ = store.read('pages/홍길동.md')
    assert props['status'] == '진행'
    assert props['kind'] == 'person'
    assert '본문' in body and 'summaries/' in body
    assert compiler.bookmark() == 1
    assert len(fabric.entries(__import__('weaver.fabric.v2.store', fromlist=['ThreadQuery']).ThreadQuery())) == 1

    fabric.append([draft().model_copy(update={'thread_id': 'two'})])
    class Failure(FakeLLM):
        def generate_text(self, task, system, user):
            raise RuntimeError('failure')
    compiler.llm = Failure()
    with pytest.raises(RuntimeError):
        compiler.run()
    assert compiler.bookmark() == 1
