"""Financial fact extraction tests.

Synthetic fixtures cover edge cases absent from the real sample; the sample tests
reconcile the supplied Microsoft filing. Live database tests are opt-in: set
SEC_DB_TEST=1 and prepare the dedicated test database as described in docs/database.md.
"""

from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import UUID, uuid4

from sec_pipeline import database as db
from sec_pipeline import fact_extraction as fx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT_ROOT / "data/raw/sec/0001193125-26-191507"
SAMPLE_XML = SAMPLE / "msft-20260331_htm.xml"
SAMPLE_HTML = SAMPLE / "msft-20260331.htm"
EX = "https://example.invalid/taxonomy/2026"
USD = "{http://www.xbrl.org/2003/iso4217}USD"
SHARES = "{http://www.xbrl.org/2003/instance}shares"
FILING, SOURCE = UUID(int=1), UUID(int=2)

HEAD = f"""<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:link="http://www.xbrl.org/2003/linkbase"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:ex="{EX}">
  <link:schemaRef xlink:type="simple" xlink:href="SCHEMA"/>
"""
ENTITY = '<xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>'
# Root children: schemaRef is /*/*[1], the five contexts follow, then four units, so
# the first fact in a test body is /*/*[11].
DEFINITIONS = f"""
  <xbrli:context id="quarter"><xbrli:entity>{ENTITY}</xbrli:entity>
    <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="ytd"><xbrli:entity>{ENTITY}</xbrli:entity>
    <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="day"><xbrli:entity>{ENTITY}</xbrli:entity>
    <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:context id="always"><xbrli:entity>{ENTITY}</xbrli:entity>
    <xbrli:period><xbrli:forever/></xbrli:period></xbrli:context>
  <xbrli:context id="cloud">
    <xbrli:entity>{ENTITY}<xbrli:segment>
      <xbrldi:explicitMember dimension="ex:ProductAxis">ex:ServerMember</xbrldi:explicitMember>
      <xbrldi:explicitMember dimension="ex:SegmentAxis">ex:CloudMember</xbrldi:explicitMember>
    </xbrli:segment></xbrli:entity>
    <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
    <xbrli:scenario>
      <xbrldi:typedMember dimension="ex:CustomerAxis"><ex:Customer note="a &amp; b">Contoso &lt;East&gt;</ex:Customer></xbrldi:typedMember>
    </xbrli:scenario></xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <xbrli:unit id="eps"><xbrli:divide>
    <xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>
    <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator>
  </xbrli:divide></xbrli:unit>
  <xbrli:unit id="usd_shares"><xbrli:measure>iso4217:USD</xbrli:measure><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
"""


def run(body, *, schema_ref="https://example.invalid/taxonomy/2026/ex.xsd", document=None, files=None, **options):
    """Write a synthetic instance to a temporary folder and extract it."""
    text = document if document is not None else HEAD.replace("SCHEMA", schema_ref) + DEFINITIONS + body + "</xbrli:xbrl>"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "instance.xml"
        path.write_text(text, encoding="utf-8")
        for name, content in (files or {}).items():
            (Path(directory) / name).write_text(content, encoding="utf-8")
        return fx.extract_facts(path, filing_id=FILING, source_document_id=SOURCE, **options)


def by_key(result):
    return {fact.occurrence_key: fact for fact in result.facts}


class ValueTests(unittest.TestCase):
    def test_decimals_negatives_and_zero_stay_exact(self):
        huge = "123456789012345678901234567890.123456789012345678901234567890"
        facts = by_key(run(f"""
            <ex:Assets id="huge" contextRef="day" unitRef="usd" decimals="INF">{huge}</ex:Assets>
            <ex:CashFlow id="negative" contextRef="quarter" unitRef="usd" decimals="-6">-11351000000</ex:CashFlow>
            <ex:Other id="zero" contextRef="quarter" unitRef="usd" decimals="-6">0</ex:Other>
            <ex:ParValue id="small" contextRef="day" unitRef="eps" decimals="INF">
                0.00000625 </ex:ParValue>
            <ex:Rate id="trailing" contextRef="day" unitRef="pure" decimals="2" precision="4">1.50</ex:Rate>
            <ex:Other id="exponent" contextRef="quarter" unitRef="usd">+1.5E3</ex:Other>"""))
        self.assertEqual(facts["huge"].numeric_value, Decimal(huge))
        self.assertEqual(str(facts["huge"].numeric_value), huge)
        self.assertEqual(facts["negative"].numeric_value, Decimal("-11351000000"))
        self.assertEqual(facts["small"].numeric_value, Decimal("0.00000625"))
        self.assertEqual(facts["exponent"].numeric_value, Decimal("1500"))
        for fact in facts.values():
            self.assertIsInstance(fact.numeric_value, Decimal)
        # Zero is a value, not a missing one.
        self.assertEqual((facts["zero"].numeric_value, facts["zero"].is_nil), (Decimal("0"), False))
        # The raw text is kept as written, including whitespace and trailing zeros.
        self.assertEqual(facts["small"].raw_value, "\n                0.00000625 ")
        self.assertEqual((facts["trailing"].raw_value, str(facts["trailing"].numeric_value)), ("1.50", "1.50"))
        self.assertEqual((facts["trailing"].reported_decimals, facts["trailing"].reported_precision), ("2", "4"))
        self.assertEqual((facts["huge"].reported_decimals, facts["exponent"].reported_decimals), ("INF", None))

    def test_nil_text_and_numeric_looking_text_keep_their_meaning(self):
        facts = by_key(run("""
            <ex:Commitments id="nil" contextRef="day" unitRef="usd" xsi:nil="true"/>
            <ex:Remark id="nil_text" contextRef="day" xsi:nil="1"></ex:Remark>
            <ex:IssuanceYear id="year" contextRef="day">2009</ex:IssuanceYear>
            <ex:Cik id="cik" contextRef="always">0000789019</ex:Cik>
            <ex:Remark id="empty" contextRef="day"/>
            <ex:Policy id="html" contextRef="ytd" xml:lang="en-US">&lt;p&gt;Revenue &amp;amp; costs&lt;/p&gt;</ex:Policy>"""))
        nil = facts["nil"]
        self.assertEqual((nil.is_nil, nil.numeric_value, nil.raw_value), (True, None, None))
        self.assertEqual(nil.unit, {"numerator": [USD], "denominator": []})
        self.assertEqual(nil.source_reference["value_source"], "xsi:nil")
        self.assertEqual((facts["nil_text"].is_nil, facts["nil_text"].unit), (True, None))
        # No unitRef means not numeric, however the text looks.
        for key, text in (("year", "2009"), ("cik", "0000789019")):
            self.assertEqual((facts[key].numeric_value, facts[key].raw_value, facts[key].unit), (None, text, None))
        # An empty string is a stated value and differs from nil.
        self.assertEqual((facts["empty"].raw_value, facts["empty"].is_nil), ("", False))
        self.assertEqual(facts["html"].raw_value, "<p>Revenue &amp; costs</p>")
        self.assertEqual(facts["html"].source_reference["lang"], "en-US")

    def test_period_forms_and_overlapping_periods_stay_separate(self):
        facts = by_key(run("""
            <ex:Revenue id="q" contextRef="quarter" unitRef="usd" decimals="-6">82886000000</ex:Revenue>
            <ex:Revenue id="y" contextRef="ytd" unitRef="usd" decimals="-6">241832000000</ex:Revenue>
            <ex:Assets id="i" contextRef="day" unitRef="usd" decimals="-6">694228000000</ex:Assets>
            <ex:Cik id="f" contextRef="always">0000789019</ex:Cik>"""))

        def period(key):
            fact = facts[key]
            return fact.period_kind, fact.period_start, fact.period_end, fact.instant_date

        self.assertEqual(period("q"), ("duration", date(2026, 1, 1), date(2026, 3, 31), None))
        self.assertEqual(period("y"), ("duration", date(2025, 7, 1), date(2026, 3, 31), None))
        self.assertEqual(period("i"), ("instant", None, None, date(2026, 3, 31)))
        self.assertEqual(period("f"), ("forever", None, None, None))
        self.assertNotEqual(facts["q"].numeric_value, facts["y"].numeric_value)

    def test_units_keep_every_measure_with_its_namespace(self):
        facts = by_key(run("""
            <ex:Revenue id="simple" contextRef="quarter" unitRef="usd">1</ex:Revenue>
            <ex:Eps id="ratio" contextRef="quarter" unitRef="eps">4.27</ex:Eps>
            <ex:Product id="product" contextRef="quarter" unitRef="usd_shares">2</ex:Product>
            <ex:Rate id="pure" contextRef="quarter" unitRef="pure">0.30</ex:Rate>"""))
        self.assertEqual(facts["simple"].unit, {"numerator": [USD], "denominator": []})
        self.assertEqual(facts["ratio"].unit, {"numerator": [USD], "denominator": [SHARES]})
        self.assertEqual(facts["product"].unit, {"numerator": [USD, SHARES], "denominator": []})
        self.assertEqual(facts["pure"].unit,
                         {"numerator": ["{http://www.xbrl.org/2003/instance}pure"], "denominator": []})
        self.assertEqual(facts["ratio"].source_reference["unit_ref"], "eps")


class ContextTests(unittest.TestCase):
    def test_explicit_and_typed_dimensions_keep_names_values_and_placement(self):
        fact = run('<ex:Revenue id="d" contextRef="cloud" unitRef="usd">5</ex:Revenue>').facts[0]
        self.assertEqual(fact.dimensions, [
            {"kind": "typed", "dimension": f"{{{EX}}}CustomerAxis",
             "value": f'<ex:Customer xmlns:ex="{EX}" note="a &amp; b">Contoso &lt;East&gt;</ex:Customer>'},
            {"kind": "explicit", "dimension": f"{{{EX}}}ProductAxis", "member": f"{{{EX}}}ServerMember"},
            {"kind": "explicit", "dimension": f"{{{EX}}}SegmentAxis", "member": f"{{{EX}}}CloudMember"},
        ])
        reference = fact.source_reference
        self.assertEqual(reference["dimension_containers"], ["scenario", "segment", "segment"])
        self.assertEqual(reference["context_ref"], "cloud")
        self.assertEqual(reference["entity"], {"scheme": "http://www.sec.gov/CIK", "identifier": "0000000001"})
        self.assertEqual(run('<ex:Revenue id="n" contextRef="day" unitRef="usd">5</ex:Revenue>').facts[0].dimensions, [])

    def test_different_prefixes_for_the_same_namespaces_give_the_same_facts(self):
        body = """
            <ex:Revenue id="a" contextRef="cloud" unitRef="eps" decimals="2">4.27</ex:Revenue>
            <ex:Cik id="b" contextRef="always">0000789019</ex:Cik>"""
        aliased = HEAD.replace("SCHEMA", "x.xsd") + DEFINITIONS + body + "</xbrli:xbrl>"
        # Rename every prefix and make the instance namespace the default one.
        for old, new in (("xbrli:", ""), ("xmlns:xbrli=", "xmlns="), ("xmlns:ex=", "xmlns:t="), ("ex:", "t:"),
                         ("xmlns:iso4217=", "xmlns:cur="), ("iso4217:", "cur:"),
                         ("xmlns:xbrldi=", "xmlns:dim="), ("xbrldi:", "dim:")):
            aliased = aliased.replace(old, new)
        self.assertIn('<measure>shares</measure>', aliased)

        def comparable(result):
            records = []
            for fact in result.facts:
                record = asdict(fact)
                record["source_reference"].pop("qname")  # the only field that records the prefix
                # A typed value keeps the prefix written in the source.
                record["dimensions"] = [d for d in record["dimensions"] if d["kind"] == "explicit"]
                records.append(record)
            return records

        original, renamed = run(body, use_schema_labels=False), run("", document=aliased, use_schema_labels=False)
        self.assertEqual(comparable(original), comparable(renamed))
        self.assertEqual(original.facts[0].source_reference["qname"], "ex:Revenue")
        self.assertEqual(renamed.facts[0].source_reference["qname"], "t:Revenue")
        self.assertEqual(renamed.facts[0].unit, {"numerator": [USD], "denominator": [SHARES]})
        self.assertEqual(renamed.facts[0].concept_namespace, EX)


class OccurrenceTests(unittest.TestCase):
    def test_repeated_occurrences_are_kept_and_keyed_by_id_or_position(self):
        result = run("""
            <ex:Revenue id="statement" contextRef="quarter" unitRef="usd" decimals="-6">82886000000</ex:Revenue>
            <ex:Revenue id="note" contextRef="quarter" unitRef="usd" decimals="-6">82886000000</ex:Revenue>
            <ex:Revenue contextRef="quarter" unitRef="usd" decimals="-6">82886000000</ex:Revenue>""")
        self.assertEqual([fact.occurrence_key for fact in result.facts], ["statement", "note", "xpath:/*/*[13]"])
        self.assertEqual({fact.numeric_value for fact in result.facts}, {Decimal("82886000000")})
        self.assertEqual(result.facts[1].source_reference["element_id"], "note")
        self.assertEqual(result.facts[1].source_reference["xpath"], "/*/*[12]")
        self.assertEqual([fact.source_reference["document_order"] for fact in result.facts], [1, 2, 3])
        self.assertNotIn("element_id", result.facts[2].source_reference)
        self.assertEqual(result.inventory["occurrence_keys_from_xpath"], 1)

    def test_problems_are_reported_with_a_reason_and_never_dropped(self):
        result = run("""
            <ex:Revenue id="ok" contextRef="quarter" unitRef="usd">1</ex:Revenue>
            <ex:Revenue id="no_context" contextRef="missing" unitRef="usd">1</ex:Revenue>
            <ex:Revenue id="no_unit" contextRef="quarter" unitRef="missing">1</ex:Revenue>
            <ex:Revenue id="words" contextRef="quarter" unitRef="usd">N/A</ex:Revenue>
            <ex:Revenue id="grouped" contextRef="quarter" unitRef="usd">1,000</ex:Revenue>
            <ex:Revenue id="infinite" contextRef="quarter" unitRef="usd">INF</ex:Revenue>
            <ex:Revenue id="nan" contextRef="quarter" unitRef="usd">NaN</ex:Revenue>
            <ex:Revenue id="blank" contextRef="quarter" unitRef="usd"> </ex:Revenue>
            <ex:Revenue id="fraction" contextRef="quarter" unitRef="usd"><xbrli:numerator>1</xbrli:numerator><xbrli:denominator>3</xbrli:denominator></ex:Revenue>
            <ex:Revenue id="bad_decimals" contextRef="quarter" unitRef="usd" decimals="six">1</ex:Revenue>
            <ex:Revenue id="bad_nil" contextRef="quarter" unitRef="usd" xsi:nil="maybe">1</ex:Revenue>
            <ex:Revenue id="nil_with_value" contextRef="quarter" unitRef="usd" xsi:nil="true">1</ex:Revenue>
            <ex:Revenue id="twice" contextRef="quarter" unitRef="usd">1</ex:Revenue>
            <ex:Revenue id="twice" contextRef="quarter" unitRef="usd">2</ex:Revenue>""")
        self.assertEqual([fact.occurrence_key for fact in result.facts], ["ok"])
        failures = {(failure.element_id, failure.category) for failure in result.failures}
        self.assertEqual(failures, {
            ("no_context", fx.MALFORMED), ("no_unit", fx.MALFORMED), ("blank", fx.UNSUPPORTED),
            ("words", fx.UNSUPPORTED), ("grouped", fx.UNSUPPORTED), ("infinite", fx.UNSUPPORTED),
            ("nan", fx.UNSUPPORTED), ("fraction", fx.UNSUPPORTED), ("bad_decimals", fx.MALFORMED),
            ("bad_nil", fx.MALFORMED), ("nil_with_value", fx.MALFORMED), ("twice", fx.MALFORMED)})
        self.assertEqual(len(result.failures), 13)
        self.assertTrue(all(failure.reason and failure.xpath.startswith("/*/*[") for failure in result.failures))
        inventory = result.inventory
        self.assertEqual((inventory["fact_occurrences"], inventory["extracted"], inventory["failed"]), (14, 1, 13))
        with self.assertRaisesRegex(fx.FactExtractionError, "13 fact occurrence"):
            result.require_complete()

    def test_unsupported_contexts_fail_their_facts_instead_of_guessing(self):
        contexts = f"""
            <xbrli:context id="timed"><xbrli:entity>{ENTITY}</xbrli:entity>
              <xbrli:period><xbrli:instant>2026-03-31T00:00:00</xbrli:instant></xbrli:period></xbrli:context>
            <xbrli:context id="reversed"><xbrli:entity>{ENTITY}</xbrli:entity>
              <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
            <xbrli:context id="custom"><xbrli:entity>{ENTITY}<xbrli:segment><ex:Division>East</ex:Division></xbrli:segment></xbrli:entity>
              <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>"""
        result = run(contexts + """
            <ex:Assets id="timed" contextRef="timed" unitRef="usd">1</ex:Assets>
            <ex:Assets id="reversed" contextRef="reversed" unitRef="usd">1</ex:Assets>
            <ex:Assets id="custom" contextRef="custom" unitRef="usd">1</ex:Assets>""")
        self.assertEqual(result.facts, [])
        self.assertEqual({failure.element_id: failure.category for failure in result.failures},
                         {"timed": fx.UNSUPPORTED, "reversed": fx.MALFORMED, "custom": fx.UNSUPPORTED})
        self.assertEqual(result.inventory["contexts"], 8)

    def test_output_is_deterministic(self):
        body = """
            <ex:Revenue id="a" contextRef="cloud" unitRef="usd">5</ex:Revenue>
            <ex:Cik contextRef="always">0000789019</ex:Cik>"""
        first, second = run(body), run(body)
        self.assertEqual(first.facts, second.facts)
        self.assertEqual(first.inventory, second.inventory)
        self.assertEqual(first.facts[0].filing_id, FILING)
        self.assertEqual(first.facts[0].source_document_id, SOURCE)
        self.assertEqual(first.facts[0].extraction_version, "facts-v1")
        self.assertEqual(run(body, extraction_version="facts-v2").facts[0].extraction_version, "facts-v2")
        with self.assertRaises(fx.FactExtractionError):
            run(body, extraction_version=" ")


class SourceSafetyTests(unittest.TestCase):
    def test_parsing_never_uses_the_network(self):
        with patch("socket.socket", side_effect=AssertionError("network access attempted")):
            result = run('<ex:Revenue id="a" contextRef="quarter" unitRef="usd">1</ex:Revenue>')
        self.assertEqual(len(result.facts), 1)
        self.assertIsNone(result.facts[0].label)  # the schema is a URL, so it is not read
        self.assertIsNone(result.inventory["labels"]["schema"])

    def test_entities_doctypes_and_non_instances_are_rejected(self):
        entity = ('<!DOCTYPE xbrl [<!ENTITY amount "999">]>'
                  '<xbrl xmlns="http://www.xbrl.org/2003/instance">&amount;</xbrl>')
        for document, message in ((entity, "DOCTYPE"), ("<xbrl><unclosed></xbrl>", "well-formed"),
                                  ("<html/>", "not an XBRL instance")):
            with self.subTest(message=message), self.assertRaisesRegex(fx.FactExtractionError, message):
                run("", document=document)

    def test_labels_come_only_from_the_local_schema_the_instance_names(self):
        schema = f"""<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" targetNamespace="{EX}"
              xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">
            <xsd:annotation><xsd:appinfo><link:linkbase><link:labelLink xlink:type="extended">
              <link:loc xlink:type="locator" xlink:href="ex.xsd#ex_Revenue" xlink:label="rev"/>
              <link:label xlink:type="resource" xlink:label="rev_lbl" xml:lang="en-US"
                  xlink:role="http://www.xbrl.org/2003/role/terseLabel">Sales</link:label>
              <link:label xlink:type="resource" xlink:label="rev_lbl" xml:lang="en-US"
                  xlink:role="http://www.xbrl.org/2003/role/label">Revenue, Net</link:label>
              <link:labelArc xlink:type="arc" xlink:from="rev" xlink:to="rev_lbl"/>
              <link:loc xlink:type="locator" xlink:href="ex.xsd#ex_NetIncome" xlink:label="ni"/>
              <link:label xlink:type="resource" xlink:label="ni_lbl" xml:lang="en-US"
                  xlink:role="http://www.xbrl.org/2003/role/totalLabel">Net income</link:label>
              <link:label xlink:type="resource" xlink:label="ni_lbl" xml:lang="en-US"
                  xlink:role="http://www.xbrl.org/2009/role/negatedLabel">Net loss</link:label>
              <link:labelArc xlink:type="arc" xlink:from="ni" xlink:to="ni_lbl"/>
              <link:loc xlink:type="locator" xlink:href="https://example.invalid/std/std.xsd#std_Assets" xlink:label="as"/>
              <link:label xlink:type="resource" xlink:label="as_lbl" xml:lang="en-US"
                  xlink:role="http://www.xbrl.org/2003/role/label">Total assets</link:label>
              <link:labelArc xlink:type="arc" xlink:from="as" xlink:to="as_lbl"/>
            </link:labelLink></link:linkbase></xsd:appinfo></xsd:annotation>
            <xsd:import namespace="urn:example:std" schemaLocation="https://example.invalid/std/std.xsd"/>
            <xsd:element id="ex_Revenue" name="Revenue"/><xsd:element id="ex_NetIncome" name="NetIncome"/>
          </xsd:schema>"""
        body = """
            <ex:Revenue id="rev" contextRef="quarter" unitRef="usd">1</ex:Revenue>
            <ex:NetIncome id="ni" contextRef="quarter" unitRef="usd">1</ex:NetIncome>
            <std:Assets xmlns:std="urn:example:std" id="assets" contextRef="day" unitRef="usd">1</std:Assets>
            <ex:Unlabeled id="none" contextRef="quarter" unitRef="usd">1</ex:Unlabeled>"""
        result = run(body, schema_ref="ex.xsd", files={"ex.xsd": schema})
        facts = by_key(result)
        self.assertEqual([facts[key].label for key in ("rev", "ni", "assets", "none")],
                         ["Revenue, Net", "Net income", "Total assets", None])
        self.assertEqual(facts["rev"].source_reference["label_source"],
                         {"schema": "ex.xsd", "role": "http://www.xbrl.org/2003/role/label"})
        self.assertEqual(facts["ni"].source_reference["label_source"]["role"],
                         "http://www.xbrl.org/2003/role/totalLabel")
        self.assertNotIn("label_source", facts["none"].source_reference)
        self.assertEqual(result.inventory["labels"]["schema"], "ex.xsd")
        self.assertEqual(result.inventory["labels"]["facts_without_label"], 1)
        # A missing file, a path outside the folder, or a disabled lookup leaves labels empty.
        for options in ({"schema_ref": "ex.xsd"}, {"schema_ref": "../ex.xsd", "files": {"ex.xsd": schema}},
                        {"schema_ref": "ex.xsd", "files": {"ex.xsd": schema}, "use_schema_labels": False}):
            with self.subTest(options=sorted(options)):
                self.assertEqual({fact.label for fact in run(body, **options).facts}, {None})


class StoreFactsTests(unittest.TestCase):
    def test_store_facts_returns_ids_with_their_scope_and_never_commits(self):
        facts = run("""
            <ex:Revenue id="a" contextRef="quarter" unitRef="usd">1</ex:Revenue>
            <ex:Revenue id="b" contextRef="quarter" unitRef="usd">1</ex:Revenue>""").facts
        connection = object()
        stored_ids = {fact.occurrence_key: uuid4() for fact in facts}
        calls = []

        def fake_store(conn, record):
            calls.append((conn, record))
            return stored_ids[record.occurrence_key]

        with patch.object(fx, "store_fact", side_effect=fake_store):
            stored = fx.store_facts(connection, facts)
            self.assertEqual(stored, fx.StoredFacts(
                filing_id=FILING, source_document_id=SOURCE, extraction_version="facts-v1", fact_ids=stored_ids))
            self.assertEqual(calls, [(connection, fact) for fact in facts])
            for invalid in ([], [facts[0], facts[0]], [facts[0], replace(facts[1], extraction_version="facts-v2")],
                            [facts[0], replace(facts[1], filing_id=uuid4())]):
                with self.assertRaises(fx.FactExtractionError):
                    fx.store_facts(connection, invalid)
            self.assertEqual(len(calls), 2)


INLINE = f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
    xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2022-02-16"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ex="{EX}"><body>
  <td>(<ix:nonFraction id="flow" name="ex:CashFlow" contextRef="quarter" unitRef="usd" scale="6"
        decimals="-6" sign="-" format="ixt:num-dot-decimal"><span>11,351</span></ix:nonFraction>)</td>
  <td><ix:nonFraction id="eps" name="ex:Eps" contextRef="quarter" unitRef="eps" decimals="2"
        format="ixt:num-dot-decimal">4.27</ix:nonFraction></td>
  <td><ix:nonFraction id="rate" name="ex:Rate" contextRef="day" unitRef="pure" scale="-2"
        decimals="2" format="ixt:num-dot-decimal">30</ix:nonFraction></td>
  <td><ix:nonFraction id="nil" name="ex:Commitments" contextRef="day" unitRef="usd" xsi:nil="true"/></td>
  <p><ix:nonNumeric id="name" name="ex:Name" contextRef="ytd">EXAMPLE CORPORATION</ix:nonNumeric></p>
  <td><ix:nonFraction id="html_only" name="ex:Other" contextRef="day" unitRef="usd"
        format="ixt:num-dot-decimal">9</ix:nonFraction></td>
</body></html>"""
INLINE_FACTS = """
    <ex:CashFlow id="flow" contextRef="quarter" unitRef="usd" decimals="-6">-11351000000</ex:CashFlow>
    <ex:Eps id="eps" contextRef="quarter" unitRef="eps" decimals="2">4.27</ex:Eps>
    <ex:Rate id="rate" contextRef="day" unitRef="pure" decimals="2">0.30</ex:Rate>
    <ex:Commitments id="nil" contextRef="day" unitRef="usd" xsi:nil="true"/>
    <ex:Name id="name" contextRef="ytd">Example Corporation</ex:Name>"""


class InlineCorrespondenceTests(unittest.TestCase):
    def check(self, facts, html=INLINE):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filing.htm"
            path.write_text(html, encoding="utf-8")
            return fx.check_inline_correspondence(path, facts)

    def test_scale_sign_and_references_agree(self):
        report = self.check(run(INLINE_FACTS).facts)
        self.assertEqual((report["facts"], report["matched"], report["mismatches"]), (5, 5, []))
        self.assertEqual((report["numeric_values_equal"], report["numeric_values_not_compared"]), (3, 0))
        self.assertEqual((report["inline_elements"], report["inline_ids_without_fact"]), (6, ["html_only"]))

    def test_differences_are_listed_by_occurrence(self):
        facts = run(INLINE_FACTS.replace("-11351000000", "11351000000").replace('id="eps" contextRef="quarter"', 'id="eps" contextRef="ytd"')
                    + '<ex:Other id="xml_only" contextRef="day" unitRef="usd">1</ex:Other>').facts
        report = self.check(facts)
        problems = {item["occurrence_key"]: item["problem"] for item in report["mismatches"]}
        self.assertEqual(set(problems), {"flow", "eps", "xml_only"})
        self.assertEqual(problems["flow"], "value: HTML shows -11351000000, XML has 11351000000")
        self.assertEqual(problems["eps"], "contextRef")
        self.assertEqual(report["matched"], 3)
        # An unrecognized number format is counted, not assumed to match.
        other_format = self.check(run(INLINE_FACTS).facts, INLINE.replace("ixt:num-dot-decimal", "ixt:num-comma-decimal"))
        self.assertEqual((other_format["numeric_values_equal"], other_format["numeric_values_not_compared"]), (0, 3))


@unittest.skipUnless(SAMPLE_XML.is_file(), "The Microsoft sample files are not present.")
class MicrosoftSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((PROJECT_ROOT / "source_manifest.json").read_text(encoding="utf-8"))
        cls.result = fx.extract_facts(SAMPLE_XML, filing_id=FILING, source_document_id=SOURCE)
        cls.facts = cls.result.facts

    def select(self, concept, **criteria):
        return [fact for fact in self.facts if fact.concept_name == concept and not fact.dimensions
                and all(getattr(fact, name) == value for name, value in criteria.items())]

    def test_inventory_reconciles_with_the_source(self):
        inventory = self.result.inventory
        expected = {"fact_occurrences": 1622, "extracted": 1622, "failed": 0, "contexts": 409, "units": 6,
                    "with_unit_ref": 1428, "without_unit_ref": 194, "extracted_nil": 2,
                    "extracted_numeric": 1426, "extracted_nonnumeric": 194, "occurrence_keys_from_xpath": 0}
        self.assertEqual({key: inventory[key] for key in expected}, expected)
        self.assertEqual(self.result.failures, [])
        self.assertIs(self.result.require_complete(), self.result)
        xml_record = next(r for r in self.manifest["files"] if r["role"] == fx.XML_ROLE)
        self.assertEqual(inventory["source"]["sha256"], xml_record["sha256"])
        self.assertEqual(inventory["source"]["size_bytes"], xml_record["size_bytes"])
        self.assertEqual(sum(inventory["by_period_kind"].values()), 1622)
        self.assertEqual(sum(inventory["by_unit_ref"].values()), 1622)

    def test_every_occurrence_has_its_own_key_and_source_evidence(self):
        keys = [fact.occurrence_key for fact in self.facts]
        self.assertEqual(len(set(keys)), 1622)
        for fact in self.facts:
            reference = fact.source_reference
            self.assertEqual(fact.occurrence_key, reference["element_id"])
            self.assertRegex(reference["xpath"], r"^/\*/\*\[\d+\]$")
            self.assertTrue(reference["context_ref"].startswith("C_"))
            self.assertEqual(reference["entity"], {"scheme": "http://www.sec.gov/CIK", "identifier": "0000789019"})
            self.assertEqual(len(reference["dimension_containers"]), len(fact.dimensions))
            self.assertEqual("unit_ref" in reference, fact.unit is not None)
        json.dumps([asdict(fact) for fact in self.facts], default=str)  # all JSON fields serialize

    def test_headline_values_periods_and_repeats(self):
        quarter = {"period_start": date(2026, 1, 1), "period_end": date(2026, 3, 31)}
        year_to_date = {"period_start": date(2025, 7, 1), "period_end": date(2026, 3, 31)}
        revenue = "RevenueFromContractWithCustomerExcludingAssessedTax"
        # The same quarterly revenue is tagged in four places; each stays a separate occurrence.
        self.assertEqual([f.numeric_value for f in self.select(revenue, **quarter)], [Decimal("82886000000")] * 4)
        self.assertEqual({f.numeric_value for f in self.select(revenue, **year_to_date)}, {Decimal("241832000000")})
        financing = self.select("NetCashProvidedByUsedInFinancingActivities", **quarter)
        self.assertEqual([(f.numeric_value, f.raw_value, f.reported_decimals) for f in financing],
                         [(Decimal("-11351000000"), "-11351000000", "-6")])
        self.assertEqual(financing[0].unit, {"numerator": [USD], "denominator": []})
        for instant, total in ((date(2026, 3, 31), "694228000000"), (date(2025, 6, 30), "619003000000")):
            for concept in ("Assets", "LiabilitiesAndStockholdersEquity"):
                self.assertEqual([f.numeric_value for f in self.select(concept, instant_date=instant)], [Decimal(total)])
        eps = self.select("EarningsPerShareDiluted", **quarter)[0]
        self.assertEqual((str(eps.numeric_value), eps.unit), ("4.27", {"numerator": [USD], "denominator": [SHARES]}))
        par = self.select("EntityListingParValuePerShare")[0]
        self.assertEqual((str(par.numeric_value), par.reported_decimals), ("0.00000625", "INF"))

    def test_nil_text_units_and_dimensions_from_the_real_filing(self):
        nil = [fact for fact in self.facts if fact.is_nil]
        self.assertEqual([(f.concept_name, f.instant_date, f.numeric_value, f.raw_value) for f in nil],
                         [("CommitmentsAndContingencies", date(2026, 3, 31), None, None),
                          ("CommitmentsAndContingencies", date(2025, 6, 30), None, None)])
        self.assertEqual(sum(1 for fact in self.facts if fact.numeric_value == 0), 87)
        year = next(fact for fact in self.facts if fact.concept_name == "DebtInstrumentIssuanceYear")
        self.assertEqual((year.numeric_value, year.raw_value, year.unit), (None, "2009", None))
        policy = self.select("SignificantAccountingPoliciesTextBlock")[0]
        self.assertEqual(len(policy.raw_value), 29456)
        self.assertTrue(policy.raw_value.startswith("<p style="))
        euro = [fact for fact in self.facts if fact.unit and fact.unit["numerator"] == ["{http://www.xbrl.org/2003/iso4217}EUR"]]
        self.assertEqual([(f.concept_name, f.numeric_value) for f in euro], [("DebtInstrumentFaceAmount", Decimal("4100000000"))])
        self.assertEqual(euro[0].dimensions, [{
            "kind": "explicit", "dimension": "{http://fasb.org/us-gaap/2025}DebtInstrumentAxis",
            "member": "{http://www.microsoft.com/20260331}IssuanceOfLongTermDebtSixMember"}])
        typed = [d for fact in self.facts for d in fact.dimensions if d["kind"] == "typed"]
        self.assertEqual(len(typed), 4)
        self.assertTrue(all(d["value"].endswith(">2026-04-01</us-gaap:RevenueRemainingPerformanceObligationExpectedTimingOfSatisfactionStartDateAxis.domain>")
                            and 'xmlns:us-gaap="http://fasb.org/us-gaap/2025"' in d["value"] for d in typed))
        self.assertEqual(self.result.inventory["dimension_kinds"], {"explicit": 1342, "typed": 4})

    def test_labels_come_from_the_supplied_extension_schema(self):
        labels = self.result.inventory["labels"]
        schema_record = next(r for r in self.manifest["files"] if r["role"] == "xbrl_extension_schema")
        self.assertEqual((labels["schema"], labels["sha256"]), ("msft-20260331.xsd", schema_record["sha256"]))
        self.assertEqual((labels["facts_with_label"], labels["facts_without_label"]), (1610, 12))
        revenue = self.select("RevenueFromContractWithCustomerExcludingAssessedTax")[0]
        self.assertEqual(revenue.label, "Revenue from Contract with Customer, Excluding Assessed Tax")
        net_income = self.select("NetIncomeLoss")[0]
        self.assertEqual((net_income.label, net_income.source_reference["label_source"]["role"]),
                         ("Net income", "http://www.xbrl.org/2003/role/totalLabel"))

    def test_every_fact_matches_its_inline_tag_in_the_filing_html(self):
        report = fx.check_inline_correspondence(SAMPLE_HTML, self.facts)
        html_record = next(r for r in self.manifest["files"] if r["role"] == fx.HTML_ROLE)
        self.assertEqual(report["html"]["sha256"], html_record["sha256"])
        self.assertEqual((report["facts"], report["inline_elements"], report["matched"]), (1622, 1622, 1622))
        self.assertEqual((report["numeric_values_equal"], report["numeric_values_not_compared"]), (1426, 0))
        self.assertEqual((report["mismatches"], report["inline_ids_without_fact"]), ([], []))

    def test_dry_run_command_reports_without_a_database(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(output), \
                patch.object(fx, "connect", side_effect=AssertionError("dry run opened a database connection")):
            target = Path(directory) / "facts.jsonl"
            arguments = ["--manifest", str(PROJECT_ROOT / "source_manifest.json"), "--data-root", str(PROJECT_ROOT),
                         "--check-html", "--output", str(target)]
            self.assertEqual(fx.main(arguments), 0)
            lines = target.read_text(encoding="utf-8").splitlines()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(fx.main(arguments), 1)  # an existing output file is never overwritten
        report = json.loads(output.getvalue())
        self.assertEqual((report["inventory"]["extracted"], report["failures"]), (1622, []))
        self.assertEqual(report["inline_correspondence"]["matched"], 1622)
        self.assertEqual(len(lines), 1622)
        first = json.loads(lines[0])
        self.assertEqual(first["filing_id"], str(UUID(int=0)))
        self.assertEqual(first["occurrence_key"], self.facts[0].occurrence_key)


@unittest.skipUnless(os.environ.get("SEC_DB_TEST") == "1", "Set SEC_DB_TEST=1 for live database tests.")
class FactLoadingIntegrationTests(unittest.TestCase):
    """Loads the real facts into the dedicated test database; everything is rolled back."""

    @classmethod
    def setUpClass(cls):
        cls.database_name = os.environ.get("SEC_DB_TEST_DATABASE", "sec_filings_test")
        if not re.fullmatch(r"[A-Za-z0-9_]+_test(?:_[A-Za-z0-9_]+)?", cls.database_name):
            raise RuntimeError("Live tests require an explicitly dedicated database name containing _test.")
        if cls.database_name == db.connection_settings(PROJECT_ROOT / ".env")["dbname"]:
            raise RuntimeError("Test database must differ from the configured application database.")
        with db.connect(PROJECT_ROOT / ".env", dbname=cls.database_name) as conn:
            baseline = db.status(conn)
        if not baseline["ready"] or any(baseline["counts"].values()):
            raise RuntimeError("Dedicated test database must be migrated and empty; tests never delete data.")

    def setUp(self):
        self.conn = db.connect(PROJECT_ROOT / ".env", dbname=self.database_name)
        self.addCleanup(self.conn.close)
        transaction = self.conn.transaction(force_rollback=True)
        transaction.__enter__()
        self.addCleanup(transaction.__exit__, None, None, None)
        seeded = db.seed_manifest(self.conn, PROJECT_ROOT / "source_manifest.json", PROJECT_ROOT)
        self.filing_id = seeded["filing_id"]
        self.source_id = seeded["document_ids"]["data/raw/sec/0001193125-26-191507/msft-20260331_htm.xml"]
        self.result = fx.extract_facts(SAMPLE_XML, filing_id=self.filing_id, source_document_id=self.source_id)

    def test_load_replay_conflict_and_provenance(self):
        stored = fx.store_facts(self.conn, self.result.facts)
        self.assertEqual((stored.filing_id, stored.source_document_id, stored.extraction_version),
                         (self.filing_id, self.source_id, "facts-v1"))
        self.assertEqual(len(set(stored.fact_ids.values())), 1622)

        counts = self.conn.execute("""
            SELECT count(*), count(*) FILTER (WHERE is_nil), count(numeric_value),
                   count(*) FILTER (WHERE unit IS NULL), count(DISTINCT occurrence_key),
                   count(*) FILTER (WHERE source_role = 'extracted_xbrl_instance'
                                    AND accession_number = '0001193125-26-191507'
                                    AND sha256 = %s)
            FROM sec.fact_provenance WHERE source_document_id = %s AND extraction_version = 'facts-v1'""",
            (self.result.inventory["source"]["sha256"], self.source_id)).fetchone()
        self.assertEqual(counts, (1622, 2, 1426, 194, 1622, 1622))

        # Every stored row reads back equal to what was extracted, including long text and JSON.
        rows = self.conn.execute("""
            SELECT occurrence_key, concept_namespace, concept_name, label, numeric_value, raw_value, is_nil,
                   reported_decimals, reported_precision, unit, period_kind, period_start, period_end,
                   instant_date, dimensions, source_reference
            FROM sec.financial_facts WHERE source_document_id = %s AND extraction_version = 'facts-v1'""",
            (self.source_id,)).fetchall()
        columns = ("occurrence_key", "concept_namespace", "concept_name", "label", "numeric_value", "raw_value",
                   "is_nil", "reported_decimals", "reported_precision", "unit", "period_kind", "period_start",
                   "period_end", "instant_date", "dimensions", "source_reference")
        expected = {fact.occurrence_key: tuple(getattr(fact, column) for column in columns) for fact in self.result.facts}
        self.assertEqual({row[0]: row for row in rows}, expected)

        # Loading the same content again keeps every UUID.
        self.assertEqual(fx.store_facts(self.conn, self.result.facts).fact_ids, stored.fact_ids)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sec.financial_facts").fetchone()[0], 1622)

        # Changed content under the same version is refused; a new version is separate.
        changed = replace(self.result.facts[0], label="changed")
        with self.assertRaises(db.DataConflict), self.conn.transaction():
            fx.store_facts(self.conn, [changed])
        second = fx.store_facts(self.conn, [replace(changed, extraction_version="facts-v2")])
        self.assertNotIn(second.fact_ids[changed.occurrence_key], stored.fact_ids.values())


if __name__ == "__main__":
    unittest.main()
