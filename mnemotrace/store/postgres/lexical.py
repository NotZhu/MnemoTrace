"""Maintain versioned lexical projections with the existing tokenizer."""
from collections import Counter

from ...retrieve.hybrid import date_terms
from ...retrieve.lexical import lexical_terms
from .projection import projection_id
from .records import FACT, SCOPE, TARGET_COLUMNS

ANALYZER = 'unicode-v2'


def project_document(sql, kind, record_id, version, text, *, field='text', missing_only=False):
    column = TARGET_COLUMNS[kind]
    document_id = projection_id('lex_', f'{kind.value}:{field}:{record_id}')
    if missing_only and sql.run(f'SELECT 1 FROM mnemotrace.lexical_documents WHERE {SCOPE} '
                                'AND id = %s AND content_version = %s AND analyzer_version = %s',
                                (*sql.scope, document_id, version, ANALYZER)).fetchone():
        return
    sql.delete('lexical_documents', f'{column} = %s AND field = %s', (record_id, field))
    if text is None:
        return
    terms = Counter(lexical_terms(text))
    sql.insert('lexical_documents', {
        'id': document_id, column: record_id, 'target_kind': kind.value, 'field': field,
        'content_version': version, 'analyzer_version': ANALYZER, 'document_length': terms.total(),
    })
    if terms:
        sql.run("""INSERT INTO mnemotrace.lexical_terms(user_id,space_id,document_id,term,tf)
            SELECT %s,%s,%s,term,tf FROM unnest(%s::text[],%s::int[]) AS input(term,tf)""",
            (*sql.scope, document_id, list(terms), list(terms.values())))


def project_text(sql, kind, record_id, version, values, *, missing_only=False):
    if kind == FACT:
        text = f"{values['text']} {date_terms(values['valid_at'])}"
    else:
        return
    project_document(sql, kind, record_id, version, text, missing_only=missing_only)
