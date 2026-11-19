"""The SEC's document-and-entity-information (DEI) namespaces, in every form they take.

The taxonomy's namespace carries its release: a year for most (`dei/2023`), a
full date for the 2019 and 2020 releases (`dei/2020-01-31`), and a quarter for
the release that added the auditor facts (`dei/2021q4`), which filings used
from January 2022 into 2023. A pattern that knows only the year misses the
registrant facts of every filing on the other two forms.
"""

import re


DEI_NAMESPACE = r'https?://xbrl\.sec\.gov/dei/[0-9]{4}(?:-[0-9]{2}-[0-9]{2}|q[1-4])?'


def is_dei_namespace(namespace):
    return bool(namespace and re.fullmatch(DEI_NAMESPACE, namespace))


def is_dei_concept(concept, name=r'[A-Za-z]+'):
    """Whether a Clark-notation concept (`{namespace}Name`) is the named DEI concept."""
    return bool(concept and re.fullmatch(r'\{' + DEI_NAMESPACE + r'\}' + name, concept))
