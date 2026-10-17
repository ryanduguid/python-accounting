"""Posting must never commit only part of a transaction."""
from datetime import datetime
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, func, select

from python_accounting.database.session import get_session
from python_accounting.models import Account, Base, Currency, Entity, Ledger, LineItem
from python_accounting.transactions import JournalEntry


class TestPostingAtomicity(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = get_session(self.engine)
        entity = Entity(name="Synthetic atomicity fixture")
        self.session.add(entity)
        self.session.commit()
        currency = Currency(name="Synthetic currency", code="XTS", entity_id=entity.id)
        self.session.add(currency)
        self.session.commit()
        self.accounts = [
            Account(name=name, account_type=Account.AccountType.INVENTORY,
                    currency_id=currency.id, entity_id=entity.id)
            for name in ("Synthetic debit", "Synthetic credit")
        ]
        self.session.add_all(self.accounts)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def transaction(self, compound):
        transaction = JournalEntry(
            narration="Synthetic atomic posting",
            transaction_date=datetime(datetime.today().year, 6, 15),
            account_id=self.accounts[1].id,
            entity_id=self.session.entity.id,
            main_account_amount=Decimal("100") if compound else Decimal("0"),
            compound=compound,
        )
        self.session.add(transaction)
        self.session.commit()
        for _ in range(1 if compound else 2):
            line = LineItem(narration="Synthetic line", account_id=self.accounts[0].id,
                            amount=Decimal("100"), credited=False,
                            entity_id=self.session.entity.id)
            self.session.add(line)
            self.session.flush()
            transaction.line_items.add(line)
        self.session.commit()
        return transaction

    def test_late_failure_leaves_no_durable_ledgers(self):
        for compound in (True, False):
            with self.subTest(compound=compound):
                transaction = self.transaction(compound)
                transaction_id = transaction.id
                original = Ledger.get_hash
                calls = 0

                def fail_late(ledger, connection):
                    nonlocal calls
                    calls += 1
                    if calls == (2 if compound else 3):
                        raise RuntimeError("Synthetic late posting failure")
                    return original(ledger, connection)

                with patch.object(Ledger, "get_hash", fail_late):
                    with self.assertRaisesRegex(RuntimeError, "late posting failure"):
                        transaction.post(self.session)
                self.session.rollback()
                with self.engine.connect() as connection:
                    count = connection.scalar(select(func.count()).select_from(Ledger)
                                              .where(Ledger.transaction_id == transaction_id))
                self.assertEqual(count, 0)

    def test_between_flush_failure_cannot_be_committed_later(self):
        for compound in (True, False):
            for error_type in (RuntimeError, KeyboardInterrupt):
                with self.subTest(compound=compound, error_type=error_type):
                    transaction = self.transaction(compound)
                    transaction_id = transaction.id
                    target, name = ((transaction, "get_compound_entries") if compound
                                    else (Ledger, "_transaction_ledgers"))
                    original = getattr(target, name)
                    calls = 0

                    def fail_second(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise error_type("Synthetic between-flush failure")
                        return original(*args, **kwargs)

                    with patch.object(target, name, side_effect=fail_second):
                        with self.assertRaisesRegex(error_type, "between-flush"):
                            Ledger.post(self.session, transaction)
                    self.assertEqual(calls, 2)
                    self.session.commit()
                    with self.engine.connect() as connection:
                        count = connection.scalar(select(func.count()).select_from(Ledger)
                                                  .where(Ledger.transaction_id == transaction_id))
                    self.assertEqual(count, 0)

    def test_success_is_durable_balanced_and_hashed(self):
        for compound in (True, False):
            with self.subTest(compound=compound):
                transaction = self.transaction(compound)
                transaction_id = transaction.id
                Ledger.post(self.session, transaction)
                with self.engine.connect() as connection:
                    rows = connection.execute(select(Ledger.__table__).where(
                        Ledger.transaction_id == transaction_id)).all()
                self.assertEqual(len(rows), 2 if compound else 4)
                self.assertEqual(sum(row.amount if row.entry_type.value == "Debit"
                                     else -row.amount for row in rows), Decimal("0"))
                self.assertTrue(all(row.hash for row in rows))
                self.session.expire_all()
                committed = self.session.get(JournalEntry, transaction_id)
                self.assertTrue(committed.is_secure(self.session))
