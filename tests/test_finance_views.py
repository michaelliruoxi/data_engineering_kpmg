"""Opt-in finance reader acceptance on rollback-only local PostgreSQL fixtures.

Set SEC_DB_TEST=1 and SEC_DB_TEST_DATABASE to a migrated dedicated *_test
database. These tests never select a production database or modify preferences
except in the explicit-preference fixture, which is rolled back with all data.
"""

from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import os
from types import SimpleNamespace

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
import pytest

from sec_pipeline import database as db
from sec_pipeline.catalog import ProcessingSetInput, select_preferred_processing_set, store_processing_set
from sec_pipeline.source_cells_v3 import fact_reference
from sec_pipeline.source_set_storage import store_source_occurrences
from sec_pipeline.table_cells import store_table_cells
from tests.test_catalog_postgres import local_test_options
from tests.test_source_set_postgres import database_fixture as source_fixture, seal as seal_source


GAAP = "http://fasb.org/us-gaap/2026"
USD = "{http://www.xbrl.org/2003/iso4217}USD"
EUR = "{http://www.xbrl.org/2003/iso4217}EUR"
SHARES = "{http://www.xbrl.org/2003/instance}shares"
MONEY = {"numerator": [USD], "denominator": []}
PER_SHARE = {"numerator": [USD], "denominator": [SHARES]}
START, END = date(2026, 1, 1), date(2026, 9, 30)


@pytest.fixture
def finance_connection():
    if os.environ.get("SEC_DB_TEST") != "1":
        pytest.skip("Dedicated local PostgreSQL checks are opt-in.")
    conn = psycopg.connect(**local_test_options())
    try:
        with conn.transaction(force_rollback=True):
            if conn.execute("SELECT to_regclass('finance.metric_value')").fetchone()[0] is None:
                raise RuntimeError("Migrate the dedicated local test database through migration 010 first.")
            yield conn
    finally:
        conn.close()


def legacy_fixture(conn, number=9910):
    cik = f"{number:010d}"
    company = db.register_company(conn, db.CompanyInput(cik=cik, name="Finance reader fixture", ticker="FTEST"))
    filing = db.register_filing(conn, db.FilingInput(
        company_id=company, accession_number=f"{cik}-26-000001", form="10-Q",
        filing_date=date(2026, 11, 1), reporting_period_end=END))
    source = db.register_source_document(conn, db.SourceDocumentInput(
        filing_id=filing, role="extracted_xbrl_instance",
        source_url=f"https://example.invalid/finance/{number}/filing.xml",
        local_path=f"finance-fixture-{number}/filing.xml", sha256=f"{number:064x}",
        size_bytes=100, retrieved_at=datetime(2026, 11, 2, tzinfo=timezone.utc)))
    return SimpleNamespace(filing=filing, company=company, cik=cik, source=source)


def add_fact(conn, item, concept, value, *, key=None, version="facts-v1", start=START, end=END,
             instant=False, unit=None, dimensions=None, issuer=None, namespace=GAAP, nil=False):
    key = key or concept
    record = db.FactInput(
        filing_id=item.filing, source_document_id=item.source, extraction_version=version,
        occurrence_key=key, concept_namespace=namespace, concept_name=concept,
        numeric_value=None if nil else Decimal(str(value)), raw_value=None if nil else str(value),
        is_nil=nil, unit=MONEY if unit is None else unit, dimensions=dimensions or [],
        period_kind="instant" if instant else "duration",
        instant_date=end if instant else None, period_start=None if instant else start,
        period_end=None if instant else end,
        source_reference={"element_id": key, "entity": {
            "scheme": "http://www.sec.gov/CIK", "identifier": item.cik if issuer is None else issuer}})
    return db.store_fact(conn, record)


def seal_legacy(conn, item, *, version="facts-v1", key="finance-fixture", status="passed", created_at=None):
    record = ProcessingSetInput(
        filing_id=item.filing, set_key=key, fact_source_document_id=item.source,
        fact_extraction_version=version,
        coverage={"facts": "complete", "tables": "absent", "reports": "absent", "chunks": "absent"},
        validation_status=status, validation={"checks": {"fixture": True}} if status == "passed" else {},
        configuration={"fixture": True})
    if created_at is None:
        return store_processing_set(conn, record)
    # An explicit insertion timestamp makes latest-version selection deterministic
    # within one transaction, where PostgreSQL now() is otherwise constant.
    values = asdict(record) | {"created_at": created_at}
    columns = list(values)
    query = sql.SQL("INSERT INTO sec.filing_processing_sets ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns))
    return conn.execute(query, [Jsonb(values[k]) if isinstance(values[k], dict) else values[k] for k in columns]).fetchone()[0]


def rows(conn, query, params=()):
    cursor = conn.execute(query, params)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def metric(conn, filing, key, *, start=START, end=END, currency="USD"):
    found = rows(conn, """SELECT * FROM finance.metric_value
        WHERE filing_id=%s AND metric_key=%s AND period_start IS NOT DISTINCT FROM %s
          AND period_end=%s AND currency=%s""", (filing, key, start, end, currency))
    assert len(found) == 1, (key, start, end, currency, found)
    return found[0]


def add_source_revenue(conn, item):
    template = item.facts[1]
    record = replace(template, occurrence_key="FinanceRevenue", concept_name="Revenues",
                     numeric_value=Decimal("123456789.25"), raw_value="123456789.25",
                     period_kind="duration", instant_date=None, period_start=START, period_end=END,
                     source_reference=template.source_reference | {"element_id": "FinanceRevenue"})
    fact_id = db.store_fact(conn, record)
    base = item.occurrence_inputs[1]
    occurrence = replace(base, fact_id=fact_id, inline_id=record.occurrence_key,
                         source_xpath="/html/body/*[name()='ix:nonfraction'][2]",
                         evidence=fact_reference(record) | {
                             "xml_id": record.occurrence_key, "inline_mapped_value": record.raw_value,
                             "inline_is_nil": False, "html_sha256": base.evidence["html_sha256"],
                             "xml_sha256": base.evidence["xml_sha256"],
                             "inline_xpath": "/html/body/ix:nonfraction[2]"})
    store_source_occurrences(conn, [occurrence])
    return fact_id


def test_default_selects_newest_passed_legacy_with_facts_without_writing_preference(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    add_fact(conn, item, "Revenues", 10, version="facts-old")
    add_fact(conn, item, "Revenues", 20, version="facts-new")
    add_fact(conn, item, "Revenues", 999, version="facts-failed")
    old = seal_legacy(conn, item, version="facts-old", key="old", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = seal_legacy(conn, item, version="facts-new", key="new", created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    failed = seal_legacy(conn, item, version="facts-failed", key="failed", status="failed",
                         created_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    available = rows(conn, "SELECT processing_family,processing_set_id FROM finance.filing_version WHERE filing_id=%s", (item.filing,))
    assert {(r["processing_family"], r["processing_set_id"]) for r in available} == {
        ("legacy", old), ("legacy", new), ("legacy", failed)}
    chosen = rows(conn, "SELECT * FROM finance.filing WHERE filing_id=%s", (item.filing,))
    assert len(chosen) == 1 and chosen[0]["processing_set_id"] == new
    assert chosen[0]["processing_family"] == "legacy" and chosen[0]["validation_status"] == "passed"
    assert chosen[0]["selection_reason"]
    assert metric(conn, item.filing, "revenue")["value"] == Decimal("20")
    assert conn.execute("SELECT count(*) FROM sec.preferred_processing_sets WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 0


def test_scalar_values_preserve_precision_duplicates_and_instant_duration_separation(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    first = add_fact(conn, item, "Revenues", "123456789.25", key="revenue-1")
    second = add_fact(conn, item, "Revenues", "123456789.25", key="revenue-2")
    add_fact(conn, item, "CostOfRevenue", 0)
    add_fact(conn, item, "OperatingIncomeLoss", "21000000.25")
    add_fact(conn, item, "NetIncomeLoss", "17000000.50")
    add_fact(conn, item, "EarningsPerShareDiluted", "1.23", unit=PER_SHARE)
    add_fact(conn, item, "CashAndCashEquivalentsAtCarryingValue", 9000000, instant=True)
    add_fact(conn, item, "Assets", 30000000, instant=True)
    add_fact(conn, item, "Liabilities", 12000000, instant=True)
    add_fact(conn, item, "StockholdersEquity", 18000000, instant=True)
    selection = seal_legacy(conn, item)
    revenue = metric(conn, item.filing, "revenue")
    assert revenue["value"] == Decimal("123456789.25") and revenue["status"] == "reported"
    assert set(revenue["fact_ids"]) == {first, second} and revenue["concept_names"] == ["Revenues"]
    income = rows(conn, "SELECT * FROM finance.income_statement WHERE filing_id=%s", (item.filing,))
    assert len(income) == 1
    assert (income[0]["revenue"], income[0]["cost_of_revenue"], income[0]["operating_income"],
            income[0]["net_income"], income[0]["eps_diluted"]) == (
                Decimal("123456789.25"), Decimal("0"), Decimal("21000000.25"), Decimal("17000000.50"), Decimal("1.23"))
    assert income[0]["processing_set_id"] == selection and income[0]["metric_status"]["revenue"] == "reported"
    balance = rows(conn, "SELECT * FROM finance.balance_sheet WHERE filing_id=%s", (item.filing,))
    assert len(balance) == 1 and balance[0]["period_end"] == END
    assert metric(conn, item.filing, "total_assets", start=None)["period_start"] is None
    assert (balance[0]["cash_and_equivalents"], balance[0]["total_assets"],
            balance[0]["total_liabilities"], balance[0]["shareholders_equity"]) == tuple(map(Decimal, (9000000, 30000000, 12000000, 18000000)))
    assert balance[0]["period_id"] != income[0]["period_id"]


def test_three_month_and_nine_month_values_with_same_end_do_not_merge(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    add_fact(conn, item, "Revenues", 300, key="nine-month")
    add_fact(conn, item, "Revenues", 110, key="quarter", start=date(2026, 7, 1))
    seal_legacy(conn, item)
    result = rows(conn, "SELECT period_id,period_start,revenue FROM finance.income_statement WHERE filing_id=%s ORDER BY period_start", (item.filing,))
    assert [(r["period_start"], r["revenue"]) for r in result] == [(START, Decimal(300)), (date(2026, 7, 1), Decimal(110))]
    assert len({r["period_id"] for r in result}) == 2


def test_period_identity_is_shared_without_merging_different_filing_values(finance_connection):
    conn = finance_connection
    first, second = legacy_fixture(conn, 9910), legacy_fixture(conn, 9912)
    for item, value in ((first, 100), (second, 200)):
        add_fact(conn, item, "Revenues", value)
        seal_legacy(conn, item)
    first_value, second_value = metric(conn, first.filing, "revenue"), metric(conn, second.filing, "revenue")
    assert first_value["period_id"] == second_value["period_id"]
    assert (first_value["value"], second_value["value"]) == (Decimal(100), Decimal(200))
    assert conn.execute("SELECT count(*) FROM finance.period WHERE period_id=%s", (first_value["period_id"],)).fetchone()[0] == 1


def test_conflicting_aliases_are_null_with_all_evidence_not_summed_or_arbitrarily_chosen(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    ids = {add_fact(conn, item, "Revenues", 100),
           add_fact(conn, item, "SalesRevenueNet", 101)}
    seal_legacy(conn, item)
    result = metric(conn, item.filing, "revenue")
    assert result["value"] is None and result["status"] == "conflict"
    assert set(result["fact_ids"]) == ids
    assert set(result["concept_names"]) == {"Revenues", "SalesRevenueNet"}


def test_contract_revenue_without_top_line_statement_evidence_is_not_total_revenue(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    contract = add_fact(conn, item, "RevenueFromContractWithCustomerExcludingAssessedTax", 75)
    add_fact(conn, item, "OperatingIncomeLoss", 10)
    seal_legacy(conn, item)
    revenue = metric(conn, item.filing, "revenue")
    assert revenue["value"] is None and revenue["status"] == "missing"
    assert conn.execute("SELECT eligible,exclusion_reason FROM finance.metric_evidence WHERE fact_id=%s", (contract,)).fetchone() == (
        False, "unverified_total_revenue")


@pytest.mark.parametrize("heading,expected", [("Total revenue", Decimal(75)), ("Product revenue", None)])
def test_contract_revenue_requires_a_verified_top_line_not_just_any_table_link(finance_connection, heading, expected):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    key = "contract-revenue"
    fact_id = add_fact(conn, item, "RevenueFromContractWithCustomerExcludingAssessedTax", 75, key=key)
    add_fact(conn, item, "OperatingIncomeLoss", 10)
    html_id = db.register_source_document(conn, db.SourceDocumentInput(
        filing_id=item.filing, role="main_inline_xbrl_filing",
        source_url="https://example.invalid/finance/9910/filing.htm", local_path="finance-fixture-9910/filing.htm",
        sha256="a" * 64, size_bytes=100, retrieved_at=datetime(2026, 11, 2, tzinfo=timezone.utc)))
    scope = {"filing_id": str(item.filing), "xml_document_id": str(item.source), "facts_version": "facts-v1"}
    reference = {
        "xml_id": key, "concept": "{" + GAAP + "}RevenueFromContractWithCustomerExcludingAssessedTax",
        "raw_value": "75", "is_nil": False, "unit": MONEY, "dimensions": [],
        "period": {"startDate": START.isoformat(), "endDate": END.isoformat()},
        "scope": scope, "method": "inline_id", "inline_xpath": "/html/body/table/tr/td/ix:nonfraction"}
    cell = {
        "cell_id": "r000c000", "row": 0, "column": 0, "rowspan": 1, "colspan": 1,
        "display_text": "75", "source_xpath": "/html/body/table/tr/td", "source_html_id": None,
        "row_heading": heading, "column_headings": ["Nine months ended September 30, 2026"],
        "column_period": reference["period"], "classification": "resolved",
        "fact_references": [reference], "unresolved_reason": None, "is_total": heading == "Total revenue"}
    table = db.FinancialTableInput(
        filing_id=item.filing, source_document_id=html_id, extraction_version="finance-table-v1",
        table_key="income", statement_type="income_statement", readable_text=heading + " 75",
        structure={"schema_version": 1, "fact_scope": scope, "rows": [[cell]], "inventory": {"resolved": 1}})
    table_id = db.store_financial_table(conn, table)
    store_table_cells(conn, table_id, table, {key: fact_id})
    store_processing_set(conn, ProcessingSetInput(
        filing_id=item.filing, set_key="finance-top-line", fact_source_document_id=item.source,
        fact_extraction_version="facts-v1", table_source_document_id=html_id, table_extraction_version=table.extraction_version,
        coverage={"facts": "complete", "tables": "partial", "reports": "absent", "chunks": "absent"},
        validation_status="passed", validation={"checks": {"fixture": True}}, configuration={}))
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    revenue = metric(conn, item.filing, "revenue")
    assert revenue["value"] == expected
    assert revenue["status"] == ("reported" if expected is not None else "missing")
    assert conn.execute("SELECT eligible FROM finance.metric_evidence WHERE fact_id=%s", (fact_id,)).fetchone()[0] is (expected is not None)


def test_ineligible_facts_stay_out_of_consolidated_metrics_with_reasons(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    good = add_fact(conn, item, "Revenues", 100, key="consolidated")
    rejected = {
        add_fact(conn, item, "Revenues", 5, key="segment", dimensions=[{"axis": "SegmentAxis", "member": "SegmentMember"}]),
        add_fact(conn, item, "Revenues", 6, key="other-issuer", issuer="0000009911"),
        add_fact(conn, item, "Revenues", 7, key="shares-not-money", unit={"numerator": [SHARES], "denominator": []}),
        add_fact(conn, item, "Revenues", 8, key="per-share-not-money", unit=PER_SHARE),
        add_fact(conn, item, "Revenues", 9, key="wrong-period", instant=True),
    }
    add_fact(conn, item, "Revenues", 999, key="custom-tag", namespace="https://example.invalid/taxonomy")
    seal_legacy(conn, item)
    result = metric(conn, item.filing, "revenue")
    assert result["value"] == Decimal(100) and result["fact_ids"] == [good]
    evidence = rows(conn, "SELECT fact_id,eligible,exclusion_reason FROM finance.metric_evidence WHERE filing_id=%s", (item.filing,))
    excluded = {r["fact_id"]: r for r in evidence if not r["eligible"]}
    assert rejected <= excluded.keys()
    assert all(excluded[identifier]["exclusion_reason"] for identifier in rejected)


def test_nil_and_missing_values_remain_null_while_reported_zero_is_valid(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    add_fact(conn, item, "Revenues", None, nil=True)
    add_fact(conn, item, "CostOfRevenue", 0)
    seal_legacy(conn, item)
    for key in ("revenue", "operating_income", "net_income", "eps_diluted"):
        result = metric(conn, item.filing, key)
        assert result["value"] is None and result["status"] == "missing"
    cost = metric(conn, item.filing, "cost_of_revenue")
    assert cost["value"] == Decimal(0) and cost["status"] == "reported"


def test_free_cash_flow_requires_same_period_currency_and_complete_inputs(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    operating = add_fact(conn, item, "NetCashProvidedByUsedInOperatingActivities", "120.25")
    capex = add_fact(conn, item, "PaymentsToAcquirePropertyPlantAndEquipment", "20.10")
    add_fact(conn, item, "NetCashProvidedByUsedInOperatingActivities", 50, key="quarter-cfo", start=date(2026, 7, 1))
    add_fact(conn, item, "PaymentsToAcquirePropertyPlantAndEquipment", 4, key="euro-quarter-capex", start=date(2026, 7, 1),
             unit={"numerator": [EUR], "denominator": []})
    seal_legacy(conn, item)
    fcf = metric(conn, item.filing, "free_cash_flow")
    assert fcf["value"] == Decimal("100.15") and fcf["status"] == "derived"
    assert set(fcf["fact_ids"]) == {operating, capex}
    for currency in ("USD", "EUR"):
        incomplete = metric(conn, item.filing, "free_cash_flow", start=date(2026, 7, 1), currency=currency)
        assert incomplete["value"] is None and incomplete["status"] == "missing"
    wide = rows(conn, "SELECT * FROM finance.cash_flow_statement WHERE filing_id=%s AND period_start=%s AND currency='USD'", (item.filing, START))
    assert len(wide) == 1 and wide[0]["free_cash_flow"] == Decimal("100.15")


def test_negative_capex_and_dividends_do_not_produce_trusted_amounts(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    add_fact(conn, item, "NetCashProvidedByUsedInOperatingActivities", 100)
    add_fact(conn, item, "PaymentsToAcquirePropertyPlantAndEquipment", -10)
    add_fact(conn, item, "PaymentsOfDividends", -5)
    seal_legacy(conn, item)
    for key in ("capital_expenditures", "dividends_paid", "free_cash_flow"):
        assert metric(conn, item.filing, key)["value"] is None


def test_total_debt_requires_every_component_in_identical_instant_and_currency(finance_connection):
    conn, item = finance_connection, legacy_fixture(finance_connection)
    ids = {add_fact(conn, item, name, amount, instant=True) for name, amount in (
        ("ShortTermBorrowings", 10), ("LongTermDebtCurrent", 20), ("LongTermDebtNoncurrent", 70))}
    add_fact(conn, item, "LongTermDebtNoncurrent", 65, key="prior-debt", instant=True, end=date(2025, 9, 30))
    seal_legacy(conn, item)
    debt = metric(conn, item.filing, "total_debt_excluding_leases", start=None)
    assert debt["value"] == Decimal(100) and debt["status"] == "derived" and set(debt["fact_ids"]) == ids
    incomplete = metric(conn, item.filing, "total_debt_excluding_leases", start=None, end=date(2025, 9, 30))
    assert incomplete["value"] is None and incomplete["status"] == "missing"


@pytest.mark.parametrize("review", [False, True])
def test_source_family_default_preserves_review_status_and_never_falls_back_silently(finance_connection, review):
    conn = finance_connection
    item = source_fixture(conn, review=review)
    revenue = add_source_revenue(conn, item)
    source_id = seal_source(conn, item)
    legacy = SimpleNamespace(filing=item.filing, source=item.sources["xml"])
    legacy_id = seal_legacy(conn, legacy)
    chosen = rows(conn, "SELECT * FROM finance.filing WHERE filing_id=%s", (item.filing,))
    assert len(chosen) == 1 and chosen[0]["processing_set_id"] == source_id
    assert chosen[0]["processing_family"] == "source"
    assert chosen[0]["validation_status"] == ("review_required" if review else "passed")
    versions = rows(conn, "SELECT processing_family,processing_set_id FROM finance.filing_version WHERE filing_id=%s", (item.filing,))
    assert {(r["processing_family"], r["processing_set_id"]) for r in versions} == {("source", source_id), ("legacy", legacy_id)}
    result = metric(conn, item.filing, "revenue")
    assert result["fact_ids"] == [revenue]
    if review:
        assert result["value"] is None and result["status"] == "review_required"
        assert conn.execute("SELECT count(*) FROM finance.metric_value WHERE filing_id=%s AND value IS NOT NULL", (item.filing,)).fetchone()[0] == 0
    else:
        assert result["value"] == Decimal("123456789.25") and result["status"] == "reported"
    assert conn.execute("SELECT count(*) FROM sec.preferred_processing_sets WHERE filing_id=%s", (item.filing,)).fetchone()[0] == 0
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_explicit_preferred_legacy_is_respected_and_explained(finance_connection):
    conn = finance_connection
    item = source_fixture(conn, review=False)
    add_source_revenue(conn, item)
    source_id = seal_source(conn, item)
    legacy_id = seal_legacy(conn, SimpleNamespace(filing=item.filing, source=item.sources["xml"]))
    select_preferred_processing_set(conn, item.filing, legacy_id)
    before = conn.execute("SELECT * FROM sec.preferred_processing_sets WHERE filing_id=%s", (item.filing,)).fetchall()
    chosen = rows(conn, "SELECT * FROM finance.filing WHERE filing_id=%s", (item.filing,))[0]
    assert chosen["processing_family"] == "legacy" and chosen["processing_set_id"] == legacy_id
    assert chosen["processing_set_id"] != source_id and "prefer" in chosen["selection_reason"]
    assert metric(conn, item.filing, "revenue")["processing_set_id"] == legacy_id
    assert conn.execute("SELECT * FROM sec.preferred_processing_sets WHERE filing_id=%s", (item.filing,)).fetchall() == before


def test_reader_can_select_all_finance_views_without_schema_or_data_write_privileges(finance_connection):
    conn = finance_connection
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='sec_reader'").fetchone() is None:
        pytest.skip("The dedicated database has no sec_reader role to verify.")
    expected = {
        "company", "filing_version", "filing", "statement", "filing_report", "filing_section", "filing_chunk",
        "metric_definition", "metric_evidence", "period", "metric_value", "income_statement", "balance_sheet",
        "cash_flow_statement",
    }
    actual = {row[0] for row in conn.execute("SELECT viewname FROM pg_views WHERE schemaname='finance'").fetchall()}
    assert actual == expected
    assert conn.execute("""SELECT has_schema_privilege('sec_reader','finance','USAGE'),
        has_schema_privilege('sec_reader','finance','CREATE')""").fetchone() == (True, False)
    for view in sorted(expected):
        relation = "finance." + view
        assert conn.execute("SELECT has_table_privilege('sec_reader',%s,'SELECT')", (relation,)).fetchone()[0]
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not conn.execute("SELECT has_table_privilege('sec_reader',%s,%s)", (relation, privilege)).fetchone()[0], (
                relation, privilege)
    # Exercise effective role access, including every view's underlying joins.
    # A failing SELECT rolls this savepoint back and restores the prior role.
    with conn.transaction():
        conn.execute("SET LOCAL ROLE sec_reader")
        for view in sorted(expected):
            conn.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier("finance", view))).fetchall()
        conn.execute("SET LOCAL ROLE NONE")
