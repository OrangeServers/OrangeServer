"""M2/S2 reviewed knowledge truth and bounded RedisStore retrieval."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy.exc import IntegrityError

from app.ai.autonomy.readiness import autonomy_redis_url
from app.ai.autonomy.repository import sanitize_text
from app.ai.diagnostic_adapters import sanitize_evidence
from app.core import config
from app.tools.basesec import decrypt_secret, encrypt_secret


LOCAL_MODEL = 'BAAI/bge-small-zh-v1.5'
LOCAL_MODEL_DIMENSION = 512
LOCAL_MODEL_ARCHIVE_SHA256 = (
    'bf023219b6029148fddf764d248808816c0ca1f107f058231bb1ae0fa526f83f'
)
CHUNK_SIZE = 400
CHUNK_OVERLAP = 60
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_INDEX_CHUNKS = 20_000
MAX_RESULTS = 8
MAX_CONTEXT_BYTES = 16 * 1024
NAMESPACE = ('ogs', 'knowledge')
STORE_PREFIX = 'ogs_knowledge'
VECTOR_PREFIX = 'ogs_knowledge_vectors'
_HEADING_RE = re.compile(r'(?m)^(#{1,6})\s+(.+?)\s*$')
_SCOPE_RE = re.compile(r'^(?:global|host:[1-9][0-9]*)$')


class KnowledgeError(RuntimeError):
    pass


class KnowledgeValidationError(KnowledgeError):
    pass


class KnowledgeNotFound(KnowledgeError):
    pass


class KnowledgeConflict(KnowledgeError):
    pass


def _fingerprint(provider_type: str, base_url: str, model: str, dimension: int) -> str:
    source = '|'.join((
        provider_type,
        base_url,
        model,
        str(int(dimension)),
        LOCAL_MODEL_ARCHIVE_SHA256 if provider_type == 'local' else '',
    ))
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


LOCAL_MODEL_FINGERPRINT = _fingerprint(
    'local', '', LOCAL_MODEL, LOCAL_MODEL_DIMENSION,
)


def _bounded_text(value: Any, field: str, limit: int) -> str:
    text = sanitize_text(str(value or '')).strip()
    if not text:
        raise KnowledgeValidationError('%s is required' % field)
    if len(text) > limit:
        raise KnowledgeValidationError('%s is too long' % field)
    return text


def _document_content(value: Any) -> str:
    text = sanitize_evidence(value).strip()
    if not text:
        raise KnowledgeValidationError('content is required')
    if len(text.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise KnowledgeValidationError('document exceeds 1 MiB')
    return text


def _document_scope(value: Any) -> str:
    scope = _bounded_text(value or 'global', 'scope', 128)
    if not _SCOPE_RE.fullmatch(scope):
        raise KnowledgeValidationError('scope must be global or host:<id>')
    return scope


def _chunks(content: str, title: str) -> list[dict[str, str]]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        separators=['\n## ', '\n### ', '\n\n', '\n', '。', '；', ' ', ''],
    )
    headings = [
        (match.start(), sanitize_text(match.group(2)).strip()[:128])
        for match in _HEADING_RE.finditer(content)
    ]
    result = []
    cursor = 0
    for text in splitter.split_text(content):
        position = content.find(text, max(0, cursor - CHUNK_OVERLAP * 2))
        if position < 0:
            position = cursor
        cursor = position + len(text)
        heading = title
        for heading_position, heading_text in headings:
            if heading_position > position:
                break
            heading = heading_text or title
        result.append({'text': text, 'heading': heading})
    return result


_local_models: dict[str, Any] = {}


def _threaded(call: Callable[[], Any]) -> Any:
    """Use gevent's own threadpool so ONNX never blocks the WSGI hub."""
    from gevent import get_hub

    return get_hub().threadpool.apply(call)


def _local_embeddings(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding

    model_path = str(config.AI_EMBEDDING_MODEL_PATH or '').strip()
    key = model_path or LOCAL_MODEL
    model = _local_models.get(key)
    if model is None:
        kwargs: dict[str, Any] = {
            'model_name': LOCAL_MODEL,
            'threads': 1,
            'lazy_load': True,
        }
        if model_path:
            if not os.path.isfile(os.path.join(model_path, 'model_optimized.onnx')):
                raise KnowledgeError('local embedding model is unavailable')
            kwargs['specific_model_path'] = model_path
        model = TextEmbedding(**kwargs)
        _local_models[key] = model
    return _threaded(
        lambda: [vector.tolist() for vector in model.embed(texts, batch_size=64)]
    )


def _remote_embeddings(row: Any, texts: list[str]) -> list[list[float]]:
    from openai import OpenAI
    from app.ai.provider_config import _assert_public_destination

    if not row.api_key_ciphertext:
        raise KnowledgeError('remote embedding API key is not configured')
    _assert_public_destination(str(row.base_url or ''))

    def request():
        client = OpenAI(
            api_key=decrypt_secret(row.api_key_ciphertext),
            base_url=str(row.base_url),
            timeout=10.0,
            max_retries=0,
        )
        response = client.embeddings.create(model=str(row.model), input=texts)
        ordered = sorted(response.data, key=lambda item: int(item.index))
        return [list(item.embedding) for item in ordered]

    return _threaded(request)


def _embed(row: Any, texts: Iterable[str]) -> list[list[float]]:
    values = [str(text) for text in texts]
    if not values:
        return []
    vectors = (
        _local_embeddings(values)
        if row.provider_type == 'local'
        else _remote_embeddings(row, values)
    )
    dimension = int(row.dimension)
    if len(vectors) != len(values) or any(len(vector) != dimension for vector in vectors):
        raise KnowledgeError('embedding response dimension mismatch')
    return vectors


@contextmanager
def _redis_store(row: Any, *, reset: bool = False):
    from langgraph.store.redis import RedisStore
    from redis.exceptions import ResponseError

    with RedisStore.from_conn_string(
        autonomy_redis_url(0),
        index={
            'dims': int(row.dimension),
            'embed': lambda texts: _embed(row, texts),
            'fields': ['text'],
        },
        store_prefix=STORE_PREFIX,
        vector_prefix=VECTOR_PREFIX,
    ) as store:
        if reset:
            for index_name in (VECTOR_PREFIX, STORE_PREFIX):
                try:
                    store._redis.execute_command('FT.DROPINDEX', index_name, 'DD')
                except ResponseError as exc:
                    if 'unknown index name' not in str(exc).lower():
                        raise
        store.setup()
        yield store


class KnowledgeService:
    def __init__(
        self,
        session,
        *,
        store_factory: Callable[..., Any] = _redis_store,
    ) -> None:
        self.session = session
        self.store_factory = store_factory

    def _config_row(self, *, create: bool = False, lock: bool = False):
        from app.core.db.database import t_ai_embedding_config

        query = self.session.query(t_ai_embedding_config).filter_by(id=1)
        row = (query.with_for_update() if lock else query).first()
        if row is None and create:
            row = t_ai_embedding_config(
                id=1,
                provider_type='local',
                base_url=None,
                model=LOCAL_MODEL,
                dimension=LOCAL_MODEL_DIMENSION,
                model_fingerprint=LOCAL_MODEL_FINGERPRINT,
                index_state='empty',
                indexed_chunks=0,
            )
            self.session.add(row)
            self.session.flush()
        return row

    @staticmethod
    def _config_dict(row) -> dict[str, Any]:
        if row is None:
            return {
                'provider_type': 'local',
                'base_url': '',
                'model': LOCAL_MODEL,
                'dimension': LOCAL_MODEL_DIMENSION,
                'api_key_configured': False,
                'model_fingerprint': LOCAL_MODEL_FINGERPRINT,
                'indexed_fingerprint': None,
                'index_state': 'empty',
                'indexed_chunks': 0,
                'created_at': None,
                'updated_at': None,
            }
        return {
            'provider_type': row.provider_type,
            'base_url': row.base_url or '',
            'model': row.model,
            'dimension': int(row.dimension),
            'api_key_configured': bool(row.api_key_ciphertext),
            'model_fingerprint': row.model_fingerprint,
            'indexed_fingerprint': row.indexed_fingerprint,
            'index_state': row.index_state,
            'indexed_chunks': int(row.indexed_chunks or 0),
            'created_at': row.created_at,
            'updated_at': row.updated_at,
        }

    def config(self) -> dict[str, Any]:
        return self._config_dict(self._config_row())

    def index_state(self) -> str:
        row = self._config_row()
        return str(row.index_state) if row is not None else 'empty'

    def save_config(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from app.ai.provider_config import _valid_base_url

        row = self._config_row(create=True)
        provider_type = str(payload.get('provider_type', row.provider_type)).strip()
        if provider_type not in {'local', 'openai_compatible'}:
            raise KnowledgeValidationError('unsupported embedding provider')
        if provider_type == 'local':
            base_url = ''
            model = LOCAL_MODEL
            dimension = LOCAL_MODEL_DIMENSION
            row.api_key_ciphertext = None
        else:
            try:
                base_url = _valid_base_url(
                    str(payload.get('base_url', row.base_url or '')),
                )
            except Exception as exc:
                raise KnowledgeValidationError(str(exc)) from exc
            model = _bounded_text(
                payload.get('model', row.model), 'model', 128,
            )
            raw_dimension = payload.get('dimension', row.dimension)
            try:
                if isinstance(raw_dimension, bool):
                    raise ValueError
                dimension = int(raw_dimension)
            except (TypeError, ValueError):
                raise KnowledgeValidationError('dimension must be an integer') from None
            if not 1 <= dimension <= 4096:
                raise KnowledgeValidationError('dimension must be between 1 and 4096')
            api_key = payload.get('api_key')
            if api_key is not None and str(api_key).strip():
                row.api_key_ciphertext = encrypt_secret(str(api_key).strip())
            if not row.api_key_ciphertext:
                raise KnowledgeValidationError('remote embedding API key is required')
        fingerprint = _fingerprint(provider_type, base_url, model, dimension)
        changed = fingerprint != row.model_fingerprint
        row.provider_type = provider_type
        row.base_url = base_url or None
        row.model = model
        row.dimension = dimension
        row.model_fingerprint = fingerprint
        if changed:
            row.index_state = 'stale'
            row.indexed_fingerprint = None
            row.indexed_chunks = 0
        self.session.commit()
        return self._config_dict(row)

    @staticmethod
    def _document_dict(row, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            'id': row.id,
            'title': row.title,
            'source_type': row.source_type,
            'source_ref': row.source_ref,
            'scope': row.scope,
            'content_sha256': row.content_sha256,
            'version': int(row.version),
            'approved': bool(row.approved),
            'indexed': bool(
                row.indexed_fingerprint and int(row.chunk_count or 0) > 0
            ),
            'chunk_count': int(row.chunk_count or 0),
            'created_by': row.created_by,
            'created_at': row.created_at,
            'updated_at': row.updated_at,
        }
        if include_content:
            result['content'] = row.content
        return result

    def list_documents(self) -> list[dict[str, Any]]:
        from app.core.db.database import t_ai_knowledge_document

        rows = self.session.query(t_ai_knowledge_document).order_by(
            t_ai_knowledge_document.updated_at.desc(),
        ).limit(200).all()
        return [self._document_dict(row, include_content=False) for row in rows]

    def _document_row(self, document_id: str):
        from app.core.db.database import t_ai_knowledge_document

        row = self.session.query(t_ai_knowledge_document).filter_by(
            id=str(document_id),
        ).first()
        if row is None:
            raise KnowledgeNotFound('knowledge document not found')
        return row

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self._document_dict(self._document_row(document_id))

    def _mark_stale(self) -> None:
        row = self._config_row(create=True)
        row.index_state = 'stale'
        row.indexed_fingerprint = None
        row.indexed_chunks = 0

    def create_document(self, owner: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from app.core.db.database import t_ai_knowledge_document

        title = _bounded_text(payload.get('title'), 'title', 128)
        content = _document_content(payload.get('content'))
        scope = _document_scope(payload.get('scope'))
        row = t_ai_knowledge_document(
            id=uuid.uuid4().hex,
            title=title,
            source_type='runbook',
            source_ref=None,
            scope=scope,
            content=content,
            content_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
            version=1,
            approved=True,
            indexed_fingerprint=None,
            chunk_count=0,
            created_by=str(owner)[:24],
        )
        self.session.add(row)
        self._mark_stale()
        self.session.commit()
        return self._document_dict(row)

    def update_document(
        self, document_id: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        row = self._document_row(document_id)
        changed = False
        if 'title' in payload:
            title = _bounded_text(payload.get('title'), 'title', 128)
            changed = changed or title != row.title
            row.title = title
        if 'scope' in payload:
            scope = _document_scope(payload.get('scope'))
            changed = changed or scope != row.scope
            row.scope = scope
        if 'content' in payload:
            content = _document_content(payload.get('content'))
            changed = changed or content != row.content
            row.content = content
            row.content_sha256 = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if changed:
            row.version = int(row.version) + 1
            row.indexed_fingerprint = None
            row.chunk_count = 0
            self._mark_stale()
        self.session.commit()
        return self._document_dict(row)

    def delete_document(self, document_id: str) -> None:
        row = self._document_row(document_id)
        self.session.delete(row)
        self._mark_stale()
        self.session.commit()

    def request_reindex(self) -> dict[str, Any]:
        row = self._config_row(create=True, lock=True)
        if row.index_state == 'rebuilding':
            raise KnowledgeConflict('knowledge index is already rebuilding')
        row.index_state = 'rebuilding'
        self.session.commit()
        return self._config_dict(row)

    def mark_index_error(self) -> None:
        row = self._config_row(create=True)
        row.index_state = 'error'
        row.indexed_fingerprint = None
        row.indexed_chunks = 0
        self.session.commit()

    def capture_run(self, owner: str, run_id: str) -> dict[str, Any]:
        from app.core.db.database import (
            t_ai_autonomous_evidence,
            t_ai_autonomous_run,
            t_ai_autonomous_step,
            t_ai_knowledge_document,
        )

        run = self.session.query(t_ai_autonomous_run).filter_by(
            id=str(run_id), owner=str(owner), status='completed', outcome='resolved',
        ).first()
        if run is None:
            raise KnowledgeValidationError(
                'only owned, completed and resolved runs can become knowledge',
            )
        steps = self.session.query(t_ai_autonomous_step).filter_by(
            run_id=run.id,
        ).order_by(t_ai_autonomous_step.seq.asc()).all()
        successful_verifications = {
            row.id for row in steps
            if row.kind == 'verification' and row.status == 'succeeded'
        }
        evidence = self.session.query(t_ai_autonomous_evidence).filter_by(
            run_id=run.id,
        ).order_by(t_ai_autonomous_evidence.created_at.asc()).all()
        if not any(
            row.kind == 'verification_observation'
            and row.step_id in successful_verifications
            for row in evidence
        ):
            raise KnowledgeValidationError(
                'successful independent verification is required',
            )
        lines = [
            '# %s' % sanitize_evidence(run.goal)[:256],
            '',
            '## Final status',
            'resolved',
            '',
            '## Verified observations',
        ]
        lines.extend(
            '- [%s] %s' % (row.kind, sanitize_evidence(row.summary)[:500])
            for row in evidence
        )
        lines.extend(['', '## Executed steps'])
        lines.extend(
            '- #%d %s: %s' % (
                int(row.seq), row.status, sanitize_evidence(row.summary)[:255],
            )
            for row in steps
        )
        content = _document_content('\n'.join(lines))
        document = t_ai_knowledge_document(
            id=uuid.uuid4().hex,
            title=sanitize_evidence(run.goal).strip()[:128] or 'Verified run',
            source_type='verified_run',
            source_ref=run.id,
            scope='host:%d' % int(run.host_id),
            content=content,
            content_sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
            version=1,
            approved=True,
            indexed_fingerprint=None,
            chunk_count=0,
            created_by=str(owner)[:24],
        )
        self.session.add(document)
        self._mark_stale()
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise KnowledgeConflict('run is already captured as knowledge') from exc
        return self._document_dict(document)

    def reindex(self) -> dict[str, Any]:
        from langgraph.store.base import PutOp
        from app.core.db.database import t_ai_embedding_config, t_ai_knowledge_document

        config_row = self._config_row(create=True)
        snapshot_fingerprint = str(config_row.model_fingerprint)
        config_row.index_state = 'rebuilding'
        self.session.commit()
        try:
            prepared: list[tuple[Any, int, dict[str, Any]]] = []
            chunk_counts: dict[str, int] = {}
            document_manifest: dict[str, tuple[int, str]] = {}
            docs = self.session.query(t_ai_knowledge_document).filter_by(
                approved=True,
            ).order_by(t_ai_knowledge_document.id.asc()).yield_per(20)
            for doc in docs:
                document_manifest[doc.id] = (
                    int(doc.version), str(doc.content_sha256),
                )
                chunks = _chunks(doc.content, doc.title)
                if len(prepared) + len(chunks) > MAX_INDEX_CHUNKS:
                    raise KnowledgeValidationError(
                        'knowledge index exceeds 20000 chunks',
                    )
                chunk_counts[doc.id] = len(chunks)
                prepared.extend(
                    (doc, position, chunk)
                    for position, chunk in enumerate(chunks)
                )
            with self.store_factory(config_row, reset=True) as store:
                for start in range(0, len(prepared), 64):
                    operations = []
                    for doc, position, chunk in prepared[start:start + 64]:
                        operations.append(PutOp(
                            NAMESPACE,
                            '%s:%d:%d' % (doc.id, int(doc.version), position),
                            {
                                'text': chunk['text'],
                                'document_id': doc.id,
                                'version': int(doc.version),
                                'title': doc.title,
                                'source_type': doc.source_type,
                                'source_ref': doc.source_ref or '',
                                'heading': chunk['heading'],
                                'scope': doc.scope,
                            },
                            index=['text'],
                        ))
                    store.batch(operations)
            self.session.expire_all()
            config_row = self.session.query(t_ai_embedding_config).filter_by(
                id=1,
            ).with_for_update().one()
            current_docs = self.session.query(t_ai_knowledge_document).filter_by(
                approved=True,
            ).order_by(t_ai_knowledge_document.id.asc()).all()
            current_manifest = {
                doc.id: (int(doc.version), str(doc.content_sha256))
                for doc in current_docs
            }
            if (
                config_row.index_state != 'rebuilding'
                or str(config_row.model_fingerprint) != snapshot_fingerprint
                or current_manifest != document_manifest
            ):
                config_row.index_state = 'stale'
                config_row.indexed_fingerprint = None
                config_row.indexed_chunks = 0
                self.session.commit()
                return self._config_dict(config_row)
            for doc in current_docs:
                doc.chunk_count = chunk_counts.get(doc.id, 0)
                doc.indexed_fingerprint = snapshot_fingerprint
            config_row.indexed_fingerprint = config_row.model_fingerprint
            config_row.indexed_chunks = len(prepared)
            config_row.index_state = 'ready' if prepared else 'empty'
            self.session.commit()
        except Exception:
            self.session.rollback()
            row = self._config_row(create=True)
            row.index_state = 'error'
            row.indexed_fingerprint = None
            row.indexed_chunks = 0
            self.session.commit()
            raise
        return self._config_dict(config_row)

    def search(
        self,
        query: str,
        *,
        limit: int = MAX_RESULTS,
        scopes: Iterable[str] = ('global',),
    ) -> list[dict[str, Any]]:
        from app.core.db.database import t_ai_knowledge_document

        query = _bounded_text(query, 'query', 512)
        limit = max(1, min(int(limit), MAX_RESULTS))
        allowed_scopes = tuple(dict.fromkeys(
            _document_scope(scope) for scope in scopes
        ))
        if not allowed_scopes:
            return []
        row = self._config_row()
        if row is None or row.index_state != 'ready':
            return []
        with self.store_factory(row, reset=False) as store:
            # ponytail: bounded overfetch avoids a second vector index; add a
            # scoped Redis filter only if cross-scope misses become measurable.
            hits = store.search(NAMESPACE, query=query, limit=MAX_RESULTS * 8)
        document_ids = {
            str((hit.value or {}).get('document_id') or '') for hit in hits
        }
        valid = {
            doc.id: doc
            for doc in self.session.query(t_ai_knowledge_document).filter(
                t_ai_knowledge_document.id.in_(document_ids),
                t_ai_knowledge_document.approved.is_(True),
                t_ai_knowledge_document.scope.in_(allowed_scopes),
            ).all()
        } if document_ids else {}
        result = []
        used_bytes = 0
        for hit in hits:
            value = dict(hit.value or {})
            doc = valid.get(str(value.get('document_id') or ''))
            if (
                doc is None
                or int(value.get('version') or 0) != int(doc.version)
                or doc.indexed_fingerprint != row.model_fingerprint
            ):
                continue
            excerpt = sanitize_text(str(value.get('text') or ''))[:CHUNK_SIZE]
            encoded = len(excerpt.encode('utf-8'))
            if used_bytes + encoded > MAX_CONTEXT_BYTES:
                break
            used_bytes += encoded
            score = float(hit.score) if hit.score is not None else None
            result.append({
                'citation_id': 'K%d' % (len(result) + 1),
                'document_id': doc.id,
                'version': int(doc.version),
                'title': doc.title,
                'source_type': doc.source_type,
                'source_ref': doc.source_ref,
                'heading': sanitize_text(str(value.get('heading') or doc.title))[:128],
                'scope': doc.scope,
                'excerpt': excerpt,
                'score': score,
                'match_reason': 'semantic similarity',
            })
            if len(result) >= limit:
                break
        return result


def prompt_citations(items: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render bounded, provenance-rich references for the planner prompt."""
    lines = []
    used = 0
    for item in items:
        line = '[%s] %s v%s | %s | %s' % (
            item.get('citation_id'), item.get('title'), item.get('version'),
            item.get('heading'), item.get('excerpt'),
        )
        line = sanitize_text(line)
        size = len(line.encode('utf-8'))
        if used + size > MAX_CONTEXT_BYTES:
            break
        used += size
        lines.append(line)
    return lines
