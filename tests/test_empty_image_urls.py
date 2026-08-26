from __future__ import annotations

import unittest

from backend.empty_image_urls import (
    EMPTY_PRODUCT_IMAGE_URLS,
    is_empty_product_image_url,
    sku_has_empty_tryon_image,
)


class EmptyImageUrlsTest(unittest.TestCase):
    def test_known_empty_urls(self) -> None:
        for url in EMPTY_PRODUCT_IMAGE_URLS:
            self.assertTrue(is_empty_product_image_url(url))

    def test_valid_url_not_empty(self) -> None:
        url = (
            "https://img.fishfay.com/shopgoods/7/"
            "F11M428119F/F11M428119FBK/1/abc.jpg"
        )
        self.assertFalse(is_empty_product_image_url(url))

    def test_empty_string_not_treated_as_placeholder(self) -> None:
        self.assertFalse(is_empty_product_image_url(""))
        self.assertFalse(is_empty_product_image_url(None))

    def test_marker_substring_match(self) -> None:
        url = (
            "https://img.fishfay.com/theme/images/goods_empty.png"
            "?v=1"
        )
        self.assertTrue(is_empty_product_image_url(url))

    def test_sku_has_empty_tryon_image(self) -> None:
        row = {
            "sku_id": "SKU1",
            "tryon_image": "https://img.fishfay.com/shopgoods/fila_empty.jpg",
        }
        self.assertTrue(sku_has_empty_tryon_image(row))
        row_ok = {
            "sku_id": "SKU2",
            "tryon_image": "https://example.com/tryon.jpg",
        }
        self.assertFalse(sku_has_empty_tryon_image(row_ok))


if __name__ == "__main__":
    unittest.main()
