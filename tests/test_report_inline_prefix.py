"""Hidden Inline XBRL resources are recognized independently of prefix spelling."""

from lxml import html
import pytest

from sec_pipeline.report_cleaning import clean_report


@pytest.mark.parametrize("namespace", [
    "http://www.xbrl.org/2008/inlineXBRL",
    "http://www.xbrl.org/2013/inlineXBRL",
])
@pytest.mark.parametrize("prefix", ["ix", "alternate"])
def test_recognized_inline_containers_are_omitted_and_visible_tails_survive(tmp_path, namespace, prefix):
    source = f"""<html xmlns:{prefix}="{namespace}"><body>
      <p>Before <{prefix}:hidden><{prefix}:nonFraction>Hidden facts</{prefix}:nonFraction></{prefix}:hidden>visible tail.</p>
      <{prefix}:header><{prefix}:resources><p>Resource context</p></{prefix}:resources></{prefix}:header>After header.
      <p>Body continues.</p>
    </body></html>""".encode()
    path = tmp_path / "report.htm"
    path.write_bytes(source)
    report = clean_report(path)
    assert report.full_text == "Before visible tail.\n\nAfter header.\n\nBody continues."
    assert path.read_bytes() == source
    document = html.document_fromstring(source, parser=html.HTMLParser(encoding="utf-8", no_network=True))
    for section in report.sections:
        assert len(document.xpath(section["source_locator"]["xpath"])) == 1


@pytest.mark.parametrize("prefix", ["ix", "alternate"])
def test_foreign_namespace_with_same_local_names_remains_visible(tmp_path, prefix):
    path = tmp_path / "report.htm"
    path.write_text(f"""<html xmlns:{prefix}="urn:example:visible"><body>
      <p><{prefix}:header>Visible foreign header</{prefix}:header> and tail.</p>
      <p><{prefix}:hidden>Visible foreign hidden name</{prefix}:hidden> after.</p>
    </body></html>""", encoding="utf-8")
    assert clean_report(path).full_text == (
        "Visible foreign header and tail.\n\nVisible foreign hidden name after."
    )


def test_nearest_namespace_override_controls_visibility(tmp_path):
    path = tmp_path / "report.htm"
    path.write_text("""<html xmlns:alt="http://www.xbrl.org/2013/inlineXBRL"><body>
      <alt:hidden>Hidden root namespace</alt:hidden>
      <div xmlns:alt="urn:example:visible"><alt:hidden>Visible shadowed namespace</alt:hidden></div>
      <div xmlns:other="urn:example:visible">
        <other:header xmlns:other="http://www.xbrl.org/2008/inlineXBRL">Hidden local namespace</other:header>Visible tail.
      </div>
    </body></html>""", encoding="utf-8")
    assert clean_report(path).full_text == "Visible shadowed namespace\n\nVisible tail."


def test_default_inline_namespace_omits_hidden_containers_without_dropping_visible_body(tmp_path):
    path = tmp_path / "report.htm"
    path.write_text("""<html><body>
      <div xmlns="http://www.xbrl.org/2013/inlineXBRL"><header>Hidden resources</header>
        <hidden>Hidden facts</hidden><p xmlns="http://www.w3.org/1999/xhtml">Visible body</p>
      </div>
      <header>Ordinary HTML header</header>
    </body></html>""", encoding="utf-8")
    assert clean_report(path).full_text == "Visible body\n\nOrdinary HTML header"
