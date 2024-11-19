from typing import List

from surya.layout import batch_layout_detection
from surya.schema import LayoutResult

from marker.settings import settings
from marker.v2.builders import BaseBuilder
from marker.v2.providers.pdf import PageLines, PageSpans, PdfProvider
from marker.v2.schema import BlockTypes
from marker.v2.schema.document import Document
from marker.v2.schema.groups.page import PageGroup
from marker.v2.schema.polygon import PolygonBox
from marker.v2.schema.registry import get_block_class
from marker.v2.schema.text.line import Line


class LayoutBuilder(BaseBuilder):
    batch_size = None

    def __init__(self, layout_model, config=None):
        self.layout_model = layout_model

        super().__init__(config)

    def __call__(self, document: Document, provider: PdfProvider):
        layout_results = self.surya_layout(document.pages)
        self.add_blocks_to_pages(document.pages, layout_results)
        self.merge_blocks(document.pages, provider.page_lines, provider.page_spans)

    def get_batch_size(self):
        if self.batch_size is not None:
            return self.batch_size
        elif settings.TORCH_DEVICE_MODEL == "cuda":
            return 6
        return 6

    def surya_layout(self, pages: List[PageGroup]) -> List[LayoutResult]:
        processor = self.layout_model.processor
        layout_results = batch_layout_detection(
            [p.lowres_image for p in pages],
            self.layout_model,
            processor,
            batch_size=int(self.get_batch_size())
        )
        return layout_results

    def add_blocks_to_pages(self, pages: List[PageGroup], layout_results: List[LayoutResult]):
        for page, layout_result in zip(pages, layout_results):
            layout_page_size = PolygonBox.from_bbox(layout_result.image_bbox).size
            provider_page_size = page.polygon.size
            for bbox in sorted(layout_result.bboxes, key=lambda x: x.position):
                block_cls = get_block_class(BlockTypes[bbox.label])
                layout_block = page.add_block(block_cls, PolygonBox(polygon=bbox.polygon))
                layout_block.polygon = layout_block.polygon.rescale(layout_page_size, provider_page_size)
                page.add_structure(layout_block)

    def merge_blocks(self, document_pages: List[PageGroup], provider_page_lines: PageLines, provider_page_spans: PageSpans):
        for document_page, provider_lines in zip(document_pages, provider_page_lines.values()):
            if not self.check_layout_coverage(document_page, provider_lines):
                document_page.text_extraction_method = "surya"
                continue
            line_spans = provider_page_spans[document_page.page_id]
            document_page.merge_blocks(provider_lines, line_spans, text_extraction_method="pdftext")

    def check_layout_coverage(
        self,
        document_page: PageGroup,
        provider_lines: List[Line],
        coverage_threshold=0.5
    ):
        layout_area = 0
        provider_area = 0
        for layout_block_id in document_page.structure:
            layout_block = document_page.get_block(layout_block_id)
            if layout_block.block_type in [BlockTypes.Figure, BlockTypes.Picture, BlockTypes.Table]:
                continue
            layout_area += layout_block.polygon.area
            for provider_line in provider_lines:
                provider_area += layout_block.polygon.intersection_area(provider_line.polygon)
        coverage_ratio = provider_area / layout_area if layout_area > 0 else 0
        return coverage_ratio >= coverage_threshold