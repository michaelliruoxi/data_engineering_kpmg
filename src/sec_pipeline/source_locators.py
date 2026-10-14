"""Stable source paths for unchanged HTML, including literal prefixed tags.

lxml's HTML parser retains ``ix:nonfraction`` as a literal tag name rather than
an XML namespace. Its getpath result then needs name() selectors to execute as
XPath without an invented namespace mapping.
"""

import re


_STEP = re.compile(r"(?P<name>[^/\[\]]+)(?P<position>\[[1-9][0-9]*\])?")


def _literal(value):
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ")"


def executable_xpath(path):
    """Convert literal qualified-name steps while retaining ordinary positions."""
    steps = []
    for step in path.split("/"):
        match = _STEP.fullmatch(step)
        if match and ":" in match["name"]:
            step = "*[name()=" + _literal(match["name"]) + "]" + (match["position"] or "")
        steps.append(step)
    return "/".join(steps)


def source_xpath(tree, element):
    """Return an executable path, preserving all regular getpath outputs."""
    return executable_xpath(tree.getpath(element))


def inline_source_xpath(tree, element):
    """Retain versioned inline-reference identity for original-DOM map lookup.

    Existing table references store getpath's literal prefixed spelling. They
    are matched through a map of the unchanged original DOM, not evaluated as
    namespace-aware XPath. source_xpath supplies an executable form when needed.
    """
    return tree.getpath(element)


def inline_elements(tree, namespace, local_name):
    """Find literal HTML inline tags only when their in-scope namespace agrees."""
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        prefix, separator, name = element.tag.partition(":")
        if not separator or name.lower() != local_name.lower():
            continue
        attribute = "xmlns:" + prefix
        for ancestor in (element, *element.iterancestors()):
            declared = ancestor.get(attribute)
            if declared is not None:
                if declared == namespace:
                    yield element
                break
