"""Le comparateur de métadonnées partagé (tests/test_migration_0004_drop_company_analyses.py,
_compare_metadata / _without_pr1_changes) ne masque aucun écart de schéma
non attendu (R1 PR 1).

Il ne retire que des écarts exactement nommés (users.level_revision, tables
et index de 0016 / 0017), uniquement pour les révisions PR 1 non encore
appliquées, et il échoue si l'un manque ou si un écart PR 1 subsiste après
l'application de sa révision. Tout autre écart (autre colonne, autre table,
autre index, suppression, modification) est rendu tel quel.
"""
import pytest
import sqlalchemy as sa

from core.db import Base
from tests.test_migration_0002_analysis_sessions import (
    _reset_schema,
    _run_alembic,
    pg_engine,  # noqa: F401 — fixture
    pg_url,  # noqa: F401 — fixture
)
from tests.test_migration_0004_drop_company_analyses import (
    PR1_0015,
    PR1_0016,
    PR1_0016_TABLES,
    PR1_0017,
    PR1_0017_INDEXES,
    PR1_0017_TABLES,
    R1D1,
    _compare_metadata,
    PR2_0018,
    PR2_0018_INDEXES,
    PR2_0018_TABLES,
    PR4_0019,
    PR7_0020,
    PR9B1_0021,
    V0T08_0022,
    V0T08_0022_INDEXES,
    V0T08_0022_TABLES,
    V0T08_0023,
    V0T08_0023_INDEXES,
    V0T08_0023_TABLES,
    V0T08_0024,
    V0T08_0024_EXISTING_UNIQUES,
    V0T08_0024_INDEXES,
    V0T08_0024_TABLES,
    _raw_compare_metadata,
    _without_pr1_changes,
)

TABLES = Base.metadata.tables
# R1 PR 9-B1 (0021) : UNIQUE composite ajoutée à conversation_identities.
OWNER_UNIQUE = next(c for c in TABLES["conversation_identities"].constraints
                    if isinstance(c, sa.UniqueConstraint) and c.name == "uq_conversation_identities_key_owner")
INDEXES = {i.name: i for t in TABLES.values() for i in t.indexes}
# V0-T08 Lot 2B-2 bloc A (0024) : UNIQUE composites ajoutées à des tables
# analytiques existantes.
UNIQUES_0024 = {c.name: c for t in TABLES.values() for c in t.constraints
                if isinstance(c, sa.UniqueConstraint) and c.name in V0T08_0024_EXISTING_UNIQUES}
# Objets inattendus construits HORS de Base.metadata (un Index lié à une
# colonne de Base.metadata s'attacherait à la vraie table).
_SHADOW = sa.Table("auth_operations", sa.MetaData(), sa.Column("kind", sa.String()))


def _expected(applied):
    diff = []
    if applied < 1:
        diff.append(("add_column", None, "users", TABLES["users"].c.level_revision))
    if applied < 2:
        diff += [("add_table", TABLES[t]) for t in PR1_0016_TABLES]
    if applied < 3:
        diff += [("add_table", TABLES[t]) for t in PR1_0017_TABLES] + [("add_index", INDEXES[i])
                                                                      for i in sorted(PR1_0017_INDEXES)]
    if applied == 3:
        diff.append(("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.blocked_until))
    if applied < 4:
        diff += [("add_table", TABLES[t]) for t in PR2_0018_TABLES] + [("add_index", INDEXES[i])
                                                                      for i in sorted(PR2_0018_INDEXES)]
    if 3 <= applied < 5:
        diff.append(("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action))
    if applied < 6:
        diff.append(("add_column", None, "portfolio_positions", TABLES["portfolio_positions"].c.position_revision))
    if applied < 7:
        diff += [("add_table", TABLES["decryptage_turn_reservations"]), ("add_constraint", OWNER_UNIQUE)]
    if applied < 8:  # V0-T08 Lot 1 (0022) : trois tables et leurs index.
        diff += [("add_table", TABLES[t]) for t in V0T08_0022_TABLES] + [("add_index", INDEXES[i])
                                                                        for i in sorted(V0T08_0022_INDEXES)]
    if applied < 9:  # V0-T08 Lot 2B-1 (0023) : quatre tables et leurs index.
        diff += [("add_table", TABLES[t]) for t in V0T08_0023_TABLES] + [("add_index", INDEXES[i])
                                                                        for i in sorted(V0T08_0023_INDEXES)]
    if applied < 10:  # V0-T08 Lot 2B-2 bloc A (0024) : deux tables, leurs index, trois UNIQUE composites.
        diff += [("add_table", TABLES[t]) for t in V0T08_0024_TABLES] + [("add_index", INDEXES[i])
                                                                        for i in sorted(V0T08_0024_INDEXES)]
        # Chaque UNIQUE n'est un écart propre que si sa table existe déjà
        # (0022 pour les événements, 0023 pour sessions et interactions).
        diff += [("add_constraint", UNIQUES_0024[name]) for name, (_, _, created)
                 in sorted(V0T08_0024_EXISTING_UNIQUES.items()) if applied >= created]
    return diff


UNEXPECTED = [
    ("add_column", None, "users", sa.Column("level_revision_shadow", sa.BigInteger())),
    ("add_column", None, "portfolio_positions", sa.Column("level_revision", sa.BigInteger())),
    ("remove_column", None, "users", sa.Column("level_revision", sa.BigInteger())),
    ("add_table", TABLES["analysis_sessions"]),
    ("remove_table", TABLES["auth_operations"]),
    ("add_index", sa.Index("ix_auth_operations_shadow", _SHADOW.c.kind)),
    ("remove_index", INDEXES["ix_auth_operations_user_id"]),
    [("modify_nullable", None, "auth_operations", "kind", {}, False, True)],
    ("add_fk", TABLES["oryx_sessions"].foreign_key_constraints.copy().pop()),
]


@pytest.mark.parametrize("applied", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_only_exact_pending_pr1_changes_are_removed(applied):
    assert _without_pr1_changes(_expected(applied) + UNEXPECTED, applied) == UNEXPECTED


@pytest.mark.parametrize("applied", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_a_missing_expected_pr1_change_fails(applied):
    expected = _expected(applied)
    for index in range(len(expected)):
        with pytest.raises(AssertionError):
            _without_pr1_changes(expected[:index] + expected[index + 1:], applied)


@pytest.mark.parametrize("applied", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_a_pr1_change_after_its_revision_fails(applied):
    for leftover in _expected(applied - 1)[: len(_expected(applied - 1)) - len(_expected(applied))]:
        with pytest.raises(AssertionError):
            _without_pr1_changes(_expected(applied) + [leftover], applied)


def test_at_head_nothing_is_removed():
    assert _without_pr1_changes(UNEXPECTED, 10) == UNEXPECTED
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_table", TABLES["oryx_sessions"])], 10)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_table", TABLES["auth_abuse_key_state"])], 10)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action)], 10)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_column", None, "portfolio_positions",
                               TABLES["portfolio_positions"].c.position_revision)], 10)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_table", TABLES["decryptage_turn_reservations"])], 10)
    with pytest.raises(AssertionError):
        _without_pr1_changes([("add_constraint", OWNER_UNIQUE)], 10)
    # V0-T08 Lot 1 : une fois 0022 appliquée, ni ses tables ni ses index ne sont masqués.
    for table in V0T08_0022_TABLES:
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_table", TABLES[table])], 10)
    for index in sorted(V0T08_0022_INDEXES):
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_index", INDEXES[index])], 10)
    # V0-T08 Lot 2B-1 : une fois 0023 appliquée, ni ses tables ni ses index ne sont masqués.
    for table in V0T08_0023_TABLES:
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_table", TABLES[table])], 10)
    for index in sorted(V0T08_0023_INDEXES):
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_index", INDEXES[index])], 10)
    # V0-T08 Lot 2B-2 bloc A : à la tête (0024), ni ses tables, ni ses index,
    # ni ses UNIQUE composites ne sont masqués.
    for table in V0T08_0024_TABLES:
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_table", TABLES[table])], 10)
    for index in sorted(V0T08_0024_INDEXES):
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_index", INDEXES[index])], 10)
    for name in sorted(UNIQUES_0024):
        with pytest.raises(AssertionError):
            _without_pr1_changes([("add_constraint", UNIQUES_0024[name])], 10)


def test_the_0024_uniques_are_removed_only_when_their_table_exists_and_exact():
    """V0-T08 Lot 2B-2 bloc A : une UNIQUE de 0024 n'est retirée comme écart
    propre (add_constraint) que si 0024 est en attente ET que sa table existe
    déjà (sinon elle arrive avec la table) ; une contrainte homonyme sur une
    autre table ou d'autres colonnes n'est jamais masquée."""
    assert sorted(UNIQUES_0024) == sorted(V0T08_0024_EXISTING_UNIQUES)
    assert _without_pr1_changes(_expected(8), 8) == [] and _without_pr1_changes(_expected(9), 9) == []
    events = ("add_constraint", UNIQUES_0024["uq_product_activity_events_attribution_identity"])
    sessions = ("add_constraint", UNIQUES_0024["uq_product_activity_sessions_owner"])
    interactions = ("add_constraint", UNIQUES_0024["uq_product_activity_session_interactions_origin"])
    for premature, applied in ((events, 7), (sessions, 8), (interactions, 8)):
        with pytest.raises(AssertionError):
            _without_pr1_changes(_expected(applied) + [premature], applied)
    shadow = sa.Table("product_activity_sessions", sa.MetaData(), sa.Column("id", sa.Uuid()),
                      sa.Column("campaign_id", sa.Uuid()), sa.Column("environment_digest", sa.String()))
    wrong_columns = sa.UniqueConstraint(shadow.c.id, shadow.c.campaign_id, shadow.c.environment_digest,
                                        name="uq_product_activity_sessions_owner")
    other = sa.Table("product_activity_events", sa.MetaData(), sa.Column("id", sa.Uuid()),
                     sa.Column("campaign_id", sa.Uuid()), sa.Column("user_id", sa.String()))
    wrong_table = sa.UniqueConstraint(other.c.id, other.c.campaign_id, other.c.user_id,
                                      name="uq_product_activity_sessions_owner")
    for impostor in (wrong_columns, wrong_table):
        # La vraie contrainte est retirée, l'imposteur homonyme reste visible.
        assert _without_pr1_changes(_expected(9) + [("add_constraint", impostor)], 9) == [("add_constraint",
                                                                                           impostor)]


def test_blocked_until_is_removed_only_as_a_pending_0018_column():
    column = ("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.blocked_until)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(2) + [column], 2)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(4) + [column], 4)


def test_http_action_is_removed_only_as_a_pending_0019_column():
    column = ("add_column", None, "auth_abuse_events", TABLES["auth_abuse_events"].c.http_action)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(2) + [column], 2)  # avant 0017 : arrive avec la table
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(5) + [column], 5)
    assert _without_pr1_changes(_expected(4), 4) == []


def test_position_revision_is_removed_only_as_a_pending_0020_column():
    column = ("add_column", None, "portfolio_positions", TABLES["portfolio_positions"].c.position_revision)
    with pytest.raises(AssertionError):
        _without_pr1_changes(_expected(6) + [column], 6)
    # Une colonne homonyme NULLABLE n'est pas celle de 0020 : jamais masquée.
    nullable = ("add_column", None, "portfolio_positions", sa.Column("position_revision", sa.BigInteger()))
    with pytest.raises(AssertionError):
        _without_pr1_changes([d for d in _expected(5) if d[0] != "add_column"] + [nullable], 5)
    assert _without_pr1_changes(_expected(5), 5) == []


def test_the_0021_objects_are_removed_only_when_pending_and_exact():
    """R1 PR 9-B1 : seule la table de 0021 et la contrainte composite exacte
    (même nom, même table, mêmes colonnes) sont retirées avant 0021."""
    assert _without_pr1_changes(_expected(6), 6) == []
    shadow_table = sa.Table("conversation_identities", sa.MetaData(), sa.Column("conversation_key", sa.String()),
                            sa.Column("user_id", sa.String()), sa.Column("other", sa.String()))
    wrong_columns = sa.UniqueConstraint(shadow_table.c.conversation_key, shadow_table.c.other,
                                        name="uq_conversation_identities_key_owner")
    shadow_other = sa.Table("assistant_deliveries", sa.MetaData(), sa.Column("conversation_key", sa.String()),
                            sa.Column("user_id", sa.String()))
    wrong_table = sa.UniqueConstraint(shadow_other.c.conversation_key, shadow_other.c.user_id,
                                      name="uq_conversation_identities_key_owner")
    for impostor in (wrong_columns, wrong_table):
        with pytest.raises(AssertionError):
            _without_pr1_changes([d for d in _expected(6) if d[0] != "add_constraint"] + [("add_constraint",
                                                                                            impostor)], 6)


# --------------------------------------------------------------------------
# Contre un vrai PostgreSQL : de vraies dérives de schéma restent visibles.
# --------------------------------------------------------------------------

def test_pg_unexpected_drift_before_pr1_is_still_reported(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    assert _compare_metadata(pg_engine) == []
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE portfolio_positions ADD COLUMN drift_column INTEGER"))
        conn.execute(sa.text("CREATE INDEX ix_drift ON users (level)"))
    diff = _compare_metadata(pg_engine)
    assert sorted((d[0], getattr(d[-1], "name", None) or d[1].name) for d in diff) == [
        ("remove_column", "drift_column"), ("remove_index", "ix_drift")]


def test_pg_premature_pr1_object_is_never_hidden(pg_url, pg_engine):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", R1D1)
    with pg_engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN level_revision BIGINT NOT NULL DEFAULT 0"))
    with pytest.raises(AssertionError):
        _compare_metadata(pg_engine)


@pytest.mark.parametrize("revision", [PR1_0015, PR1_0016, PR1_0017, PR2_0018, PR4_0019, PR7_0020, PR9B1_0021,
                                      V0T08_0022, V0T08_0023, V0T08_0024])
def test_pg_missing_pr1_object_after_its_revision_is_reported(pg_url, pg_engine, revision):  # noqa: F811
    _reset_schema(pg_engine)
    _run_alembic(pg_url, "upgrade", revision)
    assert _compare_metadata(pg_engine) == []
    statement = {
        PR1_0015: "ALTER TABLE users DROP COLUMN level_revision",
        PR1_0016: "DROP TABLE portfolio_write_idempotency",
        PR1_0017: "DROP INDEX ix_auth_operations_user_id",
        PR2_0018: "DROP INDEX uq_auth_abuse_events_refusal_bucket",
        PR4_0019: "ALTER TABLE auth_abuse_events DROP COLUMN http_action",
        PR7_0020: "ALTER TABLE portfolio_positions DROP COLUMN position_revision",
        PR9B1_0021: "DROP TABLE decryptage_turn_reservations",
        V0T08_0022: "DROP INDEX ix_product_activity_events_purge_after_at",
        V0T08_0023: "DROP INDEX ix_product_activity_sessions_open_idle_expiry",
        V0T08_0024: "DROP INDEX ix_product_activity_proof_registry_purge_after_at",
    }[revision]
    with pg_engine.begin() as conn:
        conn.execute(sa.text(statement))
    assert _raw_compare_metadata(pg_engine) != []
    with pytest.raises(AssertionError):
        _compare_metadata(pg_engine)
