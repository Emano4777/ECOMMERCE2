import unittest
from ml_panel import label_state

class LabelStateTests(unittest.TestCase):
    def test_only_ml_printable_states(self):
        for state, expected in [("ready_to_print", True), ("printed", True),
                                ("invoice_pending", False), ("buffered", False), (None, False)]:
            self.assertEqual(label_state({"id":"1", "status":"ready_to_ship", "substatus":state})["label_ready"], expected)

    def test_no_stale_download_for_transit_or_full(self):
        for extra in [{"status":"shipped"}, {"logistic_type":"fulfillment"}, {"id":None}]:
            shipping = {"id":"1", "status":"ready_to_ship", "substatus":"ready_to_print", **extra}
            self.assertFalse(label_state(shipping)["label_ready"])

    def test_release_date_is_not_posting_deadline(self):
        value = label_state({"substatus":"buffered", "label_release":"2026-11-28T10:00:00Z", "posting_deadline":"2026-11-30T23:59:00Z"})
        self.assertIn("28/11/2026 às 07:00", value["label_message"])
        self.assertFalse(value["label_ready"])
