"""The version of record must win over the author's accepted manuscript.

The Elsevier attachment scorer matched the bare substring ``"main"`` anywhere in
the object id *or* its metadata. Elsevier stores accepted manuscripts under a
``.../main/application/pdf/...`` path, so an ``-am.pdf`` collected the same +100
as the real ``-main.pdf`` it accompanies and — with a page-count bonus on top —
outranked it. Measured on ``10.1016/j.scitotenv.2021.149946`` before the fix::

    +135  1-s2.0-S004896972105021X-am.pdf     <- chosen
    +130  1-s2.0-S004896972105021X-main.pdf

The wrong file is easy to miss: the accepted manuscript carries an Elsevier
banner reading "Version of Record: https://www.sciencedirect.com/...".

The metadata below is the real text those two attachments carried from the API.
"""

from __future__ import annotations

import pytest

from scansci_pdf._publisher_strategies_core import _elsevier_attachment_score
from scansci_pdf.sources import elsevier_api

AM_EID = "1-s2.0-S004896972105021X-am.pdf"
AM_META = (
    "attachment:1-s2.0-s004896972105021x-am.pdf "
    "https://s3-eu-west-1.amazonaws.com/prod-ucs-content-store-eu-west/content/"
    "egi:10nd0hz71fn/main/application/pdf/292968d9fdc19cec1b954bae73db8e55/am.pdf "
    "am am.pdf pdf false 2047607 aam-pdf page-count:22 attachment-size:2047607"
)
VOR_EID = "1-s2.0-S004896972105021X-main.pdf"
VOR_META = (
    "coredata:https://api.elsevier.com/content/article/pii/s004896972105021x "
    "doi:10.1016/j.scitotenv.2021.149946 1-s2.0-s004896972105021x "
    "s0048-9697(21)05021-x ecohydrological effects of photovoltaic solar farms "
    "on soil microclimates and moisture regimes"
)

# The scorer exists in two places; both feed a live code path
# (_publisher_strategies_core's API lane and sources.elsevier_api's fetch_pdf).
SCORERS = (
    pytest.param(_elsevier_attachment_score, id="publisher_strategies_core"),
    pytest.param(elsevier_api._attachment_score, id="sources.elsevier_api"),
)


@pytest.mark.parametrize("score", SCORERS)
def test_version_of_record_outranks_accepted_manuscript(score):
    assert score(VOR_EID, VOR_META) > score(AM_EID, AM_META)


@pytest.mark.parametrize("score", SCORERS)
def test_author_named_accepted_manuscript_is_also_deprioritised(score):
    """Second case from the report: the AM is named after its author, so only
    the metadata identifies it."""
    am_eid = "1-s2.0-S0263823116307662-Fatemeh_Azhari_empirical_model_2017.pdf"
    am_meta = (
        "submitted to: thin-walled structures this manuscript version is made "
        "available under the cc-by-nc-nd 4.0 license"
    )
    assert score(VOR_EID, VOR_META) > score(am_eid, am_meta)


@pytest.mark.parametrize("score", SCORERS)
def test_deprioritised_manuscript_still_beats_supplementary_material(score):
    """Deprioritised, not excluded: when no version of record is attached the
    manuscript is still the best candidate available."""
    supplement = score("1-s2.0-S004896972105021X-mmc1.pdf", "supplementary mmc1 appendix")
    assert score(AM_EID, AM_META) > supplement


@pytest.mark.parametrize("score", SCORERS)
def test_manuscript_claiming_full_text_still_loses_to_the_version_of_record(score):
    """A manuscript can carry a "full-text" claim of its own; the object id
    must still decide."""
    assert score(VOR_EID, VOR_META) > score(AM_EID, AM_META + " full-text")


def test_the_two_copies_of_the_scorer_agree():
    """These two implementations had already drifted (only one discounted
    thumbnails); pin them together so issue #73 cannot be fixed in one copy
    only."""
    cases = [
        (VOR_EID, VOR_META),
        (AM_EID, AM_META),
        ("1-s2.0-X-main.pdf", ""),
        ("1-s2.0-X-am.pdf", ""),
        ("1-s2.0-X.pdf", "full-text"),
        ("1-s2.0-X-mmc1.pdf", "supplementary material"),
        ("1-s2.0-X.pdf", "thumbnail"),
        ("1-s2.0-X.pdf", ""),
    ]
    for eid, metadata in cases:
        assert _elsevier_attachment_score(eid, metadata) == elsevier_api._attachment_score(
            eid, metadata
        ), f"the two scorers disagree on {eid!r}"
