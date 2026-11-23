import unittest
from datetime import datetime, timezone
from ml_order_alerts import SELLER, eligible, message


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.activation = datetime(2026, 11, 19, tzinfo=timezone.utc)
        self.order = {"id": 2000019718322532, "seller": {"id": SELLER}, "status": "paid",
                      "date_closed": "2026-11-19T14:00:00Z",
                      "order_items": [{"quantity": 2, "item": {"title": "Perfume Moodme 15 ml"}}]}

    def test_paid_only_right_seller_and_new_orders(self):
        self.assertTrue(eligible(self.order, self.activation))
        for change in ({"status": "confirmed"}, {"status": "cancelled"},
                       {"seller": {"id": 999}}, {"date_closed": "2026-11-18T14:00:00Z"},
                       {"date_closed": None}):
            self.assertFalse(eligible({**self.order, **change}, self.activation))

    def test_message_quantities_link_and_posting_timezone(self):
        text = message(self.order, {"expected_date": "2026-11-20T20:00:00Z"})
        for expected in ("2 un. — Perfume Moodme 15 ml", "Venda #2000019718322532", "20/11/2026 às 17:00"):
            self.assertIn(expected, text)
        self.assertEqual(text.splitlines()[-1],
                         "https://www.mercadolivre.com.br/vendas/2000019718322532/detalhe")

    def test_missing_sla_does_not_invent_deadline(self):
        self.assertIn("Ainda não informado", message(self.order, None))

    def test_buffering_calendar_day_is_not_shifted_to_previous_day(self):
        text = message(self.order, {"expected_date": "2026-11-25T23:59:59-03:00"},
                       {"buffering": {"date": "2026-11-24T00:00:00.000Z"}},
                       {"status": "pending", "substatus": "buffered"})
        self.assertIn("Liberação da etiqueta: 24/11/2026 (previsão", text)
        self.assertIn("Prazo de postagem: 25/11/2026 às 23:59", text)
        self.assertIn("horário limite de recebimento da agência", text)

    def test_ready_label_and_missing_or_invalid_release_dates(self):
        self.assertIn("Já disponível", message(self.order, None, None,
                      {"status": "ready_to_ship", "substatus": "ready_to_print"}))
        for lead in (None, {"buffering": {"date": None}}, {"buffering": {"date": "invalid"}}):
            self.assertIn("Liberação da etiqueta: Ainda não informada", message(self.order, None, lead))
        self.assertNotIn("Já disponível", message(self.order, None, None,
                         {"status": "ready_to_ship", "substatus": "invoice_pending"}))


if __name__ == "__main__":
    unittest.main()
