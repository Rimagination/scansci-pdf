import pytest

from scansci_pdf.institutional.publisher_batch import (
    PaperRecord as InstitutionalPaperRecord,
)
from scansci_pdf.institutional.publisher_batch import (
    PublisherBatchDownloader as InstitutionalPublisherBatchDownloader,
)
from scansci_pdf.institutional.publisher_batch import (
    _is_usable_pdf_response as institutional_is_usable_pdf_response,
)
from scansci_pdf.publisher_batch import (
    PaperRecord,
    PublisherBatchDownloader,
    _is_usable_pdf_response,
)


@pytest.mark.parametrize(
    ("downloader", "record_type"),
    [
        (PublisherBatchDownloader, PaperRecord),
        (InstitutionalPublisherBatchDownloader, InstitutionalPaperRecord),
    ],
)
def test_springer_electronic_supplementary_material_is_not_main_article(
    downloader,
    record_type,
) -> None:
    text = """
    Electronic Supplementary Material
    Example article title
    Supporting information to https://doi.org/10.1007/s12274-022-4138-4
    """
    record = record_type(doi="10.1007/s12274-022-4138-4")

    assert not downloader._text_matches_record(text, record)


def test_main_article_with_matching_doi_is_verified() -> None:
    text = """
    Example article title
    https://doi.org/10.1007/s12274-022-4138-4
    Abstract
    """
    record = PaperRecord(doi="10.1007/s12274-022-4138-4")

    assert PublisherBatchDownloader._text_matches_record(text, record)


@pytest.mark.parametrize(
    "validator",
    [_is_usable_pdf_response, institutional_is_usable_pdf_response],
)
def test_pdf_response_rejects_utf8_replacement_byte_corruption(validator) -> None:
    valid_pdf = b"%PDF-1.7\n" + (b"binary stream data\n" * 400)
    corrupted_pdf = (
        b"%PDF-1.7\n"
        + (b"x\xef\xbf\xbdcorrupted flate stream\n" * 400)
    )

    assert validator(valid_pdf)
    assert not validator(corrupted_pdf)


@pytest.mark.parametrize(
    "validator",
    [_is_usable_pdf_response, institutional_is_usable_pdf_response],
)
def test_pdf_response_tolerates_one_replacement_sequence_in_metadata(validator) -> None:
    pdf = b"%PDF-1.7\nmetadata \xef\xbf\xbd\n" + (b"binary stream data\n" * 400)

    assert validator(pdf)
