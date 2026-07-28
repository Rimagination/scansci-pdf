import pytest

from scansci_pdf.institutional.publisher_batch import (
    PaperRecord as InstitutionalPaperRecord,
)
from scansci_pdf.institutional.publisher_batch import (
    PublisherBatchDownloader as InstitutionalPublisherBatchDownloader,
)
from scansci_pdf.publisher_batch import PaperRecord, PublisherBatchDownloader


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
