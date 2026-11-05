"""Credential-free errors at the PostgreSQL boundary."""


class DatabaseError(RuntimeError):
    code = 'database_error'


class DatabaseConfigurationError(DatabaseError, ValueError):
    code = 'database_configuration'


class DatabaseDependencyError(DatabaseError):
    code = 'database_dependency'


class DatabaseUnavailable(DatabaseError):
    code = 'database_unavailable'


class SchemaMismatch(DatabaseError):
    code = 'schema_mismatch'


class DataCorruption(DatabaseError):
    code = 'data_corruption'


class AuthenticationError(DatabaseError):
    code = 'invalid_key'


class IdentityRequired(DatabaseError):
    code = 'identity_required'


class SpaceNotFound(DatabaseError):
    code = 'space_not_found'


class AccountNotFound(DatabaseError):
    code = 'account_not_found'


class KeyNotFound(DatabaseError):
    code = 'key_not_found'


class DatabaseConstraintError(DatabaseError):
    code = 'database_constraint'


class VersionConflict(DatabaseError):
    code = 'version_conflict'


class MessageConflict(DatabaseError):
    code = 'message_conflict'


class CommitOutcomeUnknown(DatabaseUnavailable):
    code = 'commit_outcome_unknown'

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__('Commit outcome could not be verified')


class ErasureDependencies(DatabaseError):
    code = 'erasure_dependencies'

    def __init__(self, records):
        self.records = tuple(records)
        super().__init__('Additional records require explicit erasure authorization')
