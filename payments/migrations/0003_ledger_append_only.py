from django.db import migrations

CREATE = """
CREATE FUNCTION payments_ledger_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'payments_ledgerentry is append-only (% blocked)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER payments_ledgerentry_immutable
    BEFORE UPDATE OR DELETE ON payments_ledgerentry
    FOR EACH ROW EXECUTE FUNCTION payments_ledger_immutable();
"""

DROP = """
DROP TRIGGER payments_ledgerentry_immutable ON payments_ledgerentry;
DROP FUNCTION payments_ledger_immutable();
"""


class Migration(migrations.Migration):
    """The Python-level guard on LedgerEntry is bypassable (raw SQL, queryset.update()); this isn't."""

    dependencies = [("payments", "0002_deadletter")]
    operations = [migrations.RunSQL(CREATE, reverse_sql=DROP)]
