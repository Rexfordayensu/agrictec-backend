"""Transaction provenance, private evidence and append-only review history.

Existing entries are preserved. Legacy duplicate references are explicitly flagged; only those
legacy rows are exempt from the unique index, so references and financial values stay intact.
Downgrade removes the additive metadata, retaining financing rows as they are (0009 had no
record-type constraint), and never rewrites transaction amounts, references or types.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

STATUSES = "'self_reported','evidence_submitted','verification_pending','verified','rejected','disputed'"
INCOME = "'crop_sale','livestock_sale','produce_delivery','other_farm_income'"
EXPENSE = "'fertilizer','seed','equipment','transport','labour','irrigation','land_rent','other_farm_expense'"
FINANCING = "'loan_disbursement','loan_repayment','interest_payment','other_financing'"


def upgrade():
    for table in ("farm_records", "momo_transactions"):
        for col in (
            sa.Column("category", sa.String(40)),
            sa.Column("currency", sa.String(3), nullable=False, server_default="GHS"),
            sa.Column("source_type", sa.String(32), nullable=False, server_default="farmer_entry"),
            sa.Column("verification_status", sa.String(32), nullable=False, server_default="self_reported"),
            sa.Column("external_reference", sa.String(120)),
            sa.Column("payment_method", sa.String(80)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        ):
            op.add_column(table, col)
        op.create_check_constraint(f"ck_{table}_source", table, "source_type IN ('farmer_entry','statement_upload')")
        op.create_check_constraint(f"ck_{table}_status", table, f"verification_status IN ({STATUSES})")
        op.create_check_constraint(f"ck_{table}_currency", table, "currency = 'GHS'")
        op.create_index(
            f"ix_{'farm_records' if table == 'farm_records' else 'momo'}_user_status",
            table,
            ["user_id", "verification_status"],
        )
    op.add_column("farm_records", sa.Column("direction", sa.String(3)))
    op.create_check_constraint(
        "ck_farm_records_type", "farm_records", "type IN ('income','expense','activity','financing')"
    )
    op.create_check_constraint(
        "ck_farm_records_category",
        "farm_records",
        f"category IS NULL OR (type='income' AND category IN ({INCOME})) OR (type='expense' AND category IN ({EXPENSE})) OR (type='financing' AND category IN ({FINANCING}))",
    )
    op.create_check_constraint(
        "ck_farm_records_financing_direction",
        "farm_records",
        "(type='financing' AND direction IS NOT NULL AND direction IN ('in','out')) OR (type<>'financing' AND direction IS NULL)",
    )
    # Existing activities are kept unchanged; the API enforces zero for new/edited activities.
    op.add_column("momo_transactions", sa.Column("notes", sa.Text()))
    op.add_column(
        "momo_transactions", sa.Column("reference_duplicate", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("momo_transactions", "reference", nullable=True)
    op.create_check_constraint(
        "ck_momo_transactions_category",
        "momo_transactions",
        f"category IS NULL OR category IN ({INCOME},{EXPENSE},{FINANCING})",
    )
    op.execute("""WITH ranked AS (
        SELECT id, row_number() OVER (PARTITION BY user_id, upper(reference) ORDER BY created_at, id) n
        FROM momo_transactions WHERE reference IS NOT NULL AND reference <> ''
    ) UPDATE momo_transactions m SET reference_duplicate=true FROM ranked r WHERE m.id=r.id AND r.n>1""")
    op.create_index(
        "ux_momo_user_reference",
        "momo_transactions",
        ["user_id", sa.text("upper(reference)")],
        unique=True,
        postgresql_where=sa.text("reference IS NOT NULL AND reference <> '' AND NOT reference_duplicate"),
    )
    op.create_table(
        "transaction_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_key", sa.String(512), sa.ForeignKey("stored_files.key"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "sha256", name="uq_evidence_user_hash"),
        sa.CheckConstraint("kind IN ('receipt','statement','other')", name="ck_evidence_kind"),
    )
    op.create_table(
        "transaction_evidence_links",
        sa.Column(
            "evidence_id", sa.String(64), sa.ForeignKey("transaction_evidence.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("record_kind", sa.String(4), primary_key=True),
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.CheckConstraint("record_kind IN ('farm','momo')", name="ck_evidence_link_kind"),
    )
    op.create_index("ix_evidence_record", "transaction_evidence_links", ["record_kind", "record_id"])
    op.create_table(
        "transaction_verifications",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("record_kind", sa.String(4), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        # No cascading foreign key: immutable history survives entry/account deletion.
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(8), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_name", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("record_kind IN ('farm','momo')", name="ck_transaction_history_kind"),
        sa.CheckConstraint("actor_type IN ('farmer','admin','system')", name="ck_transaction_history_actor"),
        sa.CheckConstraint(
            f"to_status IN ({STATUSES}) AND (from_status IS NULL OR from_status IN ({STATUSES}))",
            name="ck_transaction_history_status",
        ),
    )
    op.create_index("ix_transaction_history_record", "transaction_verifications", ["record_kind", "record_id", "at"])
    op.execute("""CREATE FUNCTION transaction_history_read_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Transaction verification history is append-only'; END; $$""")
    op.execute(
        "CREATE TRIGGER transaction_history_no_change BEFORE UPDATE OR DELETE ON transaction_verifications FOR EACH ROW EXECUTE FUNCTION transaction_history_read_only()"
    )
    op.execute(
        "CREATE TRIGGER transaction_history_no_truncate BEFORE TRUNCATE ON transaction_verifications FOR EACH STATEMENT EXECUTE FUNCTION transaction_history_read_only()"
    )
    # A polymorphic link has no ordinary FK to two tables. Enforce its owner and target in
    # PostgreSQL too, including during restores, so a malformed backup cannot cross-link users.
    op.execute("""CREATE FUNCTION check_transaction_evidence_link() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE evidence_owner text; record_owner text; BEGIN
        SELECT user_id INTO evidence_owner FROM transaction_evidence WHERE id=NEW.evidence_id;
        IF NEW.record_kind='farm' THEN
            SELECT user_id INTO record_owner FROM farm_records WHERE id=NEW.record_id;
        ELSE
            SELECT user_id INTO record_owner FROM momo_transactions WHERE id=NEW.record_id;
        END IF;
        IF evidence_owner IS NULL OR record_owner IS NULL OR evidence_owner<>record_owner THEN
            RAISE EXCEPTION 'Evidence must belong to the transaction owner';
        END IF;
        RETURN NEW; END; $$""")
    op.execute("CREATE TRIGGER evidence_link_owner BEFORE INSERT OR UPDATE ON transaction_evidence_links FOR EACH ROW EXECUTE FUNCTION check_transaction_evidence_link()")
    # Every legacy entry starts with an honest baseline, without inventing a verification.
    for table, kind in (("farm_records", "farm"), ("momo_transactions", "momo")):
        op.execute(f"""INSERT INTO transaction_verifications
            (id,record_kind,record_id,user_id,from_status,to_status,actor_type,actor_id,actor_name,reason,at)
            SELECT md5('{kind}:' || id), '{kind}', id, user_id, NULL, 'self_reported', 'system',
            'migration-0010', 'AgriCtec', 'Existing farmer entry; no verification inferred.', now() FROM {table}""")
    # Cleanup covers single deletes, bulk clears, and account cascades in the same transaction.
    op.execute("""CREATE FUNCTION cleanup_transaction_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE kind text; BEGIN
        kind := CASE WHEN TG_TABLE_NAME='farm_records' THEN 'farm' ELSE 'momo' END;
        DELETE FROM transaction_evidence_links WHERE record_kind=kind AND record_id=OLD.id;
        DELETE FROM transaction_evidence e WHERE e.user_id=OLD.user_id AND NOT EXISTS
            (SELECT 1 FROM transaction_evidence_links l WHERE l.evidence_id=e.id);
        RETURN OLD; END; $$""")
    for table in ("farm_records", "momo_transactions"):
        op.execute(
            f"CREATE TRIGGER {table}_evidence_cleanup AFTER DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION cleanup_transaction_evidence()"
        )
    op.execute("""CREATE FUNCTION cleanup_evidence_file() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN DELETE FROM stored_files WHERE key=OLD.file_key; RETURN OLD; END; $$""")
    op.execute(
        "CREATE TRIGGER evidence_file_cleanup AFTER DELETE ON transaction_evidence FOR EACH ROW EXECUTE FUNCTION cleanup_evidence_file()"
    )


def downgrade():
    for table in ("farm_records", "momo_transactions"):
        op.execute(f"DROP TRIGGER {table}_evidence_cleanup ON {table}")
    op.execute("DROP FUNCTION cleanup_transaction_evidence()")
    op.execute("DROP TRIGGER evidence_file_cleanup ON transaction_evidence")
    op.execute("DROP FUNCTION cleanup_evidence_file()")
    op.execute("DROP TRIGGER evidence_link_owner ON transaction_evidence_links")
    op.execute("DROP FUNCTION check_transaction_evidence_link()")
    op.drop_table("transaction_evidence_links")
    op.drop_table("transaction_evidence")
    # Evidence payloads are left in stored_files on downgrade, to avoid destroying uploads.
    op.drop_table("transaction_verifications")
    op.execute("DROP FUNCTION transaction_history_read_only()")
    op.drop_index("ux_momo_user_reference", table_name="momo_transactions")
    for table in ("farm_records", "momo_transactions"):
        for name in ("source", "status", "currency", "category"):
            op.drop_constraint(f"ck_{table}_{name}", table, type_="check")
        op.drop_index(f"ix_{'farm_records' if table == 'farm_records' else 'momo'}_user_status", table_name=table)
        for name in (
            "category",
            "currency",
            "source_type",
            "verification_status",
            "external_reference",
            "payment_method",
            "updated_at",
        ):
            op.drop_column(table, name)
    op.drop_constraint("ck_farm_records_type", "farm_records", type_="check")
    op.drop_constraint("ck_farm_records_financing_direction", "farm_records", type_="check")
    op.drop_column("farm_records", "direction")
    op.drop_column("momo_transactions", "notes")
    op.drop_column("momo_transactions", "reference_duplicate")
    # 0009 represented missing references as empty strings (never invent a reference).
    op.execute("UPDATE momo_transactions SET reference='' WHERE reference IS NULL")
    op.alter_column("momo_transactions", "reference", nullable=False)
