import unittest
import classificar_tarjas_ecommerce as m

class VerifiedTests(unittest.TestCase):
    def test_all_verified_classes_are_reused(self):
        for tarja in ('vermelha','preta','sem_tarja'):
            self.assertTrue(m.verified_cache(dict(tarja=tarja,tarja_ia=tarja,override_manual=True,
                fonte_classificacao_url='https://fabricante/bula',apresentacao_verificada='Produto 10mg')))

    def test_unverified_ai_is_not_proof(self):
        self.assertFalse(m.verified_cache(dict(tarja='sem_tarja',tarja_ia_confianca='alta')))
        self.assertFalse(m.verified_cache(None))

    def test_undefined_or_conflicting_stripe_does_not_stop_verification(self):
        row=dict(tarja='desconhecida',override_manual=True,fonte_classificacao_url='url',apresentacao_verificada='Produto')
        self.assertFalse(m.verified_cache(row))
        self.assertFalse(m.verified_cache(dict(row,tarja='vermelha',tarja_ia='sem_tarja')))

    def test_positive_history_does_not_expire_after_seven_days(self):
        decision={'tarja':'vermelha'}
        self.assertEqual(m.saved_online_decision({'attempted':1,'decision':decision}),decision)

    def test_inconclusive_history_is_not_verified(self):
        self.assertIsNone(m.saved_online_decision({'attempted':1,'decision':None}))
