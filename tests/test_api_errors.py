import contextlib
import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from lovstudio_skill_helper import api, auth, cli


def http_error(status, body, content_type="application/json", ray=None):
    headers = Message()
    headers["content-type"] = content_type
    if ray:
        headers["cf-ray"] = ray
    return urllib.error.HTTPError(
        "https://lovstudio.ai/api/skills/key", status, "Denied", headers,
        io.BytesIO(body.encode()),
    )


class ApiErrorTests(unittest.TestCase):
    def test_cloudflare_json_keeps_error_and_ray_without_purchase_fallback(self):
        error = http_error(403, json.dumps({
            "cloudflare_error": True,
            "error_code": 1010,
            "error_name": "browser_signature_banned",
            "ray_id": "a36b4196fa1bd5ca",
        }))
        with patch.object(auth, "refresh_if_needed", return_value={"access_token": "test"}), \
             patch("urllib.request.urlopen", side_effect=error), \
             patch.object(cli.config, "load_licenses") as licenses, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as caught:
                cli._fetch_key("example", "1.0.0")
        self.assertEqual(caught.exception.code, 1)
        licenses.assert_not_called()
        output = stderr.getvalue()
        for expected in ("1010", "browser_signature_banned", "a36b4196fa1bd5ca", "entitlement was not checked"):
            self.assertIn(expected, output)
        self.assertNotIn("redeem", output)
        self.assertNotIn("no access", output)

    def test_html_proxy_error_is_not_an_entitlement_denial(self):
        error = api.ApiError.from_http(http_error(403, "<html>private upstream detail</html>", "text/html"))
        self.assertEqual(error.code, "unexpected_http_response")
        self.assertNotIn("private upstream", error.message)

    def test_cloudflare_html_keeps_ray(self):
        error = api.ApiError.from_http(http_error(
            403, "<html>Cloudflare Error 1010</html>", "text/html", "test-ray-SJC",
        ))
        self.assertEqual(error.code, "website_protection_blocked")
        self.assertIn("test-ray-SJC", error.message)

    def test_json_error_code_is_separate_from_ray_diagnostic(self):
        error = api.ApiError.from_http(http_error(
            403, '{"error":"skill_not_owned"}', ray="test-ray-SJC",
        ))
        self.assertEqual(error.code, "skill_not_owned")
        self.assertIn("test-ray-SJC", error.message)

    def test_all_request_paths_identify_the_official_client(self):
        def respond(request, **kwargs):
            self.assertTrue(request.get_header("User-agent").startswith("lovstudio-skill-helper/"))
            return io.BytesIO(b"{}")

        with patch("urllib.request.urlopen", side_effect=respond):
            api.account_skill_key("test", "example", "1.0.0")
            api.call("heartbeat", {})
            api.list_catalog()
            auth._post("https://lovstudio.ai/test", {})

    def test_real_entitlement_denial_can_use_legacy_license(self):
        license = {"license_key": "test", "entitled_skills": ["example"]}
        with patch.object(auth, "refresh_if_needed", return_value={"access_token": "test"}), \
             patch.object(api, "account_skill_key", side_effect=api.ApiError(403, "skill_not_owned")), \
             patch.object(cli.config, "device_id", return_value="device"), \
             patch.object(cli.config, "load_licenses", return_value=[license]), \
             patch.object(api, "skill_keys", return_value={"decryption_key": "11" * 32}):
            self.assertEqual(cli._fetch_key("example", "1.0.0"), bytes.fromhex("11" * 32))

    def test_missing_version_and_unexpected_403_do_not_request_purchase(self):
        for error in (api.ApiError(404, "skill_version_not_found"), api.ApiError(403, "unknown error")):
            with self.subTest(error=error), \
                 patch.object(auth, "refresh_if_needed", return_value={"access_token": "test"}), \
                 patch.object(api, "account_skill_key", side_effect=error), \
                 patch.object(cli.config, "load_licenses") as licenses, \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    cli._fetch_key("example", "1.0.0")
                licenses.assert_not_called()
                self.assertIn(error.message, stderr.getvalue())

    def test_legacy_clock_error_is_not_reported_as_missing_license(self):
        with patch.object(auth, "refresh_if_needed", side_effect=auth.AuthError("not logged in")), \
             patch.object(cli.config, "device_id", return_value="device"), \
             patch.object(cli.config, "load_licenses", return_value=[{"license_key": "test"}]), \
             patch.object(api, "skill_keys", side_effect=api.ApiError(401, "timestamp out of range")), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit):
                cli._fetch_key("example", "1.0.0")
        self.assertIn("timestamp out of range", stderr.getvalue())
        self.assertNotIn("no activated license covers", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
