import unittest
from ml_oauth import DEFAULT_REDIRECT_URI, redirect_uri, token_error


class OAuthConfigurationTests(unittest.TestCase):
    def test_empty_environment_uses_registered_callback(self):
        for value in (None, "", "  "):
            self.assertEqual(redirect_uri(value), DEFAULT_REDIRECT_URI)

    def test_explicit_callback_is_preserved(self):
        self.assertEqual(redirect_uri(" https://example.org/ml/callback "), "https://example.org/ml/callback")

    def test_provider_secrets_are_not_echoed(self):
        code, message = token_error('{"error":"invalid_grant","error_description":"secret-code-123"}')
        self.assertEqual(code, "invalid_grant")
        self.assertNotIn("secret-code-123", message)

    def test_unknown_or_malformed_provider_error_is_safe(self):
        for body in (b"bad response", '[]', '{"error":"secret-token"}'):
            code, message = token_error(body)
            self.assertEqual(code, "unknown")
            self.assertNotIn("secret-token", message)

    def test_operator_gets_owner_account_guidance(self):
        code, message = token_error('{"error":"invalid_operator_user_id"}')
        self.assertEqual(code, "invalid_operator_user_id")
        self.assertIn("titular", message)


if __name__ == "__main__":
    unittest.main()
