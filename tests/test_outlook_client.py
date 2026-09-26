# -*- coding: utf-8 -*-
import unittest
import json
from unittest.mock import patch

from core import outlook_client


class OutlookClientContextTests(unittest.TestCase):
    def setUp(self):
        outlook_client._CONTEXT_CACHE.clear()

    @patch("core.db.get_account_by_email")
    @patch("core.db.get_outlook_by_email", return_value=None)
    def test_restores_context_from_registered_account_after_pool_missing(
        self,
        get_outlook_by_email,
        get_account_by_email,
    ):
        get_account_by_email.return_value = {
            "email": "Registered@outlook.test",
            "email_source": "outlook",
            "password": "mail-password",
            "client_id": "client-id",
            "refresh_token": "refresh-token",
            "recovery_email": "recovery@example.test",
            "recovery_code": "recovery-code",
        }

        account = outlook_client.get_account_context("  registered@OUTLOOK.test ")

        self.assertIsNotNone(account)
        self.assertEqual(account.email, "Registered@outlook.test")
        self.assertEqual(account.password, "mail-password")
        self.assertEqual(account.client_id, "client-id")
        self.assertEqual(account.refresh_token, "refresh-token")
        self.assertEqual(account.recovery_email, "recovery@example.test")
        self.assertEqual(account.recovery_code, "recovery-code")
        get_outlook_by_email.assert_called_once_with("  registered@OUTLOOK.test ")
        get_account_by_email.assert_called_once_with("  registered@OUTLOOK.test ")

    @patch("core.db.get_account_by_email")
    @patch("core.db.get_outlook_by_email")
    def test_registered_account_fills_incomplete_pool_context(
        self,
        get_outlook_by_email,
        get_account_by_email,
    ):
        get_outlook_by_email.return_value = {
            "email": "registered@outlook.test",
            "password": "pool-password",
            "client_id": "",
            "refresh_token": "pool-refresh",
        }
        get_account_by_email.return_value = {
            "email": "registered@outlook.test",
            "email_source": "outlook",
            "password": "saved-password",
            "client_id": "saved-client",
            "refresh_token": "saved-refresh",
        }

        account = outlook_client.get_account_context("registered@outlook.test")

        self.assertIsNotNone(account)
        self.assertEqual(account.password, "pool-password")
        self.assertEqual(account.client_id, "saved-client")
        self.assertEqual(account.refresh_token, "pool-refresh")

    @patch("core.db.get_account_by_email", return_value={
        "email": "registered@outlook.test",
        "email_source": "remail",
        "password": "saved-password",
        "client_id": "saved-client",
        "refresh_token": "saved-refresh",
    })
    @patch("core.db.get_outlook_by_email", return_value={
        "email": "registered@outlook.test",
        "password": "pool-password",
        "client_id": "pool-client",
        "refresh_token": "pool-refresh",
    })
    def test_non_outlook_registered_source_is_not_read_from_outlook_pool(
        self,
        get_outlook_by_email,
        get_account_by_email,
    ):
        self.assertIsNone(outlook_client.get_account_context("registered@outlook.test"))

    @patch("core.db.get_account_by_email", return_value=None)
    @patch("core.db.get_outlook_by_email", return_value={
        "email": "Pool@outlook.test",
        "password": "pool-password",
        "client_id": "pool-client",
        "refresh_token": "pool-refresh",
    })
    def test_context_cache_is_case_insensitive(self, get_outlook_by_email, get_account_by_email):
        first = outlook_client.get_account_context("pool@outlook.test")
        second = outlook_client.get_account_context(" POOL@OUTLOOK.TEST ")

        self.assertIs(first, second)
        get_outlook_by_email.assert_called_once_with("pool@outlook.test")
        get_account_by_email.assert_called_once_with("pool@outlook.test")

    def test_container_imap_error_disables_remote_fetch(self):
        error = '{"code":"IMAP_REQUIRES_CONTAINER","error":"IMAP TCP/TLS is unavailable"}'

        self.assertTrue(outlook_client._is_remote_disabled_error(error))
        self.assertTrue(outlook_client._is_imap_only_disabled_error(error))

    @patch.object(outlook_client._email_cfg, "OUTLOOK_FETCH_MODE", "direct")
    def test_direct_mode_only_fetches_graph(self):
        self.assertEqual(outlook_client._fetch_protocols(), ("graph",))

    @patch.object(outlook_client._email_cfg, "OUTLOOK_FETCH_MODE", "auto")
    @patch.object(outlook_client._email_cfg, "OUTLOOK_FETCH_API_URL", "https://fyui.test/api/fetch")
    def test_auto_mode_prefers_configured_fetch_api(self):
        self.assertEqual(outlook_client._fetch_protocols(), ("fyui",))

    def test_fyui_message_is_compatible_with_otp_parser(self):
        item = outlook_client._normalize_fyui_message({
            "id": "message-1",
            "subject": "Your temporary ChatGPT login code",
            "from": {"name": "ChatGPT", "address": "noreply@tm.openai.com"},
            "time": "2026-09-26T10:35:27+00:00",
            "preview": "Enter this temporary verification code to continue: 595718",
            "codes": ["595718"],
            "mailbox": "INBOX",
        })

        self.assertTrue(outlook_client.looks_like_openai_email(item))
        self.assertEqual(outlook_client.extract_otp(item), "595718")
        self.assertGreater(outlook_client._parse_email_ts(item), 0)

    @patch.object(outlook_client, "_persist_rotated_refresh_token", return_value=True)
    def test_fyui_fetch_normalizes_messages_and_persists_rotated_token(self, persist):
        class _Response:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "ok": True,
                    "mode_used": "oauth2",
                    "messages": [{
                        "index": 1,
                        "id": "message-1",
                        "subject": "Your temporary ChatGPT login code",
                        "from": {"name": "ChatGPT", "address": "noreply@tm.openai.com"},
                        "time": "2026-09-26T10:35:27+00:00",
                        "preview": "verification code 595718",
                        "codes": ["595718"],
                        "mailbox": "INBOX",
                    }],
                    "new_refresh_token": "rotated-refresh-token",
                }

        class _Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return _Response()

        account = outlook_client.OutlookAccount("mail@outlook.test", "password", "client", "old-refresh")
        session = _Session()
        rows = outlook_client._fetch_via_fyui(session, account)

        self.assertEqual(len(rows), 1)
        self.assertEqual(outlook_client.extract_otp(rows[0]), "595718")
        self.assertEqual(rows[0]["_fetch_source"], "fyui_api")
        persist.assert_called_once_with(account, "rotated-refresh-token")
        self.assertEqual(json.loads(session.calls[0][1]["data"])["clientId"], "client")
        self.assertEqual(json.loads(session.calls[0][1]["data"])["returnRefreshToken"], True)

    @patch.object(outlook_client, "_secure_post", side_effect=outlook_client.OutlookClientError(
        '{"code":"IMAP_REQUIRES_CONTAINER"}'
    ))
    @patch.object(outlook_client._email_cfg, "OUTLOOK_FETCH_MODE", "remote")
    def test_imap_container_error_disables_only_remote_imap(self, secure_post):
        account = outlook_client.OutlookAccount("mail@outlook.test", "password", "client", "refresh")
        with patch.object(outlook_client, "_REMOTE_DISABLED", False), \
             patch.object(outlook_client, "_REMOTE_IMAP_DISABLED", False):
            rows = outlook_client._fetch_via(object(), "imap", account)

            self.assertEqual(rows, [])
            self.assertFalse(outlook_client._REMOTE_DISABLED)
            self.assertTrue(outlook_client._REMOTE_IMAP_DISABLED)
        secure_post.assert_called_once()

    @patch("core.db.update_outlook_credentials", return_value={"pool": True, "accounts": 1, "changed": True})
    def test_rotated_refresh_token_is_persisted_and_replaces_context(self, update_credentials):
        account = outlook_client.OutlookAccount(
            email="rotate@outlook.test",
            password="password",
            client_id="client-id",
            refresh_token="old-refresh-token",
        )
        old_key = outlook_client._ms_token_cache_key(account)
        outlook_client._MS_TOKEN_CACHE[old_key] = ("graph:old-access", 9999999999)

        self.assertTrue(outlook_client._persist_rotated_refresh_token(account, "new-refresh-token"))
        self.assertEqual(account.refresh_token, "new-refresh-token")
        self.assertNotIn(old_key, outlook_client._MS_TOKEN_CACHE)
        update_credentials.assert_called_once_with(
            "rotate@outlook.test", refresh_token="new-refresh-token"
        )

    @patch.object(outlook_client, "_ms_http")
    @patch.object(outlook_client, "_ms_access_token", return_value=("graph-token", "graph"))
    @patch.object(outlook_client, "_fetch_graph_messages")
    def test_graph_direct_scans_configured_folders(self, fetch_messages, access_token, ms_http):
        class _Http:
            def close(self):
                pass

        ms_http.return_value = _Http()
        fetch_messages.side_effect = lambda _http, _token, folder="inbox": [{"folder": folder}]
        account = outlook_client.OutlookAccount("scan@outlook.test", "password", "client", "refresh")

        rows = outlook_client._fetch_via_graph_direct(account)

        self.assertEqual([row["folder"] for row in rows], ["inbox", "junkemail", "deleteditems"])
        self.assertEqual(fetch_messages.call_count, 3)

    @patch.object(outlook_client, "_ms_http")
    @patch.object(outlook_client, "_ms_access_token", return_value=("access-token", "graph"))
    @patch.object(outlook_client, "get_account_context")
    def test_manual_refresh_uses_current_credentials_without_exposing_tokens(
        self, get_account_context, access_token, ms_http
    ):
        class _Http:
            def close(self):
                pass

        account = outlook_client.OutlookAccount(
            "manual@outlook.test", "password", "client", "refresh"
        )
        get_account_context.return_value = account
        ms_http.return_value = _Http()

        result = outlook_client.refresh_account_token(account.email)

        self.assertEqual(result, {
            "email": "manual@outlook.test",
            "kind": "graph",
            "rotated": False,
            "access_token_obtained": True,
        })
        access_token.assert_called_once_with(account, http=ms_http.return_value)


if __name__ == "__main__":
    unittest.main()
