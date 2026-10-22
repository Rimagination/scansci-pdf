"""The version of record must be scored on its own metadata, not its sibling's.

#89 traced a real Elsevier article (``10.1016/j.tws.2017.05.030``) whose
attachment container resolution ran wide: the version of record's container
was an ancestor spanning the document, so the scorer's haystack held the
*manuscript's* markers -- ``aam-pdf`` (-60) and ``thumbnail`` (-100) -- as
well as its own.

The payload below models that shape: the article-level ``<eid>`` that sits
outside every attachment block, the two attachment blocks, and a body large
enough to dominate the metadata of any node that spans it.

The sibling's markers sit at the *head* of the body, not at its tail. The
metadata window is a few KB, so markers parked behind a 340 KB body would
fall outside the window and these tests would pass even with the container
resolution reverted -- which is exactly what happened to the first version
of this file. The scores asserted below are exact, so a reverted fix moves
them from 140/-25 to -20/-25.
"""

from __future__ import annotations

import importlib
import xml.etree.ElementTree as ET

import pytest

from scansci_pdf._publisher_strategies_core import _elsevier_attachment_score

VOR_EID = "1-s2.0-S0263823116307662-main.pdf"
AM_EID = "1-s2.0-S0263823116307662-Fatemeh_Azhari_empirical_model_2017.pdf"

# The markers the sibling's metadata contributed to the version of record when
# the container ran wide. Neither belongs to the version of record.
SIBLING_MARKERS = ("aam-pdf", "thumbnail")

# +20 (.pdf) +10 (pdf) +100 (-main.pdf) +5 (page-count) +5 (attachment-size)
VOR_SCORE = 140
# +20 (.pdf) +10 (pdf) +5 (page-count) -60 ("submitted to")
AM_SCORE = -25

_BODY_PARAGRAPH = (
    "Post-fire mechanical response of ultra-high strength (Grade 1200) steel "
    "under high temperatures: linking thermal stability and microstructure. "
)

_SIBLING_MARKER_PARA = (
    '<ce:para>Supplementary material is served as aam-pdf; a thumbnail '
    "preview is available for the accepted manuscript.</ce:para>"
)


def _payload() -> str:
    """The modelled Elsevier FULL response for the reported article."""
    body = _SIBLING_MARKER_PARA + (
        "<ce:sections>" + (_BODY_PARAGRAPH * 2400) + "</ce:sections>"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
  <originalText>
    <xocs:doc xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
      <xocs:full-text-retrieval-response>
        <full-text-retrieval-response>
          <eid>1-s2.0-S0263823116307662</eid>
          <attachments>
            <attachment>
              <attachment-eid>{VOR_EID}</attachment-eid>
              <attachment-size>1234567</attachment-size>
              <page-count>22</page-count>
              <mime-type>application/pdf</mime-type>
            </attachment>
            <attachment>
              <attachment-eid>{AM_EID}</attachment-eid>
              <label>submitted to thin-walled structures this manuscript version is
                made available under the cc-by-nc-nd 4.0 license</label>
              <page-count>22</page-count>
            </attachment>
          </attachments>
          <body>{body}</body>
        </full-text-retrieval-response>
      </xocs:full-text-retrieval-response>
    </xocs:doc>
  </originalText>
</full-text-retrieval-response>
"""


def _manuscript_claims_full_text_payload() -> str:
    """The manuscript's own label claims the full text.

    That is the shape #89 describes: under the old resolution the version of
    record's container spans the document, so the manuscript's "full-text"
    claim inflates the version of record and the manuscript's own +100
    outranks it. The two copies' extractors then rank the candidates
    differently -- which is what lets the drift guard see a copy that
    resolves its container differently.
    """
    return _payload().replace(
        "<label>submitted to thin-walled structures",
        "<label>full-text submitted to thin-walled structures",
    )


# The container logic exists in two places; both feed a live code path
# (_publisher_strategies_core's API lane and sources.elsevier_api's fetch_pdf).
IMPLS = (
    pytest.param(
        (
            "scansci_pdf._publisher_strategies_core",
            "_elsevier_attachment_container",
            "_elsevier_attachment_metadata",
            "_elsevier_attachment_blocks",
            "_elsevier_eids_in",
        ),
        id="publisher_strategies_core",
    ),
    pytest.param(
        (
            "scansci_pdf.sources.elsevier_api",
            "_attachment_container",
            "_attachment_metadata",
            "_attachment_blocks",
            "_eids_in",
        ),
        id="sources.elsevier_api",
    ),
)


def _resolve(impl, xml_text):
    """For every candidate attachment: its container's tag and its metadata."""
    module_name, container_name, metadata_name, blocks_name, eids_name = impl
    module = importlib.import_module(module_name)
    root = ET.fromstring(xml_text)
    parent_map = {child: parent for parent in root.iter() for child in parent}
    blocks = getattr(module, blocks_name)(root)

    out: dict[str, tuple[str, str]] = {}
    for el in root.iter():
        for eid in getattr(module, eids_name)(el):
            if eid in out:
                continue
            container = getattr(module, container_name)(el, parent_map, eid, blocks)
            tag = str(container.tag).split("}")[-1]
            out[eid] = (tag, getattr(module, metadata_name)(container))
    return out


def _metadata_by_eid(impl, xml_text):
    """The metadata each candidate attachment is scored with."""
    return {eid: meta for eid, (_tag, meta) in _resolve(impl, xml_text).items()}


def _tag_by_eid(impl, xml_text):
    """The tag of the container each candidate attachment is scored with."""
    return {eid: tag for eid, (tag, _meta) in _resolve(impl, xml_text).items()}


def _score(impl, eid, metadata):
    """Score with the copy's OWN scorer.

    The two copies are two implementations of one rule, and they run in two
    different lanes -- ``sources.elsevier_api.fetch_pdf`` and the publisher
    strategies -- so a candidate has to be scored by the copy that will score
    it in production. Scoring both with the core copy's scorer would leave
    the sources copy's scorer untested.
    """
    module = importlib.import_module(impl[0])
    name = (
        "_elsevier_attachment_score"
        if impl[0].endswith("_core")
        else "_attachment_score"
    )
    return getattr(module, name)(eid, metadata)


@pytest.mark.parametrize("impl", IMPLS)
def test_the_version_of_record_is_scored_on_its_own_metadata(impl):
    """The version of record must not inherit the manuscript's markers.

    The score is asserted exactly: with the container running wide again the
    sibling's markers land in the haystack and the score drops to -20.
    """
    metadata = _metadata_by_eid(impl, _payload())

    vor = metadata[VOR_EID]
    assert vor, "the version of record resolved to no metadata at all"
    for marker in SIBLING_MARKERS:
        assert marker not in vor, (
            f"the version of record's metadata carries the sibling's {marker!r} marker"
        )
    assert _score(impl, VOR_EID, vor) == VOR_SCORE


@pytest.mark.parametrize("impl", IMPLS)
def test_the_manuscript_is_still_scored_as_a_manuscript(impl):
    """The manuscript keeps its own markers, and only its own.

    A fix that simply truncated every container to the same shape would also
    erase the manuscript's "submitted to" label, which is the marker that
    makes it a manuscript rather than the version of record.
    """
    metadata = _metadata_by_eid(impl, _payload())

    am = metadata[AM_EID]
    assert "submitted to" in am
    for marker in SIBLING_MARKERS:
        assert marker not in am
    assert _score(impl, AM_EID, am) == AM_SCORE


@pytest.mark.parametrize("impl", IMPLS)
def test_the_container_is_the_attachment_block_itself(impl):
    """The container must be the block that describes *this* attachment.

    Not the plural ``<attachments>`` holder, and not an ancestor that spans
    the document: both carry every sibling's metadata.
    """
    tags = _tag_by_eid(impl, _payload())

    assert tags[VOR_EID] == "attachment"
    assert tags[AM_EID] == "attachment"


@pytest.mark.parametrize("impl", IMPLS)
def test_both_containers_stay_tight(impl):
    """Pin the two containers so they cannot drift apart again.

    The defect was a container of 238,040 chars next to one of 1,171 chars for
    the same article's two attachments. Both must now be a tight block, of a
    comparable size.
    """
    metadata = _metadata_by_eid(impl, _payload())

    vor_len = len(metadata[VOR_EID])
    am_len = len(metadata[AM_EID])
    assert vor_len < 4096, f"the version of record's container is {vor_len} chars"
    assert am_len < 4096, f"the manuscript's container is {am_len} chars"
    assert max(vor_len, am_len) <= 4 * max(1, min(vor_len, am_len)), (
        f"the two containers drifted apart again: {vor_len} vs {am_len}"
    )


@pytest.mark.parametrize("impl", IMPLS)
def test_the_version_of_record_outranks_the_manuscript(impl):
    """The whole point: the version of record is the file that gets fetched."""
    metadata = _metadata_by_eid(impl, _payload())

    vor_score = _score(impl, VOR_EID, metadata[VOR_EID])
    am_score = _score(impl, AM_EID, metadata[AM_EID])
    assert vor_score > am_score, (
        f"the version of record scored {vor_score}, the manuscript {am_score}"
    )


def test_the_extraction_ranks_the_version_of_record_first():
    """End to end: the ranked candidate list starts with the version of record."""
    from scansci_pdf._publisher_strategies_core import (
        _extract_elsevier_pdf_attachment_eids,
    )

    ranked = _extract_elsevier_pdf_attachment_eids(_payload())
    assert ranked[0] == VOR_EID
    assert AM_EID in ranked


def test_the_two_copies_agree_on_the_container():
    """The container helpers had drifted too (only one knew about ``web-pdf``).

    Pin them together so #89 cannot be fixed in one copy only.
    """
    core = importlib.import_module("scansci_pdf._publisher_strategies_core")
    api = importlib.import_module("scansci_pdf.sources.elsevier_api")
    payloads = (
        _payload(),
        _nested_id_payload(),
        _cut_marker_payload(),
        _wide_container_payload(),
        _manuscript_claims_full_text_payload(),
    )

    for payload in payloads:
        core_resolved = _resolve(
            (
                "scansci_pdf._publisher_strategies_core",
                "_elsevier_attachment_container",
                "_elsevier_attachment_metadata",
                "_elsevier_attachment_blocks",
                "_elsevier_eids_in",
            ),
            payload,
        )
        api_resolved = _resolve(
            (
                "scansci_pdf.sources.elsevier_api",
                "_attachment_container",
                "_attachment_metadata",
                "_attachment_blocks",
                "_eids_in",
            ),
            payload,
        )
        assert core_resolved == api_resolved

    # The normalisation of an id to an attachment identity is shared logic.
    for eid in (VOR_EID, AM_EID, "1-s2.0-S0263823116307662"):
        assert core._elsevier_attachment_key(eid) == api._attachment_key(eid)


def test_an_article_level_id_and_its_main_pdf_name_one_attachment():
    """``1-s2.0-X`` and ``1-s2.0-X-main.pdf`` are the same version of record.

    The article-level id sits outside every attachment block, so it is the
    attachment block that describes the version of record it names -- and the
    two must not be treated as two different attachments.
    """
    core = importlib.import_module("scansci_pdf._publisher_strategies_core")
    assert core._elsevier_attachment_key("1-s2.0-X-main.pdf") == core._elsevier_attachment_key(
        "1-s2.0-X"
    )
    assert core._elsevier_attachment_key(AM_EID) != core._elsevier_attachment_key(VOR_EID)


def test_the_two_copies_expose_the_same_helpers():
    """The new helpers must exist in both copies under their own names.

    The two copies have drifted before -- only one of them knew about
    ``web-pdf`` -- so the helper set is pinned by name.
    """
    core = importlib.import_module("scansci_pdf._publisher_strategies_core")
    api = importlib.import_module("scansci_pdf.sources.elsevier_api")

    for core_name, api_name in (
        ("_elsevier_is_attachment_block", "_is_attachment_block"),
        ("_elsevier_is_block_element", "_is_block_element"),
        ("_elsevier_has_block_descendant", "_has_block_descendant"),
        ("_elsevier_declared_eids", "_declared_eids"),
        ("_elsevier_declares_file_id", "_declares_file_id"),
        ("_elsevier_block_is_tight", "_block_is_tight"),
        ("_elsevier_block_is_scoped", "_block_is_scoped"),
        ("_elsevier_block_declares", "_block_declares"),
        ("_elsevier_attachment_blocks", "_attachment_blocks"),
        ("_elsevier_attachment_container", "_attachment_container"),
        ("_elsevier_attachment_metadata", "_attachment_metadata"),
        ("_elsevier_cap_text", "_cap_text"),
    ):
        assert hasattr(core, core_name), f"the core copy lost {core_name}"
        assert hasattr(api, api_name), f"the sources copy lost {api_name}"


@pytest.mark.parametrize("impl", IMPLS)
def test_a_block_tag_is_not_mistaken_for_a_block_element(impl):
    """``<attachment-size>`` describes nothing; it is not an attachment.

    The loose "the tag name mentions attachment" test would take it for a
    block, and the walk would stop there with nothing but a number in hand.
    """
    module_name, *_ = impl
    module = importlib.import_module(module_name)

    is_block = getattr(
        module,
        "_elsevier_is_block_element" if module_name.endswith("_core") else "_is_block_element",
    )
    is_attachment_block = getattr(
        module,
        "_elsevier_is_attachment_block"
        if module_name.endswith("_core")
        else "_is_attachment_block",
    )

    root = ET.fromstring(
        "<attachments><attachment-size>42</attachment-size>"
        "<attachment-metadata>x</attachment-metadata>"
        "<attachment><attachment-eid>1-s2.0-X-main.pdf</attachment-eid></attachment>"
        "</attachments>"
    )
    size, meta, block = list(root)

    assert not is_block(size)
    assert not is_block(meta)
    assert is_block(block)
    # The loose test still treats it as attachment-shaped, which is why the
    # block index uses the explicit list above.
    assert is_attachment_block(size)


# --- regressions the adversarial review found ----------------------------- #


@pytest.mark.parametrize("impl", IMPLS)
def test_a_long_label_does_not_cost_the_version_of_record_its_own_claim(impl):
    """A 5,000-character label must not push the block's own markers out.

    The metadata window is a few KB. Capping the *total* only would let one
    long label fill it and cut the block's own "full-text" claim -- which
    costs the version of record the +100 it had earned from its own id.
    """
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>1-s2.0-S0263823116307662-mainext.pdf</attachment-eid>
      <label>{"x" * 5000}</label>
      <originalText><full-text>Version of Record</full-text></originalText>
    </attachment>
    <attachment>
      <attachment-eid>1-s2.0-S0263823116307662-am.pdf</attachment-eid>
      <label>accepted manuscript, full-text retrieval version</label>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    metadata = _metadata_by_eid(impl, payload)

    vor = metadata["1-s2.0-S0263823116307662-mainext.pdf"]
    assert "full-text" in vor, "the version of record lost its own full-text claim"
    assert len(vor) < 4096

    # +20 (.pdf) +10 (pdf) +100 (the block's own "full-text" claim)
    assert _score(impl, 
        "1-s2.0-S0263823116307662-mainext.pdf", vor
    ) == 130
    # +20 +10 +100 -60 ("accepted manuscript")
    assert _score(impl, 
        "1-s2.0-S0263823116307662-am.pdf",
        metadata["1-s2.0-S0263823116307662-am.pdf"],
    ) == 70


@pytest.mark.parametrize("impl", IMPLS)
def test_a_holder_of_two_blocks_normalising_to_one_key_is_not_a_container(impl):
    """Two blocks that normalise to one key are still two blocks.

    Counting *keys* would let such a holder look "scoped", and the sibling's
    metadata would leak back in. The block that names the file id wins.
    """
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <eid>1-s2.0-S0263823116307662</eid>
      <label>supplementary material</label>
    </attachment>
    <attachment>
      <attachment-eid>1-s2.0-S0263823116307662-main.pdf</attachment-eid>
      <attachment-size>1234567</attachment-size>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    resolved = _resolve(impl, payload)
    tag, meta = resolved["1-s2.0-S0263823116307662-main.pdf"]

    assert tag == "attachment", f"the holder was used as the container ({tag})"
    assert "supplementary" not in meta, "the sibling block's label leaked in"
    assert _score(impl, "1-s2.0-S0263823116307662-main.pdf", meta) == 140


@pytest.mark.parametrize("impl", IMPLS)
def test_a_manuscript_block_holding_a_nested_object_keeps_its_label(impl):
    """A block that also holds something else still describes its own file.

    A nested ``<object>`` makes the block hold more than one attachment, so
    the strict "scoped to this attachment" test rejects it. Falling back to
    the id element then throws the block's own label away, and the manuscript
    stops being recognisable as one.
    """
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>{VOR_EID}</attachment-eid>
      <attachment-size>1234567</attachment-size>
      <page-count>22</page-count>
    </attachment>
    <attachment>
      <attachment-eid>{AM_EID}</attachment-eid>
      <label>submitted to thin-walled structures this manuscript version is made
        available under the cc-by-nc-nd 4.0 license</label>
      <page-count>22</page-count>
      <object><object-eid>1-s2.0-S0263823116307662-mmc1.pdf</object-eid></object>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    metadata = _metadata_by_eid(impl, payload)

    assert "submitted to" in metadata[AM_EID], "the manuscript's own label was lost"
    # +20 (.pdf) +10 (pdf) +5 (page-count) -60 ("submitted to")
    # -100 (the nested object's own "mmc" id marks the block as holding
    # supplementary material) -- the block legitimately holds it, and the
    # version of record still wins by a wide margin. What this pins is that
    # the label survived: without it the score was +30, above the version of
    # record's, and the manuscript stopped being recognisable as one.
    assert _score(impl, AM_EID, metadata[AM_EID]) == -125
    assert _score(impl, VOR_EID, metadata[VOR_EID]) == 140


@pytest.mark.parametrize("impl", IMPLS)
def test_an_id_carried_as_an_attribute_resolves_to_its_own_block(impl):
    """``<attachment eid="...">`` must resolve to that attachment.

    Starting the search at the id element's *parent* cannot see the id when
    the id is an attribute of the block itself, and the search then runs up
    to the ``<attachments>`` holder.
    """
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment eid="1-s2.0-S0263823116307662">
      <attachment-size>1234567</attachment-size>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    resolved = _resolve(impl, payload)
    tag, meta = resolved["1-s2.0-S0263823116307662-main.pdf"]

    assert tag == "attachment", f"the holder was used as the container ({tag})"
    assert _score(impl, "1-s2.0-S0263823116307662-main.pdf", meta) == 140


def test_the_same_file_under_two_spellings_is_one_candidate():
    """``1-S2.0-X-MAIN.PDF`` and ``1-s2.0-x-main.pdf`` are one attachment.

    Deduplicating on the raw id string would offer the same file twice.
    The two spellings differ in more than case here, so a de-duplication on
    the lowercased id string cannot pass for a de-duplication on the
    attachment's identity.
    """
    from scansci_pdf._publisher_strategies_core import (
        _extract_elsevier_pdf_attachment_eids,
    )
    from scansci_pdf.sources.elsevier_api import (
        _extract_pdf_attachment_eids,
    )

    payload = """<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment><attachment-eid>1-S2.0-S0263823116307662-MAIN.PDF</attachment-eid></attachment>
    <attachment><attachment-eid>1-s2.0-S0263823116307662.pdf</attachment-eid></attachment>
  </attachments>
</full-text-retrieval-response>
"""
    for extract in (_extract_elsevier_pdf_attachment_eids, _extract_pdf_attachment_eids):
        ranked = extract(payload)
        assert len(ranked) == 1, f"the same file was offered twice: {ranked}"


def test_the_two_scorers_agree():
    """The two copies are two implementations of one rule.

    They live in two live lanes, so a difference between them makes the
    lanes pick different files -- the accepted manuscript in one of them and
    the version of record in the other. The fixtures below cover the
    id-derived terms and every marker the rule reads.
    """
    core = importlib.import_module("scansci_pdf._publisher_strategies_core")
    api = importlib.import_module("scansci_pdf.sources.elsevier_api")

    eids = (
        "1-s2.0-X-main.pdf",
        "1-s2.0-X-am.pdf",
        "1-s2.0-X-author_2017.pdf",
        "1-s2.0-X.pdf",
    )
    haystacks = (
        "",
        "attachment:1-s2.0-x-main.pdf attachment-size:1 page-count:2",
        "attachment:1-s2.0-x-am.pdf label:submitted to a manuscript version",
        "attachment:1-s2.0-x-am.pdf aam-pdf thumbnail supplementary mmc",
        "attachment:1-s2.0-x-main.pdf full-text pages attachment-size filesize",
        "attachment:1-s2.0-x-am.pdf accepted manuscript author manuscript",
    )
    for eid in eids:
        for haystack in haystacks:
            assert core._elsevier_attachment_score(eid, haystack) == (
                api._attachment_score(eid, haystack)
            ), f"the two scorers disagree on {eid!r} / {haystack!r}"


# --- the second review round ---------------------------------------------- #

# The window and the per-element cap are safety nets. They bound how much
# metadata reaches the scorer; they must not decide the score.


def _cut_marker_payload() -> str:
    """A label whose marker straddles the per-element cap."""
    label = "x" * 508 + "aam-pdf" + "y" * 200
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>{AM_EID}</attachment-eid>
      <label>{label}</label>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""


def _wide_container_payload() -> str:
    """A container whose decisive marker sits behind a few KB of siblings."""
    filler = "z" * 375
    kids = "".join(f"<meta-{i}>{filler}</meta-{i}>" for i in range(12))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>{VOR_EID}</attachment-eid>
      {kids}
      <label>full-text Version of Record</label>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""


def _nested_id_payload() -> str:
    """A block whose id sits one level down, so the id's parent is not the block.

    Falling back to the id's parent -- the defect #89 was about -- is only
    visible when the parent is *not* the tight block. With the id as a direct
    child of the block, both resolutions land on the same node, and a copy
    that had been reverted would look fine.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <originalText><attachment-eid>{VOR_EID}</attachment-eid></originalText>
      <attachment-size>1234567</attachment-size>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""


@pytest.mark.parametrize("impl", IMPLS)
def test_an_element_without_text_does_not_add_a_marker(impl):
    """A name is a marker only when the element produced a part.

    The metadata builder emits a part for an element only when it has text,
    so an empty ``<supplementary-material/>`` never reached the haystack in
    any version of the metadata. Reading its name anyway would demote the
    version of record by 100 points on the strength of an element that
    carries no data.
    """
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>{VOR_EID}</attachment-eid>
      <attachment-size>1234567</attachment-size>
      <supplementary-material/>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    metadata = _metadata_by_eid(impl, payload)

    vor = metadata[VOR_EID]
    assert "supplement" not in vor, "an element without text added a marker"
    # +20 (.pdf) +10 (pdf) +100 (-main.pdf) +5 (attachment-size)
    assert _score(impl, VOR_EID, vor) == 135


@pytest.mark.parametrize("impl", IMPLS)
def test_a_marker_cut_in_half_by_the_element_cap_still_counts(impl):
    """The cap must not decide the score.

    A label whose marker straddled the cap used to lose the marker: the
    scorer could not read it, and the score moved by 60 points. The cap
    bounds the size of the metadata, not what it says.
    """
    metadata = _metadata_by_eid(impl, _cut_marker_payload())

    am = metadata[AM_EID]
    assert "aam-pdf" in am, "the marker that straddled the cap was lost"
    # +20 (.pdf) +10 (pdf) +5 (page-count) -60 ("aam-pdf")
    assert _score(impl, AM_EID, am) == -25


@pytest.mark.parametrize("impl", IMPLS)
def test_the_window_does_not_decide_the_score(impl):
    """A marker behind a few KB of siblings must survive the window.

    The window used to drop the element that carried the "full-text" claim,
    and with it the name of the "page-count" element, so the version of
    record lost the +100 for its own claim.
    """
    metadata = _metadata_by_eid(impl, _wide_container_payload())

    vor = metadata[VOR_EID]
    assert "full-text" in vor, "the version of record lost its own full-text claim"
    assert "page-count" in vor, "the page-count element's name was lost with it"
    # +20 (.pdf) +10 (pdf) +100 (full-text) +5 (page-count)
    assert _score(impl, VOR_EID, vor) == 135


@pytest.mark.parametrize("impl", IMPLS)
def test_the_bounded_metadata_scores_the_same_as_the_unbounded_one(impl):
    """The bounds must not move the score.

    The same payloads are scored with the bounds in place and with them
    removed. The two scores have to be identical, or the window decides
    which file is fetched.
    """
    module_name = impl[0]
    module = importlib.import_module(module_name)
    cap_name = (
        "_ELSEVIER_METADATA_PART_CAP"
        if module_name.endswith("_core")
        else "_ATTACHMENT_METADATA_PART_CAP"
    )
    window_name = (
        "_ELSEVIER_METADATA_WINDOW"
        if module_name.endswith("_core")
        else "_ATTACHMENT_METADATA_WINDOW"
    )
    payloads = (_payload(), _cut_marker_payload(), _wide_container_payload())

    bounded: dict[tuple[str, str], int] = {}
    for payload in payloads:
        for eid, meta in _metadata_by_eid(impl, payload).items():
            bounded[(payload, eid)] = _score(impl, eid, meta)

    old_cap = getattr(module, cap_name)
    old_window = getattr(module, window_name)
    try:
        setattr(module, cap_name, 10**9)
        setattr(module, window_name, 10**9)
        for payload in payloads:
            for eid, meta in _metadata_by_eid(impl, payload).items():
                unbounded = _score(impl, eid, meta)
                assert bounded[(payload, eid)] == unbounded, (
                    f"the window moved the score of {eid} from {unbounded} "
                    f"to {bounded[(payload, eid)]}"
                )
    finally:
        setattr(module, cap_name, old_cap)
        setattr(module, window_name, old_window)


@pytest.mark.parametrize("impl", IMPLS)
def test_the_metadata_window_bounds_the_output(impl):
    """A container with a lot of metadata must still be bounded.

    The window is the safety net: without it a pathological container fills
    the scorer's haystack with megabytes of text.
    """
    filler = "n" * 900
    kids = "".join(f"<note-{i}>{filler}</note-{i}>" for i in range(40))
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachments>
    <attachment>
      <attachment-eid>{VOR_EID}</attachment-eid>
      {kids}
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    metadata = _metadata_by_eid(impl, payload)

    assert len(metadata[VOR_EID]) < 4096, (
        f"the container's metadata is {len(metadata[VOR_EID])} chars"
    )


@pytest.mark.parametrize("impl", IMPLS)
def test_an_id_no_block_declares_resolves_to_the_block_naming_the_same_file(impl):
    """The id may name the file under a spelling no block declares.

    ``1-s2.0-X-main.pdf`` and ``1-s2.0-X.pdf`` are the same attachment, so
    an id that sits outside every attachment block still resolves to the
    block that describes the file -- and not to the element that carried it,
    whose metadata is nothing but the id.
    """
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <attachment-eid>1-s2.0-S0263823116307662-main.pdf</attachment-eid>
  <attachments>
    <attachment>
      <attachment-eid>1-s2.0-S0263823116307662.pdf</attachment-eid>
      <attachment-size>1234567</attachment-size>
      <page-count>22</page-count>
    </attachment>
  </attachments>
</full-text-retrieval-response>
"""
    resolved = _resolve(impl, payload)
    tag, meta = resolved["1-s2.0-S0263823116307662-main.pdf"]

    assert tag == "attachment", f"the container was the {tag} element"
    assert "attachment-size" in meta, "the block's own metadata was lost"


@pytest.mark.parametrize("impl", IMPLS)
def test_an_id_that_no_block_declares_resolves_to_its_own_element(impl):
    """The tightest scope there is: the element that carried the id.

    Falling back to the id's *parent* is the defect #89 was about, so the
    fallback is pinned to the id element itself.
    """
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<full-text-retrieval-response>
  <weird><attachment-eid>1-s2.0-UNKNOWN-main.pdf</attachment-eid></weird>
</full-text-retrieval-response>
"""
    resolved = _resolve(impl, payload)
    tag, _meta = resolved["1-s2.0-UNKNOWN-main.pdf"]

    assert tag == "attachment-eid", f"the container was the {tag} element"


def test_the_two_copies_rank_the_same_candidates():
    """The drift guard has to compare the *result*, not the helper names.

    Reverting the sources copy's container resolution used to leave every
    test green: the new test file only drove the core copy's extractor, so
    the sources copy could be broken without anything noticing.
    """
    from scansci_pdf._publisher_strategies_core import (
        _extract_elsevier_pdf_attachment_eids,
    )
    from scansci_pdf.sources.elsevier_api import (
        _extract_pdf_attachment_eids,
    )

    payloads = (
        _payload(),
        _nested_id_payload(),
        _cut_marker_payload(),
        _wide_container_payload(),
        _manuscript_claims_full_text_payload(),
    )
    for payload in payloads:
        assert _extract_elsevier_pdf_attachment_eids(payload) == (
            _extract_pdf_attachment_eids(payload)
        ), "the two copies ranked the candidates differently"


def test_the_two_copies_agree_on_their_constants():
    """The constants are shared logic too, and they drift silently."""
    core = importlib.import_module("scansci_pdf._publisher_strategies_core")
    api = importlib.import_module("scansci_pdf.sources.elsevier_api")

    assert core._ELSEVIER_BLOCK_TAGS == api._ATTACHMENT_BLOCK_TAGS
    assert core._ELSEVIER_ID_TAGS == api._ATTACHMENT_ID_TAGS
    assert core._ELSEVIER_METADATA_WINDOW == api._ATTACHMENT_METADATA_WINDOW
    assert core._ELSEVIER_METADATA_PART_CAP == api._ATTACHMENT_METADATA_PART_CAP
    assert core._ELSEVIER_METADATA_MARKERS == api._ATTACHMENT_METADATA_MARKERS
    assert (
        core._ELSEVIER_ACCEPTED_MANUSCRIPT_MARKERS
        == api._ACCEPTED_MANUSCRIPT_MARKERS
    )
